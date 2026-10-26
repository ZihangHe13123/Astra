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

from agent.runtime.jobs.calendar import quiet_hours, set_quiet_hours, zone
from agent.runtime.jobs.store import NOTIFY_POLICIES, PUSH_TARGETS, JobStore, finite
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
    schedule.add_argument("--time", help="local time of day, HH:MM on a 24-hour clock; repeats on --days")
    add.add_argument("--every", type=float, default=0, help="fixed interval in seconds, at least 60")
    add.add_argument("--days", default="", help="with --time: weekdays such as mon,wed,fri (default every day)")
    add.add_argument("--tz", default="", help="with --time: IANA time zone (default this computer's)")
    add.add_argument("--notify", choices=sorted(NOTIFY_POLICIES), default="always",
                     help="on_change: deliver a check's result only when it reports something new")
    add.add_argument("--on-mail-from", default=None, help="start on new mail whose sender contains this")
    add.add_argument("--on-mail-subject", default=None, help="start on new mail whose subject contains this")
    add.add_argument("--on-mail-folder", default="INBOX", help="with --on-mail-*: the folder to watch")
    add.add_argument("--on-new-file", metavar="PATTERN", default=None,
                     help="start when a new file matching PATTERN (such as '*.pdf') appears in --workdir")
    add.add_argument("--targets", default=",".join(PUSH_TARGETS),
                     help="where results are pushed besides the inbox: desktop,weixin (empty for the inbox only)")
    add.add_argument("--catch-up", type=float, help="maximum lateness in seconds, from 1 to 86400")
    add.add_argument("--prompt", required=True)
    add.add_argument("--request", help="original user authorization; required for agent checks")
    add.add_argument("--reminder", action="store_true", help="deliver the text without using a model")
    add.add_argument("--workdir", type=Path)
    add.add_argument("--model", default="", help="configured model key; captured when the job is created")
    add.add_argument("--web", action="store_true", help="allow existing web search/fetch tools for this check")
    add.add_argument("--mail", action="store_true", help="let this check read the configured mailbox (read-only)")
    add.add_argument("--read-calendar", action="store_true",
                     help="let this check read Calendar and Reminders on macOS (read-only)")
    add.add_argument("--idle", type=float, default=120)
    add.add_argument("--timeout", type=float, default=None,
                     help="hard limit in seconds per run (default 2700 with --web, else 900)")
    add.add_argument("--iterations", type=int, default=None,
                     help="most model rounds per run, 1 to 50 (default 30 with --web, else 10)")
    update = commands.add_parser("update", help="change a check's rounds or time limit for its later runs")
    update.add_argument("id")
    update.add_argument("--iterations", type=int, help="most model rounds per run, 1 to 50")
    update.add_argument("--timeout", type=float, help="hard limit in seconds per run")
    update.add_argument("--request", default="", help="why it changed, kept with the job's authorization")
    commands.add_parser("list", help="show jobs and their schedules")
    for action in ("show", "pause", "resume", "remove", "run"):
        item = commands.add_parser(action)
        item.add_argument("id")
        if action == "run":
            item.add_argument("--queue", action="store_true",
                              help="leave the extra run to the running scheduler instead of running it here")
    history = commands.add_parser("history", help="show occurrence outcomes, including missed slots")
    history.add_argument("id", nargs="?", default="")
    history.add_argument("--limit", type=int, default=50)
    inbox = commands.add_parser("inbox", help="read results and delivery receipts")
    inbox.add_argument("--read", default="", help="acknowledge one result by occurrence ID")
    inbox.add_argument("--limit", type=int, default=50)
    quiet = commands.add_parser("quiet", help="show or set the hours when results wait instead of being pushed")
    quiet.add_argument("hours", nargs="*", help="START END as HH:MM (default 23:00 08:00), or 'off'")
    commands.add_parser("tick", help="recover receipts and execute currently due jobs once")
    serve = commands.add_parser("serve", help="foreground standalone scheduler; stop with Ctrl+C")
    serve.add_argument("--poll", type=float, default=5)
    return parser


def job_model_key(selected: str) -> str:
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


def _with_times(job: dict) -> None:
    if job["spec"].get("trigger"):
        # An event starts it; its stored time is a far-future sentinel that Windows cannot even format.
        job["next_at_utc"] = None
        return
    job["next_at_utc"] = datetime.fromtimestamp(job["next_at"], timezone.utc).isoformat()
    calendar = job["spec"].get("calendar")
    if calendar:
        job["next_at_local"] = datetime.fromtimestamp(job["next_at"], zone(calendar["tz"])).isoformat()


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
        if args.action == "run" and args.queue:
            return json.dumps(JobStore(home if home is not None else state_dir()).request_run(args.id),
                              ensure_ascii=False, indent=2), ""
        if args.action in {"run", "tick", "serve"} and not allow_execution:
            raise ValueError("Start execution with 'astra jobs run ID', 'astra jobs tick', or 'astra jobs serve' "
                             "in a separate terminal. Job checks never run inside the active conversation.")
        store = JobStore(home if home is not None else state_dir())
        if args.action == "add":
            period = finite(args.every, 0, 365 * 86400, "Interval")
            now = time.time()
            calendar = None
            next_at: float | None = None
            trigger = None
            if args.on_mail_from is not None or args.on_mail_subject is not None:
                trigger = {"type": "mail", "from": args.on_mail_from or "", "subject": args.on_mail_subject or "",
                           "folder": args.on_mail_folder}
            elif args.on_new_file is not None:
                trigger = {"type": "file", "pattern": args.on_new_file}
            if trigger and (args.time or args.at or args.after is not None or period):
                raise ValueError("A job starts on a schedule or on an event, not both.")
            if trigger is None:
                if args.time:
                    if period:
                        raise ValueError("Choose --every or --time, not both.")
                    calendar = {"time": args.time, "days": [day for day in args.days.split(",") if day.strip()],
                                "tz": args.tz}
                elif args.days or args.tz:
                    raise ValueError("--days and --tz apply to --time.")
                elif args.at:
                    instant = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
                    if instant.tzinfo is None:
                        raise ValueError("--at needs an explicit UTC Z or numeric timezone offset.")
                    next_at = instant.timestamp()
                elif args.after is not None or period:
                    delay = finite(args.after if args.after is not None else period, 60, 365 * 86400, "Delay")
                    next_at = now + delay
                else:
                    raise ValueError("Choose --at, --after, --every or --time.")
            if not args.reminder and not args.request:
                raise ValueError("Agent checks require --request with the original user authorization.")
            model = args.model
            if not args.reminder:
                model = job_model_key(model)
            value = store.add(name=args.name, prompt=args.prompt, original_request=args.request or args.prompt,
                              workdir=args.workdir or workdir or Path.cwd(), next_at=next_at,
                              interval_seconds=period, calendar=calendar, trigger=trigger, notify=args.notify,
                              targets=[target for target in args.targets.split(",") if target.strip()],
                              grace_seconds=args.catch_up if args.catch_up is not None
                              else 3600 if calendar else min(period / 2, 3600) if period else 300,
                              kind="reminder" if args.reminder else "check", model=model, web=args.web, mail=args.mail,
                              read_calendar=args.read_calendar,
                              idle_seconds=args.idle, timeout_seconds=args.timeout, max_iterations=args.iterations)
            _with_times(value)
        elif args.action == "update":
            if args.iterations is None and args.timeout is None:
                raise ValueError("Give --iterations or --timeout.")
            value = store.update(args.id, request=args.request.strip() or "Changed from the command line.",
                                 max_iterations=args.iterations, timeout_seconds=args.timeout)
            _with_times(value)
        elif args.action == "list":
            value = store.jobs()
            for job in value:
                _with_times(job)
        elif args.action == "show":
            value = {"job": store.job(args.id), "occurrences": store.history(args.id)}
        elif args.action in {"pause", "resume", "remove"}:
            value = store.set_state(args.id, {"pause": "paused", "resume": "enabled", "remove": "removed"}[args.action])
        elif args.action == "quiet":
            if args.hours == ["off"]:
                value = {"quiet_hours": set_quiet_hours(store.home, None)}
            elif len(args.hours) == 2:
                value = {"quiet_hours": set_quiet_hours(store.home, {"start": args.hours[0], "end": args.hours[1]})}
            elif not args.hours:
                value = {"quiet_hours": quiet_hours(store.home)}
            else:
                raise ValueError("Give START END, or 'off'.")
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
