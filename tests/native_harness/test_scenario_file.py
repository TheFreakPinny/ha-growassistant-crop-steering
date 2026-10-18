"""CLI entry through pytest's isolated HomeAssistant fixture."""

from pathlib import Path

from .scenario import Scenario, ScenarioRunner


async def test_scenario_file(request, scenario_harness, monkeypatch, tmp_path):
    path = request.config.getoption("--crop-scenario") or (
        Path(__file__).parent / "scenarios" / "normal-18-6.json"
    )
    scenario = Scenario.read(path)
    runner = ScenarioRunner(scenario_harness, scenario, monkeypatch)
    await runner.run()
    runner.write(request.config.getoption("--crop-output") or tmp_path)
    assert runner.elapsed == 86400
    assert not scenario_harness.shots.recovery_assessment.native_activation_allowed
