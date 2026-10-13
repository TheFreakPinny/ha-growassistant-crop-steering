"""Pure decision safety, phase compatibility and civil-time scheduling tests."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from custom_components.growassistant_crop_steering.const import (
    BOOLEAN_STATE_DEFAULTS,
    NUMERIC_SETTING_DEFAULTS,
)
from custom_components.growassistant_crop_steering.irrigation import (
    Inputs,
    capture_inputs,
    evaluate,
    phase_windows,
)

BERLIN = ZoneInfo("Europe/Berlin")


def config(**changes):
    """Resolve existing keys with all normal safety inputs available."""
    return {
        **NUMERIC_SETTING_DEFAULTS,
        **BOOLEAN_STATE_DEFAULTS,
        "led_sunrise": "06:00:00",
        "led_sunset": "18:00:00",
        "p1_mode": "sensor",
        "p2_mode": "sensor",
        "p1_active": True,
        "p1_done": False,
        "p2_ref_vwc": 55,
        "vwc_cap": 90,
        "pump_switch": "switch.pump",
        "pump_state": "off",
        "drain_sensor_configured": False,
        "drain_tray_sensor_configured": True,
        "drain_tray_sensor": "off",
        "last_shot": None,
        "last_shot_type": None,
        **changes,
    }


def now(hour=8, minute=0):
    return datetime(2026, 10, 13, hour, minute, tzinfo=BERLIN)


def decision(at=None, vwc=None, **changes):
    return evaluate(
        Inputs(config(**changes), vwc if vwc is not None else {"sensor.vwc": "40"}),
        at or now(),
    )


@pytest.mark.parametrize(
    "hour,phase",
    [(5, "p3_dryback"), (6, "p0_transpiration"), (7, "p1_morning"), (18, "p3_dryback")],
)
def test_phase_transitions(hour, phase):
    result = decision(now(hour))
    assert result.phase == phase
    assert result.eligible == (phase == "p1_morning")
    if phase == "p0_transpiration":
        assert "p0_blocked" in result.blocking_reasons


@pytest.mark.parametrize("mode", ["sensor", "manual"])
def test_active_p1_modes(mode):
    assert decision(p1_mode=mode).eligible
    assert not decision(p1_mode=mode, p1_active=False).eligible
    assert not decision(p1_mode=mode, p1_done=True).eligible


def test_sensor_p1_extends_window_but_never_past_sunset():
    assert decision(now(16)).eligible
    assert decision(now(16), p1_mode="manual").phase == "p3_dryback"
    assert not decision(now(18)).eligible
    late = decision(now(16), p1_active=False)
    assert late.phase == "p3_dryback"
    assert not late.eligible


def test_p2_completion_and_manual_progression():
    assert decision(p1_active=False, p1_done=True).phase == "p2_midday"
    assert decision(p1_active=False, p1_done=True).eligible
    assert not decision(p1_active=False, p1_done=True, p2_mode="manual").eligible
    result = decision(
        now(12), p1_mode="manual", p1_active=False, p1_done=False, p2_end_offset_min=60
    )
    assert result.phase == "p2_midday"
    assert "p1_not_done" in result.blocking_reasons


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"p1_shots": 0}, "shot_limit_reached"),
        ({"p1_shots_done": 10}, "shot_limit_reached"),
        ({"field_capacity_vwc": 40}, "field_capacity_reached"),
        ({"p1_active": "unknown"}, "invalid_p1_active"),
        ({"p1_done": "nonsense"}, "invalid_p1_done"),
        ({"p1_mode": "unknown"}, "invalid_p1_mode"),
        ({"p2_mode": "unknown"}, "invalid_p2_mode"),
    ],
)
def test_p1_blocking(changes, reason):
    result = decision(**changes)
    assert not result.eligible
    assert reason in result.blocking_reasons


@pytest.mark.parametrize(
    "value",
    [
        None,
        "unknown",
        "unavailable",
        "oops",
        float("nan"),
        float("inf"),
        -1,
        0,
        301,
        True,
    ],
)
@pytest.mark.parametrize("phase", ["p1", "p2", "p3_emergency"])
def test_invalid_durations(value, phase):
    changes = {phase + "_shot_duration_s": value}
    at = now()
    if phase == "p2":
        changes.update(p1_active=False, p1_done=True)
    if phase == "p3_emergency":
        changes.update(p3_emergency_enabled=True, p3_emergency_max_shots=2)
        at = now(19)
    result = decision(at, vwc={"sensor.vwc": 20}, **changes)
    assert not result.eligible
    assert "invalid_" + phase + "_shot_duration_s" in result.blocking_reasons


@pytest.mark.parametrize("value", [None, "on", "unknown", "unavailable", "dry", "OFF"])
def test_pump_state_must_be_exactly_off(value):
    assert "pump_not_off" in decision(pump_state=value).blocking_reasons


@pytest.mark.parametrize(
    "entity", [None, "sensor.pump", "switch.bad space", "pump", 42]
)
def test_invalid_pump_entity(entity):
    assert "invalid_pump_entity" in decision(pump_switch=entity).blocking_reasons


@pytest.mark.parametrize(
    "value",
    [
        None,
        "unknown",
        "unavailable",
        "oops",
        "nan",
        " NaN ",
        "infinity",
        float("inf"),
        -1,
        101,
        True,
    ],
)
def test_invalid_vwc_never_authorizes(value):
    partial = decision(vwc={"sensor.one": 20, "sensor.two": value})
    if value in (None, "unknown", "unavailable"):
        assert partial.eligible
    else:
        assert not partial.eligible
    assert not decision(vwc={"sensor.vwc": value}).eligible


def test_average_and_partial_availability():
    result = decision(
        vwc={"sensor.one": 30, "sensor.two": 50, "sensor.three": "unavailable"}
    )
    assert result.eligible
    assert result.vwc_average == 40
    assert result.vwc_valid_count == 2
    assert not decision(vwc={}).eligible


@pytest.mark.parametrize(
    "value,reason",
    [
        ("on", "wet"),
        ("wet", "wet"),
        ("unavailable", "unavailable"),
        ("unknown", "unavailable"),
        (None, "unavailable"),
        ("garbage", "unavailable"),
    ],
)
@pytest.mark.parametrize("key", ["drain_sensor", "drain_tray_sensor"])
def test_required_p1_drains(value, reason, key):
    result = decision(**{key: value, key + "_configured": True})
    assert not result.eligible
    assert key + "_" + reason in result.blocking_reasons


def test_optional_absent_drains_and_p2_runoff_policy():
    assert decision(drain_tray_sensor_configured=False).eligible
    assert decision(
        p1_active=False, p1_done=True, drain_sensor_configured=True, drain_sensor="wet"
    ).eligible
    assert not decision(
        p1_active=False, p1_done=True, drain_tray_sensor="unavailable"
    ).eligible


@pytest.mark.parametrize(
    "value", [None, float("nan"), float("inf"), "unknown", True, -1, 1.5, 101]
)
@pytest.mark.parametrize(
    "key", ["p1_shots_done", "p1_shots", "p2_shots_done", "p2_shots"]
)
def test_invalid_counters(value, key):
    assert not decision(**{key: value}).eligible


@pytest.mark.parametrize(
    "value", [None, "unknown", float("nan"), float("inf"), -1, 0, 361, True]
)
def test_invalid_soak(value):
    assert not decision(p1_soak_min=value).eligible


@pytest.mark.parametrize(
    "raw", ["invalid", "unavailable", "2026-10-14T08:00:00+02:00", 42]
)
def test_invalid_last_shot(raw):
    assert "invalid_last_shot" in decision(last_shot=raw).blocking_reasons


def test_soak_boundary_and_previous_shot_type():
    last = now() - timedelta(minutes=5)
    result = decision(last_shot=last.isoformat())
    assert result.blocking_reasons == ("soak_active",)
    assert result.next_eligible_time == now() + timedelta(minutes=10)
    assert decision(now(8, 10), last_shot=last.isoformat()).eligible
    assert decision(last_shot=last.isoformat(), last_shot_type="p2").eligible
    assert not decision(last_shot=last.isoformat(), last_shot_type="bad").eligible
    assert "missing_last_shot" in decision(p1_shots_done=1).blocking_reasons
    assert (
        "missing_last_shot"
        in decision(p1_active=False, p1_done=True, p1_shots_done=1).blocking_reasons
    )


def test_soak_cannot_schedule_beyond_window():
    result = decision(
        now(11, 59), p1_active=False, p1_done=True, last_shot=now(11, 58).isoformat()
    )
    assert result.phase == "p2_midday"
    assert result.soak_until is not None
    assert result.next_eligible_time is None


def test_p2_end_offset_and_limits():
    assert decision(now(11, 59), p1_active=False, p1_done=True).eligible
    assert decision(now(12), p1_active=False, p1_done=True).phase == "p3_dryback"
    assert decision(p1_active=False, p1_done=True, p2_shots=0).phase == "p3_dryback"
    assert (
        decision(p1_active=False, p1_done=True, p2_shots_done=1).phase == "p3_dryback"
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"p2_ref_vwc": 0}, "p2_reference_missing"),
        ({"p2_ref_vwc": 44}, "p2_drop_not_reached"),
        ({"vwc_cap": 40}, "vwc_cap_active"),
        ({"p2_vwc_drop": "nan"}, "invalid_p2_vwc_drop"),
    ],
)
def test_p2_thresholds(changes, reason):
    result = decision(p1_active=False, p1_done=True, **changes)
    assert reason in result.blocking_reasons
    assert not result.eligible


def test_p3_emergency():
    base = dict(p3_emergency_enabled=True, p3_emergency_max_shots=2)
    assert decision(now(19), vwc={"sensor.vwc": 30}, **base).eligible
    assert not decision(now(19), vwc={"sensor.vwc": 30.1}, **base).eligible
    assert not decision(
        now(19), vwc={"sensor.vwc": 20}, **{**base, "p3_emergency_enabled": False}
    ).eligible
    assert not decision(
        now(19), vwc={"sensor.vwc": 20}, **{**base, "p3_emergency_shots_done": 2}
    ).eligible
    assert not decision(
        now(19), vwc={"sensor.vwc": 20}, **base, drain_tray_sensor_configured=False
    ).eligible
    assert not decision(now(6), vwc={"sensor.vwc": 20}, **base).eligible


def test_overnight_cycle_identifier_and_windows():
    settings = config(
        led_sunrise="20:00:00", led_sunset="08:00:00", p2_end_offset_min=60
    )
    first = evaluate(Inputs(settings, {"sensor.vwc": 40}), now(22))
    after_midnight = evaluate(
        Inputs(settings, {"sensor.vwc": 40}), now(2) + timedelta(days=1)
    )
    assert first.eligible and after_midnight.eligible
    assert first.cycle_id == after_midnight.cycle_id
    assert first.windows.sunset == now(8) + timedelta(days=1)
    assert not evaluate(
        Inputs(settings, {"sensor.vwc": 40}), now(8) + timedelta(days=1)
    ).eligible


@pytest.mark.parametrize(
    "month,day,last_hour,last_fold,now_hour,now_fold",
    [(3, 29, 1, 0, 3, 0), (10, 25, 2, 0, 2, 1)],
)
def test_dst_soak_elapsed_time(month, day, last_hour, last_fold, now_hour, now_fold):
    last = datetime(2026, month, day, last_hour, 55, tzinfo=BERLIN, fold=last_fold)
    at = datetime(2026, month, day, now_hour, 5, tzinfo=BERLIN, fold=now_fold)
    settings = config(
        led_sunrise="20:00:00",
        led_sunset="08:00:00",
        p0_transpiration_min=0,
        p1_duration_min=600,
        p2_end_offset_min=60,
        last_shot=last.isoformat(),
    )
    result = evaluate(Inputs(settings, {"sensor.vwc": 40}), at)
    assert result.blocking_reasons == ("soak_active",)
    assert result.soak_until.astimezone(UTC) - last.astimezone(UTC) == timedelta(
        minutes=15
    )
    assert evaluate(Inputs(settings, {"sensor.vwc": 40}), result.soak_until).eligible


@pytest.mark.parametrize("month,day", [(3, 29), (10, 25)])
def test_dst_unsafe_boundary_fails_closed(month, day):
    at = datetime(2026, month, day, 4, tzinfo=BERLIN)
    result = decision(at, led_sunrise="02:30:00")
    assert "invalid_schedule" in result.blocking_reasons
    assert not result.eligible


@pytest.mark.parametrize(
    "changes",
    [
        {"led_sunrise": None},
        {"led_sunset": "unknown"},
        {"led_sunrise": "25:00:00"},
        {"led_sunset": "06:00:00"},
        {"p0_transpiration_min": "nan"},
        {"p1_duration_min": float("inf")},
        {"p2_end_offset_min": -1},
    ],
)
def test_invalid_schedule(changes):
    assert "invalid_schedule" in decision(**changes).blocking_reasons


def test_naive_now_rejected():
    assert not decision(datetime(2026, 10, 13, 8)).eligible
    with pytest.raises(ValueError):
        phase_windows(Inputs(config(), {}), datetime(2026, 10, 13, 8))


def test_determinism_immutability_and_json():
    original = config()
    snapshot = Inputs(original, {"sensor.vwc": "40"})
    first = evaluate(snapshot, now())
    original["pump_state"] = "on"
    assert first == evaluate(snapshot, now())
    assert json.loads(json.dumps(first.as_dict()))["eligible"] is True
    with pytest.raises(TypeError):
        snapshot.config["pump_state"] = "on"


def test_capture_legacy_managed_precedence_and_no_side_effects():
    states = {
        "input_datetime.sunrise": SimpleNamespace(state="06:00:00", attributes={}),
        "input_datetime.sunset": SimpleNamespace(state="18:00:00", attributes={}),
        "switch.pump": SimpleNamespace(state="off", attributes={}),
        "sensor.vwc": SimpleNamespace(state="40", attributes={}),
        "number.duration": SimpleNamespace(state="90", attributes={}),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get), services=Mock())
    entry = SimpleNamespace(
        data={
            "led_sunrise": "input_datetime.sunrise",
            "led_sunset": "input_datetime.sunset",
            "pump_switch": "switch.pump",
            "vwc_sensor": ["sensor.vwc"],
            "p1_shot_duration_s": "number.duration",
        },
        options={"p1_active": True},
    )
    before = deepcopy(entry)
    captured = capture_inputs(hass, entry)
    assert evaluate(captured, now()).eligible
    assert evaluate(captured, now()).duration_s == 90
    entry.options["p1_shot_duration_s"] = 80
    assert evaluate(capture_inputs(hass, entry), now()).duration_s == 80
    entry.options = before.options
    for _ in range(3):
        assert captured == capture_inputs(hass, entry)
        assert evaluate(captured, now()).eligible
    assert entry == before
    assert hass.services.mock_calls == []


@pytest.mark.parametrize("mode", ["sensor", "manual"])
@pytest.mark.parametrize(
    "active,complete", [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("overnight", [False, True])
@pytest.mark.parametrize("hour", [0, 6, 8, 12, 20])
def test_existing_phase_semantics_outside_dst(
    mode, active, complete, overnight, hour, monkeypatch
):
    from custom_components.growassistant_crop_steering import sensor

    settings = config(p1_mode=mode, p1_active=active, p1_done=complete)
    if overnight:
        settings.update(led_sunrise="20:00:00", led_sunset="08:00:00")
    at = now(hour)
    monkeypatch.setattr(sensor.dt_util, "now", lambda: at)
    states = {
        "input_datetime.sunrise": SimpleNamespace(
            state=settings["led_sunrise"], attributes={}
        ),
        "input_datetime.sunset": SimpleNamespace(
            state=settings["led_sunset"], attributes={}
        ),
    }
    entry = SimpleNamespace(
        data={
            "led_sunrise": "input_datetime.sunrise",
            "led_sunset": "input_datetime.sunset",
        },
        options={
            key: value
            for key, value in settings.items()
            if key not in {"led_sunrise", "led_sunset"}
        },
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    expected, _ = sensor._calculate_phase(hass, entry)
    assert evaluate(Inputs(settings, {"sensor.vwc": 40}), at).phase == expected


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"hour": 6, "minute": 0}, "06:00:00"),
        ({"hour": 25, "minute": 0}, "invalid"),
        ({"hour": float("nan"), "minute": 0}, "invalid"),
    ],
)
def test_capture_clock_attributes(attrs, expected):
    entry = SimpleNamespace(data={"led_sunrise": "input_datetime.sunrise"}, options={})
    states = {
        "input_datetime.sunrise": SimpleNamespace(
            state="2026-10-13 06:00:00", attributes=attrs
        )
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    assert capture_inputs(hass, entry).config["led_sunrise"] == expected


@pytest.mark.parametrize(
    "value", [None, "unknown", float("nan"), float("inf"), -1, 101]
)
def test_invalid_p3_threshold(value):
    assert not decision(
        now(19),
        vwc={"sensor.vwc": 20},
        p3_emergency_enabled=True,
        p3_emergency_max_shots=2,
        p3_emergency_threshold_vwc=value,
    ).eligible


def test_interval_helpers_do_not_add_gates():
    assert decision(p1_interval_min="unknown", p2_interval_min="unknown").eligible
    assert decision(p1_active=False, p1_done=True, p2_interval_min=360).eligible


def test_capture_cleared_optional_sensor_does_not_fall_back():
    entry = SimpleNamespace(
        data={"drain_tray_sensor": "binary_sensor.tray"},
        options={"drain_tray_sensor": ""},
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None))
    assert capture_inputs(hass, entry).config["drain_tray_sensor_configured"] is False


def test_capture_missing_legacy_timestamp_is_invalid():
    entry = SimpleNamespace(data={"last_shot": "input_datetime.last"}, options={})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None))
    assert capture_inputs(hass, entry).config["last_shot"] == "invalid"


@pytest.mark.parametrize("mode", ["sensor", "manual"])
def test_active_p1_uses_capacity_not_start_threshold(mode):
    result = decision(p1_mode=mode, p1_start_vwc=30)
    assert result.eligible
    assert not result.p1_start_eligible
    assert not decision(p1_mode=mode, p1_start_vwc=30, field_capacity_vwc=40).eligible


def test_separate_p1_start_readiness():
    result = decision(p1_active=False)
    assert result.p1_start_eligible
    assert not result.eligible
    assert result.blocking_reasons == ("p1_not_active",)
    assert not decision(p1_active=False, p1_start_vwc=39).p1_start_eligible
    assert not decision(p1_active=False, p1_start_vwc="nan").p1_start_eligible
    assert not decision(p1_active=False, p1_window_opened_today=True).p1_start_eligible
    assert not decision(p1_active=False, p1_mode="manual").p1_start_eligible
    assert not decision(now(13), p1_active=False).p1_start_eligible
    assert not decision(p1_active=False, drain_tray_sensor="wet").p1_start_eligible


@pytest.mark.parametrize("value", ["30.5", "nan", "number.duration", None])
def test_capture_managed_numeric_strings_are_not_entity_ids(value):
    hass = SimpleNamespace(states=SimpleNamespace(get=Mock(return_value=None)))
    entry = SimpleNamespace(data={}, options={"p1_shot_duration_s": value})
    snapshot = capture_inputs(hass, entry)
    assert snapshot.config["p1_shot_duration_s"] == value
    hass.states.get.assert_not_called()


def test_invalid_flags_and_mutable_values_fail_closed():
    assert not decision(p1_active=1.0).eligible
    assert not decision(p1_mode=[]).eligible
    assert not decision(vwc={"sensor.vwc": []}).eligible


def test_p3_soak_and_required_tray_blocks():
    base = dict(p3_emergency_enabled=True, p3_emergency_max_shots=2)
    for state in ("wet", "unavailable", None):
        assert not decision(
            now(19), vwc={"sensor.vwc": 20}, drain_tray_sensor=state, **base
        ).eligible
    result = decision(
        now(19), vwc={"sensor.vwc": 20}, last_shot=now(18, 55).isoformat(), **base
    )
    assert result.blocking_reasons == ("soak_active",)
    assert result.next_eligible_time == now(19, 10)


def test_overnight_p2_cutoff():
    settings = dict(
        led_sunrise="20:00:00",
        led_sunset="08:00:00",
        p2_end_offset_min=60,
        p1_active=False,
        p1_done=True,
    )
    assert decision(now(6, 59), **settings).eligible
    assert decision(now(7), **settings).phase == "p3_dryback"
