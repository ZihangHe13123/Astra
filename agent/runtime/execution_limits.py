"""Loaded execution limits shared by schemas, validation and diagnostics."""

COMMAND_FOREGROUND_MAX_MS = 90_000
DELEGATE_FOREGROUND_MAX_MS = 120_000
POLL_MAX_MS = 300_000
# One assignment's own time limit. A subagent has no turn or output budget, so this is
# what stops one that is stuck while nobody watches; it is not shared across assignments.
WORKER_TIMEOUT_MAX_SECONDS = 14_400
WORKER_TIMEOUT_DEFAULT_SECONDS = 1_800


def validate_wait_ms(value: int, *, name: str, maximum: int) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(
            f"{name} must be between 0 and {maximum} ms. "
            "For longer work, retain the returned process/team handle and wait again; "
            "this controls one foreground wait, not the job's total time limit."
        )


def execution_limits() -> dict[str, int]:
    return {
        "command_foreground_max_ms": COMMAND_FOREGROUND_MAX_MS,
        "delegate_foreground_max_ms": DELEGATE_FOREGROUND_MAX_MS,
        "poll_max_ms": POLL_MAX_MS,
        "worker_timeout_max_seconds": WORKER_TIMEOUT_MAX_SECONDS,
    }
