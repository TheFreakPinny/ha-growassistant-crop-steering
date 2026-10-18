"""Full native 24h scenarios assert observed policies and durable outcomes."""

import csv
import json
from copy import deepcopy
from pathlib import Path

import pytest

from .scenario import Scenario, ScenarioRunner

EXAMPLE = Path(__file__).parent / "scenarios" / "normal-18-6.json"


def example():
    return json.loads(EXAMPLE.read_text())


async def run(harness, monkeypatch, data):
    return await ScenarioRunner(harness, Scenario.load(data), monkeypatch).run()


def results(runner):
    return [event for event in runner.events if event["kind"] == "shot_result"]


async def test_normal_18_6_day(scenario_harness, monkeypatch, tmp_path):
    runner = await run(scenario_harness, monkeypatch, example())
    assert runner.elapsed == 86400
    phases = [
        event["phase"] for event in runner.events if event["kind"] == "phase_change"
    ]
    assert phases[:4] == ["p0_transpiration", "p1_morning", "p2_midday", "p3_dryback"]
    assert runner.summary()["completed"] == 3
    assert runner.summary()["interrupted"] == runner.summary()["faulted"] == 0
    assert scenario_harness.entry.options["p1_shots_done"] == 1
    assert scenario_harness.entry.options["p2_shots_done"] == 2
    assert scenario_harness.entry.options["p2_ref_vwc"] == 60
    runner.write(tmp_path)
    document = json.loads((tmp_path / "events.json").read_text())
    with (tmp_path / "timeline.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(document["samples"])
    assert rows[-1]["elapsed_seconds"] == "86400"
    assert "<svg" in (tmp_path / "report.html").read_text()
    assert all(event["journal"]["shutdown_confirmed"] for event in results(runner))
    commands = scenario_harness.actor.commands
    assert [command.service for command in commands] == ["turn_on", "turn_off"] * 3
    assert all(
        commands[i + 1].monotonic - commands[i].monotonic >= 0.99
        for i in range(0, 6, 2)
    )


async def test_p1_start_capacity_and_transition(scenario_harness, monkeypatch):
    data = example()
    data["vwc"]["points"] = data["vwc"]["points"][:2]
    runner = await run(scenario_harness, monkeypatch, data)
    decisions = [event for event in runner.events if event["kind"] == "decision"]
    assert any(
        event["decision"]["phase"] == "p1_morning" and event["decision"]["eligible"]
        for event in decisions
    )
    assert runner.summary()["completed"] == 1
    assert scenario_harness.entry.options["p1_done"]
    assert scenario_harness.entry.options["p2_ref_vwc"] == 60
    assert any(
        "p2_drop_not_reached" in event["decision"]["blocking_reasons"]
        for event in decisions
    )


async def test_p2_dryback_and_repeat_shots(scenario_harness, monkeypatch):
    runner = await run(scenario_harness, monkeypatch, example())
    p2 = [row for row in runner.rows if row["phase"] == "p2_midday"]
    assert any("p2_drop_not_reached" in row["blocking_reasons"] for row in p2)
    assert max(row["p2_shots_done"] for row in p2) == 2
    assert [event["journal"]["phase"] for event in results(runner)] == [
        "p1_morning",
        "p2_midday",
        "p2_midday",
    ]


async def test_p3_night_dryback(scenario_harness, monkeypatch):
    runner = await run(scenario_harness, monkeypatch, example())
    night = [row for row in runner.rows if 64800 <= row["elapsed_seconds"] < 86400]
    assert all(
        row["phase"] == "p3_dryback" and row["pump_state"] == "off" for row in night
    )
    assert float(night[-1]["vwc_s1"]) < float(night[0]["vwc_s1"])


async def test_p3_emergency(scenario_harness, monkeypatch):
    data = example()
    data["vwc"]["points"] += [{"at_seconds": 84000, "values": [24, 25, 26]}]
    runner = await run(scenario_harness, monkeypatch, data)
    assert scenario_harness.entry.options["p3_emergency_shots_done"] == 2
    assert (
        len(
            [
                event
                for event in results(runner)
                if event["journal"]["phase"] == "p3_dryback"
            ]
        )
        == 2
    )


async def test_sensor_failure_during_shot(scenario_harness, monkeypatch):
    data = example()
    data["vwc"]["points"] = [
        {"at_seconds": 0, "values": [39, 40, 41]},
        {"at_seconds": 3600.3, "values": [39, "unavailable", 41]},
        {"at_seconds": 4000, "values": [39, 40, 41]},
    ]
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.summary()["interrupted"] == 1
    assert runner.summary()["completed"] == 0
    assert runner.summary()["shot_requests"] == 1
    assert scenario_harness.scheduler.retry_blocked
    assert results(runner)[0]["journal"]["recovery_required"]
    assert scenario_harness.entry.options["p1_shots_done"] == 0


async def test_pump_failure_blocks_followup(scenario_harness, monkeypatch):
    data = example()
    data["events"] = [
        {"at_seconds": 0, "pump": {"fail_on": True}},
        {"at_seconds": 4000, "pump": {"fail_on": False}},
    ]
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.summary()["completed"] == 0
    assert runner.summary()["shot_requests"] == 1
    assert scenario_harness.scheduler.retry_blocked
    assert results(runner)[0]["journal"]["recovery_required"]
    assert (
        sum(command.service == "turn_on" for command in scenario_harness.actor.commands)
        == 1
    )


async def test_synthetic_explicit_test_assumptions(scenario_harness, monkeypatch):
    data = example()
    data["vwc"] = {
        "mode": "synthetic",
        "initial": [39, 40, 41],
        "rise_per_shot": [12, 12, 12],
        "dryback_per_hour": [0.5, 0.5, 0.5],
    }
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.summary()["completed"] >= 2
    assert runner.summary()["interrupted"] == 0
    assert scenario_harness.entry.options["p1_done"]
    assert scenario_harness.entry.options["p2_ref_vwc"] >= 60


async def test_independent_silent_sensor_loss(scenario_harness, monkeypatch):
    data = example()
    data["events"] = [{"at_seconds": 1, "sensor_reports": [True, False, True]}]
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.summary()["shot_requests"] == 0
    assert any(
        event["safety"]["sensors"]["rejected"].get("sensor.native_harness_vwc_1")
        == "stale"
        for event in runner.events
        if event["kind"] == "decision"
    )


async def test_tray_safety_fault_interrupts(scenario_harness, monkeypatch):
    data = example()
    data["events"] = [{"at_seconds": 3600.3, "tray": True}]
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.summary()["interrupted"] == 1
    assert runner.summary()["completed"] == 0
    assert runner.summary()["shot_requests"] == 1
    assert scenario_harness.scheduler.retry_blocked


@pytest.mark.timeout(60)
async def test_missing_off_confirmation_retains_real_deadline(
    scenario_harness, monkeypatch
):
    from custom_components.growassistant_crop_steering.pump import CONFIRM_TIMEOUT

    data = example()
    data["events"] = [{"at_seconds": 0, "pump": {"off_confirmation_delay_s": None}}]
    runner = await run(scenario_harness, monkeypatch, data)
    assert CONFIRM_TIMEOUT == 5.0
    assert runner.summary()["completed"] == 0
    assert runner.summary()["faulted"] == 1
    assert runner.summary()["shot_requests"] == 1
    event = results(runner)[0]
    assert not event["journal"]["shutdown_confirmed"]
    assert event["journal"]["recovery_required"]
    assert event["safety"]["recovery"]["review_required"]
    assert not any(event["kind"] == "shutdown_confirmed" for event in runner.events)
    off = next(
        event
        for event in runner.events
        if event["kind"] == "pump_command" and event["service"] == "turn_off"
    )
    assert (
        event["observed_monotonic_seconds"] - off["observed_monotonic_seconds"]
        >= CONFIRM_TIMEOUT
    )


async def test_endpoint_interruption_is_observed(scenario_harness, monkeypatch):
    data = example()
    data["vwc"]["points"] = [
        {"at_seconds": 0, "values": [60, 60, 60]},
        {"at_seconds": 86399.7, "values": [25, 25, 25]},
    ]
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.summary()["shot_requests"] == 1
    assert runner.summary()["interrupted"] == 1
    assert runner.summary()["completed"] == 0
    assert results(runner)[0]["journal"]["shutdown_confirmed"]
    assert any(event["kind"] == "runner_stop" for event in runner.events)


async def test_dst_keeps_24_elapsed_hours(scenario_harness, monkeypatch):
    data = example()
    data["start"] = "2026-10-24T06:00:00+02:00"
    data["vwc"]["points"] = [{"at_seconds": 0, "values": [60, 60, 60]}]
    runner = await run(scenario_harness, monkeypatch, data)
    assert runner.elapsed == 86400
    assert runner.rows[0]["timestamp"].endswith("06:00:00+02:00")
    assert runner.rows[-1]["timestamp"].endswith("05:00:00+01:00")
    assert runner.summary()["shot_requests"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"start": "2026-10-13T06:00:00"},
        {"config": {"native_enabled": True}},
        {"config": {"pump_switch": "switch.real"}},
        {"config": {"p1_done": True}},
        {"config": {"p1_shot_duration_s": 0}},
        {"config": {"p1_shots": True}},
        {
            "vwc": {
                "mode": "scripted",
                "points": [{"at_seconds": 0, "values": [40, 40]}],
            }
        },
        {"events": [{"at_seconds": 1, "pump": {"target": "switch.real"}}]},
    ],
)
def test_invalid_inputs_are_rejected(change):
    data = deepcopy(example())
    data.update(change)
    with pytest.raises(ValueError):
        Scenario.load(data)
