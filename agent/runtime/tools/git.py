"""git tools — status/log/diff/add/commit/push/pull/clone/reset/revert/checkout"""

import asyncio
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from ..process_env import hidden_process_creationflags
from .approval import ScopedApprovalStore
from .registry import ToolRegistry, ToolDef


_READ_ONLY_COMMANDS = {"status", "log", "diff", "show", "branch"}
_NETWORK_COMMANDS = {"push", "pull", "clone"}


def _git_timeout(args: tuple[str, ...]) -> int:
    command = args[0] if args else ""
    if command in _READ_ONLY_COMMANDS:
        return 15
    if command in _NETWORK_COMMANDS:
        return 180
    return 60


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a Git process and every descendant without using pipes."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=hidden_process_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, OSError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        pass


def _run_git_blocking(
    args: tuple[str, ...],
    cwd: str,
    timeout: int,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Run Git with file-backed output and a process-tree hard deadline."""
    proc: subprocess.Popen | None = None
    try:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if args and args[0] in _READ_ONLY_COMMANDS:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        popen_kwargs = {
            "cwd": str(Path(cwd).resolve()),
            "stdin": subprocess.DEVNULL,
            "env": env,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = hidden_process_creationflags(
                new_process_group=True,
            )
        else:
            popen_kwargs["start_new_session"] = True

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            proc = subprocess.Popen(
                ["git", *args],
                stdout=stdout_file,
                stderr=stderr_file,
                **popen_kwargs,
            )
            deadline = time.monotonic() + timeout
            terminal_error = ""
            while proc.poll() is None:
                if cancel_event is not None:
                    cancelled = cancel_event.wait(timeout=0.05)
                else:
                    time.sleep(0.05)
                    cancelled = False
                if cancelled:
                    terminal_error = "[Cancelled] git command was cancelled"
                    _terminate_process_tree(proc)
                    break
                if time.monotonic() >= deadline:
                    terminal_error = f"[Timeout] git command exceeded {timeout}s"
                    _terminate_process_tree(proc)
                    break

            if proc.poll() is None:
                _terminate_process_tree(proc)
            stdout_file.seek(0)
            stderr_file.seek(0)
            output = stdout_file.read().decode("utf-8", errors="replace").strip()
            error = stderr_file.read().decode("utf-8", errors="replace").strip()
            if terminal_error:
                return {"output": output, "error": terminal_error, "exit_code": -1}
            return {
                "output": output,
                "error": error,
                "exit_code": proc.returncode or 0,
            }
    except OSError as exc:
        if proc is not None and proc.poll() is None:
            _terminate_process_tree(proc)
        return {
            "output": "",
            "error": f"[GitExecutionError] {type(exc).__name__}: {exc}",
            "exit_code": -1,
        }


async def _run_git(*args, cwd: str = ".") -> dict:
    normalized_args = tuple(str(arg) for arg in args)
    cancel_event = threading.Event()
    execution = asyncio.create_task(asyncio.to_thread(
        _run_git_blocking,
        normalized_args,
        cwd,
        _git_timeout(normalized_args),
        cancel_event,
    ))
    try:
        return await execution
    except asyncio.CancelledError:
        cancel_event.set()
        raise


def register_git_tools(
    registry: ToolRegistry,
    workdir: str = ".",
    *,
    allow_sibling_paths: bool = True,
):
    """注册 12 个 Git 工具，可选择是否允许工作区同级路径。"""
    root = Path(workdir).resolve()
    approvals = ScopedApprovalStore(approved_scopes=registry.approved_permission_scopes)

    def _safe_path(path: str = ".") -> str:
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        resolved = p.resolve()
        # Always allow root descendants; ordinary registries may also allow siblings.
        if resolved == root or root in resolved.parents:
            return str(resolved)
        if allow_sibling_paths and (
            resolved.parent == root.parent or resolved.parent == root
        ):
            return str(resolved)
        raise ValueError(f"Path escapes workspace: {path}")

    def _git_scope(operation: str, path: str = ".") -> tuple[str, str]:
        target = _safe_path(path)
        return f"git-write:{operation}:{os.path.normcase(target)}", target

    def _require_git_write_enabled(operation: str, path: str = "."):
        scope, _target = _git_scope(operation, path)
        if os.getenv("AGENT_ALLOW_GIT_WRITE") != "1" and scope not in approvals.approved_scopes:
            raise PermissionError(
                f"{operation} modifies git state. Set AGENT_ALLOW_GIT_WRITE=1 to enable git write tools."
            )

    def _git_permission_check(operation: str):
        def check(args: dict) -> dict | None:
            scope, target = _git_scope(operation, str(args.get("path", ".")))
            if os.getenv("AGENT_ALLOW_GIT_WRITE") == "1":
                return None
            return approvals.request(
                scope=scope,
                kind="git_write",
                operation=operation,
                target=target,
                reason=f"{operation} wants to modify Git state",
                detail=(
                    f"{operation} modifies git state. "
                    "Set AGENT_ALLOW_GIT_WRITE=1 to enable git write tools."
                ),
            )
        return check

    def _git_permission_grant(args: dict, request: dict, decision: str):
        return approvals.grant(args, request, decision)

    # ── 只读 ──
    async def _git_status(path: str = ".") -> str:
        r = await _run_git("status", cwd=_safe_path(path))
        return r["output"] or r["error"] or "(empty status)"

    async def _git_log(path: str = ".", max_count: int = 10) -> str:
        r = await _run_git("log", f"--max-count={max_count}", "--oneline", "--graph", cwd=_safe_path(path))
        return r["output"] or r["error"] or "(no commits)"

    async def _git_diff(path: str = ".", target: str = "HEAD", files: list[str] | None = None) -> str:
        args = ["diff", target]
        if files:
            args.extend(["--", *files])
        r = await _run_git(*args, cwd=_safe_path(path))
        return r["output"] or r["error"] or "(no diff)"

    async def _git_show(commit: str = "HEAD", path: str = ".") -> str:
        r = await _run_git("show", commit, "--stat", "--no-patch", cwd=_safe_path(path))
        return r["output"] or r["error"] or "(no output)"

    async def _git_branch(path: str = ".") -> str:
        r = await _run_git("branch", "-a", cwd=_safe_path(path))
        return r["output"] or "(no branches)"

    # ── 写入（安全） ──
    async def _git_add(path: str = ".", files: str = ".") -> str:
        _require_git_write_enabled("git_add", path)
        r = await _run_git("add", *files.split(), cwd=_safe_path(path))
        return f"Staged: {files}" + (f"\n{r['error']}" if r["error"] else "")

    async def _git_commit(path: str = ".", message: str = "") -> str:
        _require_git_write_enabled("git_commit", path)
        if not message:
            return "Error: commit message is required"
        r = await _run_git("commit", "-m", message, cwd=_safe_path(path))
        return r["output"] or r["error"] or "(commit done)"

    async def _git_push(path: str = ".", remote: str = "origin", branch: str = "") -> str:
        _require_git_write_enabled("git_push", path)
        args = ["push", remote]
        if branch:
            args.append(branch)
        r = await _run_git(*args, cwd=_safe_path(path))
        return r["output"] or r["error"] or "(pushed)"

    async def _git_pull(path: str = ".", remote: str = "origin", branch: str = "") -> str:
        _require_git_write_enabled("git_pull", path)
        args = ["pull", remote]
        if branch:
            args.append(branch)
        r = await _run_git(*args, cwd=_safe_path(path))
        return r["output"] or r["error"] or "(pulled)"

    async def _git_clone(url: str, path: str = ".", branch: str = "") -> str:
        _require_git_write_enabled("git_clone", path)
        args = ["clone", url]
        if branch:
            args.extend(["-b", branch])
        args.append(_safe_path(path))
        r = await _run_git(*args, cwd=str(root))
        return r["output"] or r["error"] or f"Cloned {url}"

    # ── 危险（改错代码时救回） ──
    async def _git_checkout(path: str = ".", target: str = "", files: str = "") -> str:
        """撤销文件修改或切换分支。files='.' 恢复所有文件"""
        _require_git_write_enabled("git_checkout", path)
        args = ["checkout"]
        if target:
            args.append(target)
        if files:
            args.extend(files.split())
        r = await _run_git(*args, cwd=_safe_path(path))
        return r["output"] or r["error"] or "(checkout done)"

    async def _git_revert(path: str = ".", commit: str = "") -> str:
        """安全撤销一个 commit（创建反向 commit）"""
        _require_git_write_enabled("git_revert", path)
        if not commit:
            return "Error: commit hash is required"
        r = await _run_git("revert", "--no-edit", commit, cwd=_safe_path(path))
        return r["output"] or r["error"] or f"Reverted {commit}"

    async def _git_reset(path: str = ".", target: str = "HEAD", mode: str = "mixed") -> str:
        """重置 HEAD: soft(保留修改), mixed(取消暂存), hard(丢弃修改!)"""
        _require_git_write_enabled("git_reset", path)
        if mode not in ("soft", "mixed", "hard"):
            return f"Error: invalid mode '{mode}'. Use soft/mixed/hard"
        r = await _run_git("reset", f"--{mode}", target, cwd=_safe_path(path))
        return r["output"] or r["error"] or f"Reset {mode} to {target}"

    # 注册
    for name, fn, desc, extra_props in [
        ("git_status", _git_status, "Show working tree status", {}),
        ("git_log", _git_log, "Show commit history graph", {"max_count": {"type": "integer", "description": "Max commits"}}),
        ("git_show", _git_show, "Show details of a commit", {"commit": {"type": "string", "description": "Commit hash or ref (default: HEAD)"}}),
        ("git_branch", _git_branch, "List branches", {}),
        ("git_add", _git_add, "Stage file(s) for commit. Use '.' to stage all.", {"files": {"type": "string", "description": "Files to stage (default: '.')"}}),
        ("git_commit", _git_commit, "Commit staged changes with a message", {"message": {"type": "string", "description": "Commit message"}}),
        ("git_push", _git_push, "Push commits to remote", {"remote": {"type": "string", "description": "Remote name (default: origin)"}, "branch": {"type": "string", "description": "Branch (default: current)"}}),
        ("git_pull", _git_pull, "Pull from remote", {"remote": {"type": "string", "description": "Remote name (default: origin)"}, "branch": {"type": "string", "description": "Branch (default: current)"}}),
        ("git_clone", _git_clone, "Clone a repository", {"url": {"type": "string", "description": "Repository URL"}, "branch": {"type": "string", "description": "Branch (optional)"}}),
        ("git_checkout", _git_checkout, "⚠️ 撤销文件修改或切换分支. 改错代码时最常用的救回工具", {"target": {"type": "string", "description": "Branch/commit, or '--' for file restore"}, "files": {"type": "string", "description": "Files to restore"}}),
        ("git_revert", _git_revert, "⚠️ 创建一个新提交来撤销指定 commit 的更改（安全救回方式）", {"commit": {"type": "string", "description": "Commit hash to revert"}}),
        ("git_reset", _git_reset, "⚠️ 重置 HEAD. mode=soft(保留) mixed(取消暂存) hard(丢弃!)", {"target": {"type": "string", "description": "Ref to reset to (default: HEAD)"}, "mode": {"type": "string", "description": "soft/mixed/hard"}}),
    ]:
        props = {"path": {"type": "string", "description": "Git repo path (default: current dir)"}}
        props.update(extra_props)
        readonly = name in {"git_status", "git_log", "git_diff", "git_show", "git_branch"}
        registry.register(ToolDef(
            name=name, description=desc,
            parameters={"type": "object", "properties": props, "required": [k for k in props if k != "path"]},
            fn=fn, sandboxed=False,
            # _run_git owns the command-specific hard deadline and terminates
            # the whole process tree. Do not add a competing Registry timeout.
            timeout=None,
            risk="read" if readonly else "write",
            approval="never" if readonly else "on_risk",
            idempotent=readonly,
            group="git",
            # Rarely used; execute_shell covers them. AGENT_TOOL_EXPOSE brings them back.
            expose_by_default=name not in {"git_pull", "git_revert", "git_reset"},
            permission_check=None if readonly else _git_permission_check(name),
            permission_grant=None if readonly else _git_permission_grant,
        ))

    # git_diff with optional scoped files — registered outside the loop because
    # *files* must not appear in required (the loop auto-requires every extra_prop).
    registry.register(ToolDef(
        name="git_diff",
        description="Show unstaged changes or diff against a ref",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Git repo path (default: current dir)"},
                "target": {"type": "string", "description": "Git ref (default: HEAD)"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Optional file paths to scope the diff"},
            },
            "required": [],
        },
        fn=_git_diff, sandboxed=False, timeout=None,
        risk="read", approval="never", idempotent=True, group="git",
    ))
