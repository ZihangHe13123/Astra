"""Scoped project and request snapshots for a durable worker conversation."""

from __future__ import annotations

import copy

from .project_instructions import ProjectInstructions
from .runtime_context_projection import RuntimeContextProjection


class WorkerGuidance:
    def __init__(self, workdir: str, saved_state: dict, *, durable: bool):
        self.durable = durable
        self.project = ProjectInstructions(workdir, snapshot=saved_state.get("project_guidance"))
        self.projection = RuntimeContextProjection(saved_state.get("runtime_context_projection")) if durable else None
        self._schemas = saved_state.get("tool_manifest")

    @property
    def system_suffix(self) -> str:
        return "\n\n" + self.project.base_prompt if self.project.base_prompt else ""

    def schemas(self, current: list[dict], *, finalizing: bool) -> list[dict]:
        if self.durable and not finalizing:
            if not isinstance(self._schemas, list):
                self._schemas = copy.deepcopy(current)
            return copy.deepcopy(self._schemas)
        return current

    def checkpoint(self) -> dict:
        return {"project_guidance": self.project.snapshot(),
                "runtime_context_projection": self.projection.state if self.projection is not None else {},
                "tool_manifest": self._schemas}

    def decorate_result(self, message: dict, output: str, saved_output: str, arguments: object) -> tuple[dict, str, str]:
        hints = self.project.discover_tool_call(arguments)
        if hints:
            suffix = "\n\n" + hints
            message["content"] += suffix
            output += suffix
            saved_output += suffix
        return message, output, saved_output
