"""Notebook execution reuses code-tool approval and background lifecycle."""
from pathlib import Path

from .registry import ToolDef, approval_justification_schema


def notebook_code(args):
    config = {
        "path": args["path"], "output_path": args.get("output_path", ""),
        "start_cell": args.get("start_cell", 1), "end_cell": args.get("end_cell", 0),
        "skip_cells": args.get("skip_cells", []), "cell_timeout": args.get("cell_timeout", 600),
        "kernel_name": args.get("kernel_name", ""),
    }
    worker = Path(__file__).resolve().parent.parent / "notebook_worker.py"
    return worker.read_text(encoding="utf-8") + "\nrun_notebook(" + repr(config) + ")\n"


def register_notebook_tools(registry, execute_python, permission_check, permission_grant):
    async def execute(path, output_path="", start_cell=1, end_cell=0, skip_cells=None,
                      cell_timeout=600, kernel_name="", foreground_yield_ms=10000,
                      background=False, _task_id=""):
        args = {"path": path, "output_path": output_path, "start_cell": start_cell, "end_cell": end_cell,
                    "skip_cells": skip_cells or [], "cell_timeout": cell_timeout, "kernel_name": kernel_name}
        return await execute_python(notebook_code(args), foreground_yield_ms=foreground_yield_ms,
                                    background=background, _task_id=_task_id)

    def permission(args):
        request = permission_check({**args, "code": notebook_code(args)})
        if request:
            request.update(approval_title="执行 Notebook",
                           approval_summary=f"执行 {args['path']} 的指定 cell",
                           approval_effect="Notebook 代码会在所选环境运行；新文件保留输出，原文件不改。",
                           arguments={k: v for k, v in args.items() if not k.startswith("_")})
        return request

    registry.register(ToolDef(
        name="notebook_execute",
        description=("Execute an .ipynb in a fresh Jupyter kernel using the selected sandbox. "
                     "Cell indices count ALL cells, 1-based inclusive; end_cell=0 means last cell. "
                     "Earlier cells are NOT automatically run; start from 1 for dependencies. "
                     "Writes a NEW notebook, clears stale outputs in that copy, preserves partial "
                     "results on failure and reports each cell. Existing output files are refused. "
                     "Requires nbclient/nbformat and the selected kernel; never installs dependencies. "
                     "Use process_poll/process_read/process_cancel for returned background process_id. "
                     "kernel_name selects an installed kernelspec; it is not a Python executable path."),
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "output_path": {"type": "string", "default": ""},
            "start_cell": {"type": "integer", "minimum": 1, "default": 1},
            "end_cell": {"type": "integer", "minimum": 0, "default": 0},
            "skip_cells": {"type": "array", "items": {"type": "integer", "minimum": 1}, "default": []},
            "cell_timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 600},
            "kernel_name": {"type": "string", "default": ""},
            "foreground_yield_ms": {
                "type": "integer", "minimum": 0, "maximum": 90000, "default": 10000,
                "description": (
                    "How long to wait for the run, in milliseconds, before returning a process_id "
                    "and letting it continue. 0 does not yield: the run stays in the foreground "
                    "and is stopped at the sandbox time limit. On the host sandbox a positive "
                    "value or background=true runs without that limit; the Docker sandbox "
                    "applies it to every run."
                ),
            },
            "background": {
                "type": "boolean", "default": False,
                "description": "Return immediately with a process_id.",
            },
            **approval_justification_schema(),
        }, "required": ["path"]},
        fn=execute, sandboxed=True, risk="execute", approval="on_risk", group="code",
        timeout=None, permission_check=permission, permission_grant=permission_grant,
        approval_justification=True,
        # Opt-in with AGENT_TOOL_EXPOSE=notebook_execute; notebooks otherwise run
        # through execute_shell with the kernel's own environment.
        expose_by_default=False,
    ))
