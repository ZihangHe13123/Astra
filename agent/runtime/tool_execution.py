"""Execution outcome metadata, independent of text rendering or tool RPC success."""

from __future__ import annotations

from typing import Any


def execution_metadata(result: dict[str, Any]) -> dict[str, Any]:
    code = result.get("exit_code")
    code = code if type(code) is int else None
    state = result.get("status")
    if result.get("shell_reset"):
        state = "unknown"
    elif state in {"starting", "running"}:
        state, code = "running", None
    elif state not in {"cancelled", "interrupted", "timed_out", "unknown"}:
        state = "completed" if code is not None else "unknown"
    metadata = {"status": state, "exit_code": code}
    if result.get("process_id"):
        metadata["process_id"] = str(result["process_id"])
    return metadata


class ExecutionResult(str):
    """String-compatible result for existing tool and notebook consumers."""

    execution: dict[str, Any]

    def __new__(cls, text: str, result: dict[str, Any] | None = None):
        value = super().__new__(cls, text)
        value.execution = execution_metadata(result or {})
        return value


class ExecutionFailure(RuntimeError):
    def __init__(self, text: str, result: dict[str, Any]):
        super().__init__(text)
        self.execution = execution_metadata(result)


class PartialResult(str):
    """Successful text that the tool itself cut short.

    Use it when a tool returns one bounded page, a capped list or a clipped
    view and its text already says how to get the rest. The result envelope
    then stops describing the result as complete.
    """

    partial = True
