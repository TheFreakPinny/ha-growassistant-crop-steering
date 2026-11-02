"""Local entry routing only; never evidence of hardware exclusivity."""

from enum import StrEnum

from homeassistant.exceptions import HomeAssistantError


class ControlRoute(StrEnum):
    LEGACY = "LEGACY"
    NATIVE_SIMULATED = "NATIVE_SIMULATED"
    BLOCKED = "BLOCKED"


def control_route(entry):
    from .native_runtime import NativeRuntimeState, native_runtime

    runtime = native_runtime(entry)
    if runtime is None or runtime.state == NativeRuntimeState.CLOSED:
        return ControlRoute.LEGACY
    if runtime.state == NativeRuntimeState.READY_STOPPED and not runtime.error:
        return ControlRoute.NATIVE_SIMULATED
    return ControlRoute.BLOCKED


def require_legacy(entry, action):
    if control_route(entry) != ControlRoute.LEGACY:
        from .native_runtime import native_runtime

        runtime = native_runtime(entry)
        reason = (
            runtime.error
            or runtime.cycle.recovery_reason
            or "native cycle owns accounting"
        )
        raise HomeAssistantError(
            f"{action} blocked: {reason}; native owner controls cycle flags, counters and history"
        )
