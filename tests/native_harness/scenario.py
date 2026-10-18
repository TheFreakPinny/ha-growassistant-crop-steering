"""Test-only scenario inputs and native scheduler observations.

Civil time can jump while idle. Active shots spend real monotonic time; no
production timer, safety gate, journal or ownership implementation is patched.
"""

from __future__ import annotations

import asyncio
import csv
import html
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.core import callback
from homeassistant.helpers import event as event_helper
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.growassistant_crop_steering import scheduler as scheduler_module
from custom_components.growassistant_crop_steering.const import (
    BOOLEAN_STATE_DEFAULTS,
    NUMERIC_SETTING_DESCRIPTIONS,
)
from custom_components.growassistant_crop_steering.shot_engine import (
    SIGNAL_SHOT_FINISHED,
    ShotRejected,
)

from .harness import PUMP, SENSORS

COUNTERS = ("p1_shots_done", "p2_shots_done", "p3_emergency_shots_done")
# Managed state is exclusively owned by the native controllers, never by input.
STATE = {*COUNTERS, "p1_active", "p1_done", "p1_window_opened_today", "p2_ref_vwc"}


def number(value, minimum, maximum):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    if not minimum <= value <= maximum:
        raise ValueError(f"Number outside [{minimum}, {maximum}]")
    return value


def readings(values):
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError("Exactly three independent VWC values required")
    for value in values:
        if value is not None and value not in ("unavailable", "unknown"):
            number(value, 0, 100)
    return values


@dataclass(frozen=True)
class Scenario:
    """Validated JSON format; existing configuration keys and original units."""

    data: dict
    start: datetime
    zone: ZoneInfo

    @classmethod
    def load(cls, data):
        allowed = {
            "name",
            "start",
            "timezone",
            "lighting",
            "config",
            "vwc",
            "events",
            "sample_seconds",
        }
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Unknown scenario fields")
        data = json.loads(json.dumps(data, allow_nan=False))
        if not isinstance(data.get("name"), str) or not data["name"]:
            raise ValueError("Scenario name required")
        zone = ZoneInfo(data["timezone"])
        start = datetime.fromisoformat(data["start"])
        if (
            start.tzinfo is None
            or start.utcoffset() != start.astimezone(zone).utcoffset()
        ):
            raise ValueError("Start must have the timezone's explicit UTC offset")
        start = start.astimezone(UTC)
        lighting = data.setdefault(
            "lighting", {"sunrise": "06:00:00", "sunset": "00:00:00"}
        )
        if set(lighting) != {"sunrise", "sunset"}:
            raise ValueError("Lighting requires sunrise and sunset wall times")
        for value in lighting.values():
            if time.fromisoformat(value).tzinfo is not None:
                raise ValueError("Lighting uses local wall time")
        if lighting["sunrise"] == lighting["sunset"]:
            raise ValueError("Sunrise and sunset must differ")
        specs = {spec.key: spec for spec in NUMERIC_SETTING_DESCRIPTIONS}
        for key, value in data.setdefault("config", {}).items():
            if key in STATE:
                raise ValueError("Managed cycle state cannot be supplied")
            if key in specs:
                spec = specs[key]
                number(value, spec.native_min_value, spec.native_max_value)
            elif key in BOOLEAN_STATE_DEFAULTS:
                if type(value) is not bool:
                    raise ValueError("Boolean setting required")
            elif key in {"p1_mode", "p2_mode"} and value in {"sensor", "manual"}:
                pass
            else:
                raise ValueError(f"Unknown steering setting: {key}")
        number(data.setdefault("sample_seconds", 60), 1, 240)
        model = data["vwc"]
        if model.get("mode") == "scripted":
            if set(model) != {"mode", "points"} or not model["points"]:
                raise ValueError("Scripted model requires points")
            previous = -1
            for point in model["points"]:
                if set(point) != {"at_seconds", "values"}:
                    raise ValueError("Invalid VWC point")
                at = number(point["at_seconds"], 0, 86400)
                if at <= previous:
                    raise ValueError("VWC points must be strictly increasing")
                previous = at
                readings(point["values"])
            if model["points"][0]["at_seconds"] != 0:
                raise ValueError("Scripted model must start at zero")
        elif model.get("mode") == "synthetic":
            if set(model) != {"mode", "initial", "rise_per_shot", "dryback_per_hour"}:
                raise ValueError("Synthetic model requires explicit assumptions")
            readings(model["initial"])
            for key in ("initial", "rise_per_shot", "dryback_per_hour"):
                values = model[key]
                if len(values) != 3:
                    raise ValueError("Three model parameters required")
                for value in values:
                    number(value, 0, 100)
        else:
            raise ValueError("VWC mode must be scripted or synthetic")
        previous = -1
        for event in data.setdefault("events", []):
            if set(event) - {
                "at_seconds",
                "sensor_reports",
                "pump",
                "tray",
                "drain",
                "lose_ownership",
            }:
                raise ValueError("Unknown fault event")
            at = number(event["at_seconds"], 0, 86400)
            if at < previous:
                raise ValueError("Events must be sorted")
            previous = at
            if "sensor_reports" in event and (
                len(event["sensor_reports"]) != 3
                or any(type(v) is not bool for v in event["sensor_reports"])
            ):
                raise ValueError("sensor_reports requires three booleans")
            for key in ("tray", "drain", "lose_ownership"):
                if key in event and type(event[key]) is not bool:
                    raise ValueError("Safety event requires boolean")
            for key, value in event.get("pump", {}).items():
                if key == "fail_on" and type(value) is bool:
                    continue
                if key in {
                    "on_service_delay_s",
                    "off_service_delay_s",
                    "on_confirmation_delay_s",
                    "off_confirmation_delay_s",
                }:
                    if value is None and "confirmation" in key:
                        continue
                    number(value, 0, 30)
                else:
                    raise ValueError("Unknown fake pump setting")
        return cls(data, start, zone)

    @classmethod
    def read(cls, path):
        return cls.load(json.loads(Path(path).read_text()))


class ScenarioRunner:
    """Observe unmodified native calls; only fixed harness IO receives inputs."""

    def __init__(self, harness, scenario, monkeypatch):
        self.h = harness
        self.scenario = scenario
        self.events = []
        self.rows = []
        self.elapsed = 0.0
        self.phase = None
        self.last_decision = None
        self.monotonic_start = harness.hass.loop.time()
        self.reporting = [True] * 3
        model = scenario.data["vwc"]
        self.values = list(
            model.get("initial", model.get("points", [{}])[0].get("values", []))
        )
        self.point = 0
        self.event = 0
        self.operation = 0
        # HA retains a separate alias for civil scheduler deadlines. Keep that
        # alias on the harness clock too; asyncio/loop.time remain untouched.
        monkeypatch.setattr(
            event_helper, "time_tracker_utcnow", lambda: harness.clock.at
        )
        # Only the scheduler's *civil* wakeups use a deterministic queue. Invoke
        # its original callback at the requested instant, including cancellation.
        # Pump/shot/asyncio monotonic timers retain their real implementations.
        self.civil_timers = {}

        def track(hass, action, when):
            token = object()
            self.civil_timers[token] = (when.astimezone(UTC), action)
            return lambda: self.civil_timers.pop(token, None)

        monkeypatch.setattr(scheduler_module, "async_track_point_in_utc_time", track)
        self._instrument(monkeypatch)

    def emit(self, kind, **data):
        event = {
            "sequence": len(self.events),
            "timestamp": self.h.clock.at.astimezone(self.scenario.zone).isoformat(),
            "elapsed_seconds": round(self.elapsed, 6),
            "kind": kind,
            "observed_monotonic_seconds": self.h.hass.loop.time()
            - self.monotonic_start,
            **data,
        }
        self.events.append(event)
        return event

    def safety(self):
        return {
            "sensors": self.h.shots.sensor_assessment().diagnostic(),
            "recovery": self.h.shots.recovery_assessment.diagnostic(),
            "retry_blocked": self.h.scheduler.retry_blocked,
            "execution_available": self.h.shots.execution_available,
            "pump_controller": self.h.pump.state.value,
        }

    def snapshot(self, kind, **details):
        assessment = self.h.shots.sensor_assessment()
        decision = self.last_decision
        row = {
            "timestamp": self.h.clock.at.astimezone(self.scenario.zone).isoformat(),
            "elapsed_seconds": round(self.elapsed, 6),
            "kind": kind,
            "phase": self.phase,
            **{
                f"vwc_s{i + 1}": None
                if (state := self.h.hass.states.get(entity)) is None
                else state.state
                for i, entity in enumerate(SENSORS)
            },
            "vwc_average": assessment.average,
            "pump_state": self.h.hass.states.get(PUMP).state,
            "shot_state": self.h.shots.state.value,
            **{key: self.h.entry.options.get(key, 0) for key in COUNTERS},
            "p2_ref_vwc": self.h.entry.options.get("p2_ref_vwc", 0),
            "eligible": decision.eligible if decision else None,
            "blocking_reasons": list(decision.blocking_reasons) if decision else [],
            "retry_blocked": self.h.scheduler.retry_blocked,
            "execution_available": self.h.shots.execution_available,
            "details": details,
        }
        self.rows.append(row)

    def _instrument(self, monkeypatch):
        active_safety = self.h.shots._active_safety

        def check(snapshot):
            reason = active_safety(snapshot)
            self.emit(
                "active_safety_check",
                operation_id=snapshot.operation_id,
                blocking_reason=reason,
                safety=self.safety(),
            )
            return reason

        monkeypatch.setattr(self.h.shots, "_active_safety", check)
        stop_locked = self.h.pump._stop_locked

        async def shutdown_check():
            confirmed = await stop_locked()
            self.emit(
                "pump_shutdown_check",
                confirmed=confirmed,
                controller_state=self.h.pump.state.value,
            )
            return confirmed

        monkeypatch.setattr(self.h.pump, "_stop_locked", shutdown_check)

        @callback
        def finished(result):
            if result is None:
                self.emit("shot_result_unavailable", safety=self.safety())
                return
            record = self.h.records["shots"]["operations"][result.operation_id]
            self.emit(
                "shot_result",
                result=asdict(result),
                journal=record,
                safety=self.safety(),
            )
            self.snapshot("shot_result", result=asdict(result))
            if record.get("shutdown_confirmed"):
                self.emit(
                    "shutdown_confirmed",
                    operation_id=result.operation_id,
                    accounted=record.get("accounted", False),
                )

        self.remove_finished = async_dispatcher_connect(
            self.h.hass, f"{SIGNAL_SHOT_FINISHED}_{self.h.entry.entry_id}", finished
        )
        original_decision = self.h.shots.validated_decision

        def decision(*args, **kwargs):
            value = original_decision(*args, **kwargs)
            if value.phase != self.phase:
                self.emit("phase_change", previous=self.phase, phase=value.phase)
                self.phase = value.phase
            self.emit("decision", decision=value.as_dict(), safety=self.safety())
            self.last_decision = value
            self.snapshot("decision", decision=value.as_dict())
            return value

        monkeypatch.setattr(self.h.shots, "validated_decision", decision)
        original_execute = self.h.shots.async_execute

        async def execute(operation_id=None):
            self.operation += 1
            # Stable test operation identity; does not change authorization.
            operation_id = operation_id or f"scenario-shot-{self.operation:04d}"
            self.emit("shot_request", operation_id=operation_id, safety=self.safety())
            try:
                result = await original_execute(operation_id)
            except ShotRejected as error:
                self.emit(
                    "shot_rejected",
                    operation_id=operation_id,
                    reasons=error.reasons,
                    safety=self.safety(),
                )
                raise
            model = self.scenario.data["vwc"]
            if result.accounted and model["mode"] == "synthetic":
                self.values = [
                    min(100, value + rise)
                    for value, rise in zip(
                        self.values, model["rise_per_shot"], strict=True
                    )
                ]
                # A post-shot test response, with gradual independent reports.
                # Never inject recovery/ownership changes or clear a fault.
                if all(self.reporting) and all(
                    self.h.hass.states.get(entity) is not None for entity in SENSORS
                ):
                    await self.h.ramp_vwc(*self.values)
                    self.emit("synthetic_response", values=list(self.values))
            return result

        monkeypatch.setattr(self.h.shots, "async_execute", execute)
        original_call = self.h.actor.async_call

        async def command(domain, service, *args, **kwargs):
            if service == "turn_on" and self.h.shots.snapshot is not None:
                reservation = self.h.shots.snapshot
                self.emit(
                    "shot_authorized",
                    operation_id=reservation.operation_id,
                    decision=reservation.decision.as_dict(),
                    safety=self.safety(),
                )
            self.emit("pump_command", service=service)
            return await original_call(domain, service, *args, **kwargs)

        monkeypatch.setattr(self.h.actor, "async_call", command)
        original_report = self.h.actor.report

        def report(value):
            original_report(value)
            self.emit("pump_report", state=value)
            self.snapshot("pump_report", state=value)

        monkeypatch.setattr(self.h.actor, "report", report)

    def _inputs(self, elapsed, delta):
        model = self.scenario.data["vwc"]
        if model["mode"] == "scripted":
            points = model["points"]
            while (
                self.point + 1 < len(points)
                and points[self.point + 1]["at_seconds"] <= elapsed
            ):
                self.point += 1
            self.values = list(points[self.point]["values"])
        else:
            on = self.h.hass.states.get(PUMP).state == "on"
            self.values = [
                max(
                    0,
                    min(
                        100,
                        value + (0 if on else -dry * delta / 3600),
                    ),
                )
                for value, dry in zip(
                    self.values,
                    model["dryback_per_hour"],
                    strict=True,
                )
            ]
        events = self.scenario.data["events"]
        while self.event < len(events) and events[self.event]["at_seconds"] <= elapsed:
            event = events[self.event]
            self.event += 1
            self.emit("input_event", input=event)
            if "sensor_reports" in event:
                self.reporting = list(event["sensor_reports"])
            for key in ("drain", "tray"):
                if key in event:
                    self.h.hass.states.async_set(
                        f"binary_sensor.native_harness_{key}",
                        "on" if event[key] else "off",
                    )
            if event.get("lose_ownership"):
                self.h.shots.ownership = None
            for key, value in event.get("pump", {}).items():
                if key == "fail_on":
                    self.h.actor.fail_on = value
                else:
                    service = "turn_on" if key.startswith("on_") else "turn_off"
                    target = (
                        self.h.actor.confirmation_delay
                        if "confirmation" in key
                        else self.h.actor.service_delay
                    )
                    target[service] = value
        for entity, value, reporting in zip(
            SENSORS, self.values, self.reporting, strict=True
        ):
            if not reporting:
                continue
            if value is None:
                self.h.hass.states.async_remove(entity)
            else:
                self.h.hass.states.async_set(
                    entity,
                    str(value),
                    force_update=True,
                    timestamp=self.h.clock.at.timestamp(),
                )

    async def settle(self):
        # Drain immediate HA work, but never wait for real-duration background
        # shots here. Pending scheduler callbacks must run before any time jump.
        async with asyncio.timeout(5):
            while True:
                await self.h.hass.async_block_till_done()
                await asyncio.sleep(0.001)
                scheduler = self.h.scheduler
                if self.h.shots.execution_active or (
                    scheduler._pending is None
                    and (scheduler._task is None or scheduler._task.done())
                ):
                    return

    def next_idle(self):
        data = self.scenario.data
        candidates = [86400, self.elapsed + data["sample_seconds"]]
        if self.event < len(data["events"]):
            candidates.append(data["events"][self.event]["at_seconds"])
        model = data["vwc"]
        if model["mode"] == "scripted" and self.point + 1 < len(model["points"]):
            candidates.append(model["points"][self.point + 1]["at_seconds"])
        when = self.h.scheduler.next_evaluation
        if when:
            candidates.append(
                (when.astimezone(UTC) - self.scenario.start).total_seconds()
            )
        return min(value for value in candidates if value > self.elapsed + 1e-6)

    async def run(self):
        h = self.h
        h.configure(**self.scenario.data["config"])
        h.lighting(**self.scenario.data["lighting"])
        await h.hass.config.async_set_time_zone(self.scenario.zone.key)
        h.move_to(self.scenario.start.isoformat(), refresh=False)
        self._inputs(0, 0)
        await h.scheduler.async_start()
        await self.settle()
        self.snapshot("sample")
        while self.elapsed < 86400:
            active = h.shots.execution_active or (
                h.scheduler._task is not None and not h.scheduler._task.done()
            )
            next_time = (
                min(86400, round(self.elapsed + 0.1, 6)) if active else self.next_idle()
            )
            delta = next_time - self.elapsed
            # Active civil ticks are paced with real asyncio time. Only idle
            # intervals jump. Production monotonic timers are never replaced.
            if active:
                await asyncio.sleep(delta)
            self.elapsed = next_time
            h.move_to(
                (self.scenario.start + timedelta(seconds=self.elapsed)).isoformat(),
                refresh=False,
            )
            if self.elapsed == 86400:
                break  # End is exclusive: do not reset/start the following day.
            self._inputs(self.elapsed, delta)
            for token, (when, action) in list(self.civil_timers.items()):
                if when <= h.clock.at:
                    self.civil_timers.pop(token, None)
                    action(h.clock.at)
            h.scheduler.async_request_evaluation()
            await self.settle()
            self.snapshot("sample")
        # No new evaluation beyond the 24-hour observation window. Stop drains
        # an in-flight boundary shot safely and records its actual interruption.
        self.emit("runner_stop", reason="24_hour_endpoint")
        await h.scheduler.async_stop()
        await h.hass.async_block_till_done()
        self.snapshot("end")
        self.emit("safety_final", safety=self.safety())
        self.remove_finished()
        return self

    def summary(self):
        results = [
            event["result"] for event in self.events if event["kind"] == "shot_result"
        ]
        return {
            "completed": sum(result["accounted"] for result in results),
            "interrupted": sum(result["state"] == "interrupted" for result in results),
            "faulted": sum(result["state"] == "fault" for result in results),
            "rejected_requests": sum(
                event["kind"] == "shot_rejected" for event in self.events
            ),
            "blocked_evaluations": sum(
                event["kind"] == "decision" and not event["decision"]["eligible"]
                for event in self.events
            ),
            "shot_requests": sum(
                event["kind"] == "shot_request" for event in self.events
            ),
        }

    def write(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        document = {
            "scenario": self.scenario.data,
            "duration_seconds": 86400,
            "synthetic_model_is_test_assumption": True,
            "summary": self.summary(),
            "events": self.events,
            "samples": self.rows,
            "final_checkpoint": self.h.checkpoint(),
        }
        (directory / "events.json").write_text(
            json.dumps(document, indent=2, allow_nan=False) + "\n"
        )
        with (directory / "timeline.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(
                {
                    key: json.dumps(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
                for row in self.rows
            )
        (directory / "report.html").write_text(render_report(document))


def render_report(document):
    """Standalone SVG charts; no network, CDN or executable scenario content."""
    rows = document["samples"]

    def chart(title, series, maximum, labels, *, step=False):
        paths = []
        colors = ("#2563eb", "#16a34a", "#e8790c", "#111827")
        for index, (name, values) in enumerate(series.items()):
            segments = []
            pen = False
            previous_y = 0
            for row, value in zip(rows, values, strict=True):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    pen = False
                    continue
                if not math.isfinite(value):
                    pen = False
                    continue
                x, y = (
                    60 + row["elapsed_seconds"] / 86400 * 900,
                    200 - value / maximum * 160,
                )
                if step and pen:
                    segments.append(f"L{x:.2f},{previous_y:.2f}")
                segments.append(f"{'L' if pen else 'M'}{x:.2f},{y:.2f}")
                previous_y = y
                pen = True
            paths.append(
                f'<path d="{" ".join(segments)}" fill="none" stroke="{colors[index]}"/><text x="{65 + index * 180}" y="22" fill="{colors[index]}">{html.escape(name)}</text>'
            )
        ticks = "".join(
            f'<text x="{60 + hour / 24 * 900}" y="225">{hour}h</text>'
            for hour in range(0, 25, 3)
        )
        return f'<h2>{html.escape(title)}</h2><p>{html.escape(labels)}</p><svg viewBox="0 0 1000 240" role="img" aria-label="{html.escape(title)}"><path d="M60,35 V200 H960" stroke="#999" fill="none"/>{"".join(paths)}{ticks}</svg>'

    vwc = chart(
        "VWC (%)",
        {
            **{f"S{i}": [row[f"vwc_s{i}"] for row in rows] for i in range(1, 4)},
            "Validierter Mittelwert": [row["vwc_average"] for row in rows],
        },
        100,
        "Skala 0–100 %. Lücken zeigen fehlende oder ungültige Werte.",
    )
    phases = {"p0_transpiration": 0, "p1_morning": 1, "p2_midday": 2, "p3_dryback": 3}
    phase = chart(
        "Native Phasen",
        {"Phase": [phases.get(row["phase"]) for row in rows]},
        3,
        "0 = P0, 1 = P1, 2 = P2, 3 = P3",
        step=True,
    )
    pump = chart(
        "Fake-Pumpe",
        {"Pumpenmeldung": [1 if row["pump_state"] == "on" else 0 for row in rows]},
        1,
        "0 = off, 1 = on. Pumpenberichte und Shot-Ergebnisse sind zusätzliche Messpunkte.",
        step=True,
    )
    events = "".join(
        f"<tr><td>{html.escape(event['timestamp'])}</td><td>{html.escape(event['kind'])}</td><td><pre>{html.escape(json.dumps({k: v for k, v in event.items() if k not in {'timestamp', 'sequence', 'elapsed_seconds', 'kind'}}, ensure_ascii=False))}</pre></td></tr>"
        for event in document["events"]
        if event["kind"] != "decision"
        and (event["kind"] != "active_safety_check" or event["blocking_reason"])
    )
    return f'<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Crop Steering Scenario Report</title><style>body{{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:1rem;color:#172033}}svg{{width:100%;background:#f8fafc}}td{{vertical-align:top;border-bottom:1px solid #ddd;padding:.4rem}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-width:700px}}h2{{margin-top:2rem}}</style><h1>{html.escape(document["scenario"]["name"])}</h1><p>24 Stunden native Testbeobachtung ab {html.escape(document["scenario"]["start"])} ({html.escape(document["scenario"]["timezone"])})</p><p>Das synthetische VWC-Modell ist ausschließlich eine Testannahme, kein reales Pflanzen- oder Substratmodell. Aktive Shots verwenden reale monotone Timer.</p><pre>{html.escape(json.dumps(document["summary"], indent=2))}</pre>{vwc}{phase}{pump}<h2>Ereignisse</h2><p>Alle Entscheidungen und Sicherheitsprüfungen stehen vollständig in events.json; Messpunkte in timeline.csv. Abgelehnte Requests und blockierte Bewertungen werden getrennt gezählt.</p><table><tr><th>Zeit</th><th>Ereignis</th><th>Beobachtung</th></tr>{events}</table></html>\n'
