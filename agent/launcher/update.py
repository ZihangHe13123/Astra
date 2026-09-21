"""Fast-forward source updates. Package-manager installations keep their owner."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path

from . import dependencies
from .common import LauncherError, git, run
from .installation import Installation
from .locking import exclusive, require_idle
from .local_changes import LocalChanges
from .services import ServiceMaintenance, pending_services
from .transaction import Transaction, receipt


def require_source(install: Installation) -> None:
    if install.kind != "source":
        raise LauncherError(f"This installation belongs to {install.owner}. Reinstall/upgrade with its original "
                            "wheel or package manager. Source updates cannot modify this installation.")
    if not (install.root / ".git").exists():
        raise LauncherError("This is a source archive without Git history. Use a Git clone for source updates.")
    if Path(git(install.root, "rev-parse", "--show-toplevel")).resolve() != install.root:
        raise LauncherError("The Astra directory is not the Git checkout root.")


def current_branch(install: Installation) -> str:
    try:
        return git(install.root, "symbolic-ref", "--short", "HEAD")
    except LauncherError as exc:
        raise LauncherError("Detached HEAD: check out the intended tracking branch before updating.") from exc


def check_clean(install: Installation) -> str:
    changed = git(install.root, "status", "--porcelain", "--untracked-files=no")
    if changed:
        raise LauncherError("Local tracked changes were preserved. Commit or move them before updating:\n" + changed)
    return git(install.root, "rev-parse", "HEAD")


def configured_target(install: Installation) -> tuple[str, str]:
    branch = current_branch(install)
    config = install.metadata
    selected = config.get("branch", branch)
    if selected != branch:
        raise LauncherError(f"Update channel is {selected}; checkout is on {branch}. No branch was changed.")

    def setting(name: str) -> str:
        try:
            return git(install.root, "config", "--get", name)
        except LauncherError:
            return ""

    remote = config.get("remote") or setting(f"branch.{branch}.remote")
    ref = config.get("ref") or setting(f"branch.{branch}.merge")
    if not remote or not ref:
        raise LauncherError("No tracking upstream. Configure this branch's Git upstream before updating.")
    if remote.startswith("-") or not ref.startswith("refs/heads/"):
        raise LauncherError("Unsupported upstream configuration. Use a named remote and a branch upstream.")
    git(install.root, "check-ref-format", ref)
    return remote, ref


def fetch_target(install: Installation) -> tuple[str, str]:
    remote, ref = configured_target(install)
    temporary_ref = "refs/astra/updates/" + uuid.uuid4().hex
    try:
        git(install.root, "fetch", "--no-tags", "--no-write-fetch-head", "--", remote, f"{ref}:{temporary_ref}")
        target = git(install.root, "rev-parse", "--verify", f"{temporary_ref}^{{commit}}")
        return target, f"{remote}/{ref.removeprefix('refs/heads/')}"
    finally:
        git(install.root, "update-ref", "-d", temporary_ref)


def require_fast_forward(install: Installation, before: str, target: str) -> None:
    try:
        git(install.root, "merge-base", "--is-ancestor", before, target)
    except LauncherError as exc:
        raise LauncherError("Local commits are ahead of or diverge from the upstream. No reset, rebase or branch switch was performed.") from exc


def legacy_processes(install: Installation) -> list[str]:
    """Catch pre-launcher processes and other users of this installation's venv.

    Leases provide race-free exclusion for new Astra entrypoints. This bounded OS
    scan is a migration safeguard, not an excuse to terminate unrelated processes.
    """
    if os.name == "nt":
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"]
        import json
        try:
            entries = json.loads(run(command, cwd=install.root, timeout=30) or "[]")
            if isinstance(entries, dict):
                entries = [entries]
            rows = [(str(row.get("ProcessId")), str(row.get("ExecutablePath") or "") + " "
                     + str(row.get("CommandLine") or "")) for row in entries]
        except (LauncherError, ValueError) as exc:
            raise LauncherError("Cannot inspect Windows process ownership. Run astra doctor and retry after closing Astra.") from exc
    else:
        output = run(["ps", "-axo", "pid=,command="], cwd=install.root, timeout=20)
        rows = [tuple(line.strip().split(maxsplit=1)) for line in output.splitlines() if len(line.strip().split(maxsplit=1)) == 2]
    # Include nested worktrees: they may use this checkout's venv through a
    # symlink even though their command line spells the worktree's path.
    needle = os.path.normcase(str(install.root)) + os.sep
    holders = []
    for pid, command in rows:
        if pid in {str(os.getpid()), str(os.getppid())}:
            continue
        normalized = os.path.normcase(command)
        runtime_path = any(os.sep + name + os.sep in normalized for name in (".venv", "ui-tui", "ui-gui"))
        if needle in normalized and runtime_path:
            # Report identities only; process command lines can contain credentials.
            module = re.search(r"(?:^|\s)-m\s+([a-zA-Z0-9_.]+)", command)
            roles = {"agent.runtime.activity_recorder.browser_bridge": "Astra browser recorder (background service)",
                     "agent.cli.backend": "Astra session backend", "agent.cli.api_server": "Astra API server"}
            role = roles.get(module[1], "Astra Python process") if module else "Astra interface/runtime process"
            holders.append(f"PID {pid}: {role}; uses this installation")
    return holders


def preflight(install: Installation, *, allowed_pids: set[int] | None = None) -> None:
    require_idle(install)
    dependencies.check_environment_ownership(install)
    holders = legacy_processes(install)
    if allowed_pids:
        holders = [line for line in holders if not any(line.startswith(f"PID {pid}:") for pid in allowed_pids)]
    if holders:
        raise LauncherError("Close processes using this installation before updating:\n" + "\n".join(holders))


def environment_plan(install: Installation, before: str, target: str, repair: bool,
                     local_files: list[str] | None = None) -> dict[str, bool]:
    if repair or not dependencies.is_ready(install) or (dependencies.gui_enabled(install) and not dependencies.gui_ready(install)):
        return {"python": True, "node": True, "build": True}
    changed = set(git(install.root, "diff", "--name-only", before, target).splitlines())
    changed.update(local_files or [])
    python = bool(changed & {"pyproject.toml", "uv.lock"})
    interfaces = ["ui-tui", "ui-core"] + (["ui-gui"] if dependencies.gui_enabled(install) else [])
    node = bool(changed & {f"{ui}/{name}" for ui in interfaces for name in ("package.json", "package-lock.json")})
    build = node or any(name.startswith(tuple(f"{ui}/" for ui in interfaces)) for name in changed)
    return {"python": python, "node": node, "build": build}


def update_source(install: Installation, *, check: bool = False, recover: bool = False, repair: bool = False,
                  local_policy: str = "ask", choose_local: Callable[[dict], str] | None = None) -> dict:
    if local_policy not in {"ask", "keep", "overwrite"}:
        raise LauncherError("Unknown local-change policy.")
    if not recover:
        require_source(install)
    elif install.kind != "source":
        raise LauncherError("Recovery belongs to the original package installer.")
    with exclusive(install, allow_pending=recover):
        if recover:
            services = ServiceMaintenance(install, recovering=True)
            if (install.control / "pending.json").exists():
                current = ServiceMaintenance(install)
                preflight(install, allowed_pids=current.allowed_pids)
                with services.suspended():
                    preflight(install)
                    result = Transaction.load(install).recover()
            elif pending_services(install):
                require_idle(install)
                dependencies.check_environment_ownership(install)
                services.restore()
                result = {"outcome": "services_recovered", "installation": str(install.root)}
            else:
                raise LauncherError("No interrupted update to recover.")
            return services.finish(result)
        before = git(install.root, "rev-parse", "HEAD")
        target, channel = (before, "current checkout") if repair else fetch_target(install)
        local = LocalChanges(install, before, target)
        if check:
            services = ServiceMaintenance(install)
            return {"outcome": "available" if before != target else "current", "before": before, "target": target,
                    "channel": channel, "local_changes": bool(local.paths), "local_details": local.summary(),
                    "process_holders": legacy_processes(install), "installation": str(install.root),
                    "managed_services": services.summary()}
        if before == target and not repair and dependencies.is_ready(install) and (
                not dependencies.gui_enabled(install) or dependencies.gui_ready(install)):
            try:
                dependencies.python_health(install)
                dependencies.node_health(install)
                if dependencies.gui_enabled(install):
                    dependencies.gui_health(install)
            except LauncherError:
                repair = True
            else:
                return {"outcome": "current", "before": before, "target": target,
                        "local_changes": bool(local.paths), "installation": str(install.root)}
        require_fast_forward(install, before, target)
        services = ServiceMaintenance(install)
        blockers = []
        try:
            preflight(install, allowed_pids=services.allowed_pids)
        except LauncherError as exc:
            blockers.append(str(exc))
        preview = {"before": before, "target": target, "local_details": local.summary(), "blockers": blockers}
        if blockers:
            raise LauncherError("\n".join(blockers) + "\nLocal files: " + repr(local.paths)
                                + "\nOverlapping incoming files: " + repr(local.conflicts))
        policy = local_policy
        if repair and local.paths:
            raise LauncherError("Local tracked changes were preserved. Use astra setup --repair for edited source metadata.")
        if policy == "ask":
            if local.conflicts:
                if choose_local is None:
                    raise LauncherError("Local tracked changes overlap incoming files. Choose --keep-local or --overwrite-local; "
                                        "files were preserved: " + repr(local.conflicts))
                policy = choose_local(preview)
                if policy == "cancel":
                    return {"outcome": "cancelled", "before": before, "target": target, "local_changes": bool(local.paths)}
                if policy not in {"keep", "overwrite"}:
                    raise LauncherError("No local-file choice was made; nothing was updated.")
            else:
                policy = "keep"
        local.unchanged()
        if not repair and dependencies.is_ready(install) and (
                not dependencies.gui_enabled(install) or dependencies.gui_ready(install)):
            try:
                dependencies.python_health(install)
                dependencies.node_health(install)
                if dependencies.gui_enabled(install):
                    dependencies.gui_health(install)
            except LauncherError:
                repair = True
            else:
                if before == target:
                    return receipt(install, outcome="current", before=before, target=target)
        # Resolve required tooling before moving any environment or source file.
        dependencies.uv_command(install)
        dependencies.check_node(install.root)
        extras = dependencies.enabled_extras(install)
        plan = environment_plan(install, before, target, repair, local.paths)
        # Include the shared package on upgrades that introduce it, before it
        # exists in the old checkout. Disabled GUI stays optional.
        interfaces = ["ui-tui", "ui-core"] + (["ui-gui"] if dependencies.gui_enabled(install) else [])
        generated = tuple(([".venv"] if plan["python"] else []) + [f"{ui}/{folder}"
                          for ui in interfaces for folder, selected in (("node_modules", plan["node"]), ("dist", plan["build"])) if selected])
        transaction = Transaction(install, before, target, generated=generated)
        if local.paths:
            transaction.attach_local(local, policy)
        with services.suspended():
            preflight(install)
            result = _apply_transaction(install, transaction, before, target, local, extras, plan)
        return services.finish(result)


def _apply_transaction(install: Installation, transaction: Transaction, before: str, target: str,
                       local: LocalChanges, extras: list[str], plan: dict[str, bool]) -> dict:
    try:
        transaction.prepare()
        transaction.clear_local()
        if before != target:
            if check_clean(install) != before:
                raise LauncherError("Checkout changed during update preparation.")
            git(install.root, "-c", f"core.hooksPath={os.devnull}", "merge", "--ff-only", "--no-edit", target)
        transaction.save("code_updated")
        transaction.apply_local()
        dependencies.synchronize(install, extras, **plan)
        dependencies.validate(install)
        if git(install.root, "rev-parse", "HEAD") != target:
            raise LauncherError("HEAD changed during update verification. The external commit was preserved.")
        if local.paths:
            local.require_owned()
        dependencies.record_environment(install, extras)
        return transaction.commit()
    except BaseException as exc:
        if (install.control / "pending.json").exists():
            try:
                recovered = transaction.recover()
                if recovered["outcome"] == "applied":
                    return recovered
            except BaseException as recovery_error:
                raise LauncherError("Update did not complete and recovery needs attention. Run astra update --recover. "
                                    f"Recovery: {recovery_error}") from exc
        if isinstance(exc, KeyboardInterrupt):
            raise LauncherError("Update cancelled; the previous installation was restored.") from exc
        raise LauncherError(f"Update failed; the previous installation was restored. {exc}") from exc
