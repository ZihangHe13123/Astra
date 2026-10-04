"""One portable command adapter for launcher, text CLI, TUI and GUI."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from agent.runtime.jobs.store import JobStore, finite
from agent.runtime.paths import state_dir


class JobParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def jobs_parser() -> JobParser:
    parser = JobParser(prog="astra jobs", description="Persistent reminders and bounded read-only agent checks.",
                       epilog="Run 'astra jobs serve' separately, or invoke 'astra jobs tick' from your OS scheduler. "
                       "Results go to the local inbox; unknown executions are never retried automatically.")
    commands = parser.add_subparsers(dest="action")
    add = commands.add_parser("add", help="create an explicitly authorized job")
    add.add_argument("name")
    schedule = add.add_mutually_exclusive_group()
    schedule.add_argument("--at", help="one-shot or first UTC/offset ISO timestamp")
    schedule.add_argument("--after", type=float, help="first run after at least 60 seconds")
    add.add_argument("--every", type=float, default=0, help="fixed interval in seconds, at least 60")
    add.add_argument("--catch-up", type=float, help="maximum lateness in seconds, from 1 to 86400")
    add.add_argument("--prompt", required=True)
    add.add_argument("--request", help="original user authorization; required for agent checks")
    add.add_argument("--reminder", action="store_true", help="deliver the text without using a model")
    add.add_argument("--workdir", type=Path)
    add.add_argument("--model", default="", help="configured model key; captured when the job is created")
    add.add_argument("--web", action="store_true", help="allow existing web search/fetch tools for this check")
    add.add_argument("--idle", type=float, default=120)
    add.add_argument("--timeout", type=float, default=900)
    add.add_argument("--iterations", type=int, default=10)
    commands.add_parser("list", help="show jobs and their schedules")
    for action in ("show", "pause", "resume", "remove", "run"):
        item = commands.add_parser(action)
        item.add_argument("id")
    history = commands.add_parser("history", help="show occurrence outcomes, including missed slots")
    history.add_argument("id", nargs="?", default="")
    history.add_argument("--limit", type=int, default=50)
    inbox = commands.add_parser("inbox", help="read results and delivery receipts")
    inbox.add_argument("--read", default="", help="acknowledge one result by occurrence ID")
    inbox.add_argument("--limit", type=int, default=50)
    commands.add_parser("tick", help="recover receipts and execute currently due jobs once")
    serve = commands.add_parser("serve", help="foreground standalone scheduler; stop with Ctrl+C")
    serve.add_argument("--poll", type=float, default=5)
    return parser


def _model_key(selected: str) -> str:
    from agent.cli.model_catalog import configured_model_catalog
    from agent.cli.model_preferences import read_selected_model
    from agent.cli.models import model_profiles

    catalog = configured_model_catalog()
    value = selected or read_selected_model() or os.getenv("LLM_MODEL", "deepseek-flash")
    entry = catalog.resolve_persisted(value)
    if entry is None:
        alias = model_profiles().get(value)
        matches = [item for item in catalog.entries if alias is not None
                   and item.model_id == (alias.model_id or value)
                   and item.base_url.rstrip("/") == alias.base_url.rstrip("/")]
        entry = matches[0] if len(matches) == 1 else None
    if entry is None:
        raise ValueError("Unknown configured model. Choose an explicit --model key.")
    return entry.key


def execute_jobs_command(argv: list[str], *, home: Path | None = None,
                         workdir: Path | None = None, allow_execution: bool = False) -> tuple[str, str]:
    parser = jobs_parser()
    if not argv or argv in (["help"], ["--help"], ["-h"]):
        return parser.format_help(), ""
    if "--help" in argv or "-h" in argv:
        # argparse's normal help exits the process; a GUI command must not do so.
        subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        child = subcommands.choices.get(argv[0])
        return (child or parser).format_help(), ""
    try:
        args = parser.parse_args(argv)
        if args.action in {"run", "tick", "serve"} and not allow_execution:
            raise ValueError("Start execution with 'astra jobs run ID', 'astra jobs tick', or 'astra jobs serve' "
                             "in a separate terminal. Job checks never run inside the active conversation.")
        store = JobStore(home if home is not None else state_dir())
        if args.action == "add":
            period = finite(args.every, 0, 365 * 86400, "Interval")
            now = time.time()
            if args.at:
                instant = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError("--at needs an explicit UTC Z or numeric timezone offset.")
                next_at = instant.timestamp()
            elif args.after is not None or period:
                delay = finite(args.after if args.after is not None else period, 60, 365 * 86400, "Delay")
                next_at = now + delay
            else:
                raise ValueError("Choose --at, --after or --every.")
            if not args.reminder and not args.request:
                raise ValueError("Agent checks require --request with the original user authorization.")
            model = args.model
            if not args.reminder:
                model = _model_key(model)
            value = store.add(name=args.name, prompt=args.prompt, original_request=args.request or args.prompt,
                              workdir=args.workdir or workdir or Path.cwd(), next_at=next_at,
                              interval_seconds=period,
                              grace_seconds=args.catch_up if args.catch_up is not None else min(period / 2, 3600) if period else 300,
                              kind="reminder" if args.reminder else "check", model=model, web=args.web,
                              idle_seconds=args.idle, timeout_seconds=args.timeout, max_iterations=args.iterations)
            value["next_at_utc"] = datetime.fromtimestamp(value["next_at"], timezone.utc).isoformat()
        elif args.action == "list":
            value = store.jobs()
            for job in value:
                job["next_at_utc"] = datetime.fromtimestamp(job["next_at"], timezone.utc).isoformat()
        elif args.action == "show":
            value = {"job": store.job(args.id), "occurrences": store.history(args.id)}
        elif args.action in {"pause", "resume", "remove"}:
            value = store.set_state(args.id, {"pause": "paused", "resume": "enabled", "remove": "removed"}[args.action])
        elif args.action == "history":
            value = store.history(args.id, limit=args.limit)
        elif args.action == "inbox":
            if args.read:
                store.mark_read(args.read)
            value = store.inbox(limit=args.limit)
        else:
            from agent.runtime.jobs.scheduler import tick

            if args.action == "serve":
                poll = finite(args.poll, 1, 60, "Poll interval")
                print(json.dumps({"scheduler": "serving", "home": str(store.home)}, ensure_ascii=False), flush=True)
                while True:
                    result = tick(store)
                    if result["executed"] or result["recovered_unknown"] or result["delivered"]:
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                    time.sleep(poll)
            value = tick(store, manual_job=args.id if args.action == "run" else "")
        return json.dumps(value, ensure_ascii=False, indent=2), ""
    except (ValueError, OSError, sqlite3.Error) as exc:
        return "", str(exc) if isinstance(exc, ValueError) else f"Job operation failed: {type(exc).__name__}"


def main(argv: list[str] | None = None) -> int:
    from agent.launcher.installation import discover
    from agent.launcher.locking import RuntimeLease

    # Capture the selected home before loading optional model/provider modules.
    home = state_dir()
    arguments = sys.argv[1:] if argv is None else argv
    with RuntimeLease(discover(), "jobs scheduler" if arguments[:1] == ["serve"] else "jobs command"):
        if (home / ".env").is_file():
            os.environ["ASTRA_ENV_FILE"] = str(home / ".env")
        from agent.cli.environment import load_project_env

        load_project_env(Path(__file__).resolve().parents[2])
        os.environ["ASTRA_HOME"] = str(home)
        try:
            output, error = execute_jobs_command(arguments, home=home,
                                                  workdir=Path(os.getenv("ASTRA_WORKSPACE", str(Path.cwd()))),
                                                  allow_execution=True)
        except KeyboardInterrupt:
            return 130
        print(error or output, file=sys.stderr if error else sys.stdout)
        return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
