"""What the models cost: every request's tokens, per day, model, source and conversation, and the daily budget.

The model client (llm.py) reports each answered request here; a process records it only once it has installed
a ledger (the backend and the scheduled-job worker do). Prices are the person's own, per million tokens, in
settings.json (``model_prices``), so a cost appears only for models they priced. The budget
(``usage_budget``) is in tokens or in that money. The first request that takes a day over it is noted once, for
all processes; scheduled checks then wait until the next day, and conversations go on with a visible warning.
"""

from __future__ import annotations

import contextvars
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.runtime.paths import state_path

# What the code running now is doing, for the ledger: chat, channel, job, ...
_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar("astra_usage_source", default="chat")
_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("astra_usage_session", default="")
_LEDGER: UsageLedger | None = None
_LOCK = threading.Lock()


def usage_db_path() -> Path:
    import os

    configured = os.environ.get("ASTRA_USAGE_DB", "").strip()
    return Path(configured).expanduser() if configured else state_path("usage.sqlite3")


def set_usage_context(*, source: str | None = None, session: str | None = None) -> list[contextvars.Token]:
    tokens: list[contextvars.Token] = []
    if source is not None:
        tokens.append(_SOURCE.set(source))
    if session is not None:
        tokens.append(_SESSION.set(session))
    return tokens


def reset_usage_context(tokens: list[contextvars.Token]) -> None:
    for token in reversed(tokens):
        token.var.reset(token)


@contextmanager
def usage_context(*, source: str | None = None, session: str | None = None) -> Iterator[None]:
    """Attribute the model requests made inside to this source and conversation."""
    tokens = set_usage_context(source=source, session=session)
    try:
        yield
    finally:
        reset_usage_context(tokens)


def _count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _day(at: float) -> str:
    from agent.runtime.jobs.calendar import local_timezone_name, zone

    return datetime.fromtimestamp(at, zone(local_timezone_name())).date().isoformat()


class UsagePreferences:
    """Prices and the budget from settings.json, read again whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._data: dict[str, Any] = {}

    def _read(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._stamp, self._data = None, {}
            return {}
        if stamp != self._stamp:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._data = data if isinstance(data, dict) else {}
            self._stamp = stamp
        return self._data

    def prices(self) -> dict[str, dict[str, float]]:
        raw = self._read().get("model_prices")
        prices: dict[str, dict[str, float]] = {}
        if isinstance(raw, dict):
            for model, value in raw.items():
                if isinstance(value, dict):
                    prices[str(model)] = {key: float(value[key]) for key in ("input", "output", "cached_input")
                                          if isinstance(value.get(key), (int, float)) and value[key] >= 0}
        return prices

    def currency(self) -> str:
        value = self._read().get("price_currency")
        return value[:8] if isinstance(value, str) and value.strip() else "¥"

    def budget(self) -> dict[str, float]:
        raw = self._read().get("usage_budget")
        if not isinstance(raw, dict):
            return {}
        return {key: float(raw[key]) for key in ("daily_tokens", "daily_cost")
                if isinstance(raw.get(key), (int, float)) and not isinstance(raw[key], bool) and raw[key] > 0}


def request_cost(prices: dict[str, float], prompt: int, completion: int, cached: int) -> float | None:
    """The cost of one request's tokens at per-million prices, or None when the model has no price."""
    if not prices or "input" not in prices or "output" not in prices:
        return None
    fresh = max(0, prompt - cached)
    cached_price = prices.get("cached_input", prices["input"])
    return (fresh * prices["input"] + cached * cached_price + completion * prices["output"]) / 1_000_000


class UsageLedger:
    def __init__(self, path: Path | None = None, *, preferences: UsagePreferences | None = None):
        self.path = Path(path or usage_db_path())
        self.preferences = preferences
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Called once a day, in the process whose request first went over the budget.
        self.on_over_budget: Callable[[dict[str, Any]], None] | None = None
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL,
                    session TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    prompt INTEGER NOT NULL, completion INTEGER NOT NULL, cached INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_day ON requests(day);
                CREATE INDEX IF NOT EXISTS requests_session ON requests(session);
                CREATE TABLE IF NOT EXISTS over_budget (day TEXT PRIMARY KEY, at REAL NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def record(self, *, provider: str, model: str, usage: dict[str, Any], at: float | None = None,
               source: str | None = None, session: str | None = None) -> None:
        prompt, completion = _count(usage, "prompt_tokens"), _count(usage, "completion_tokens")
        if not prompt and not completion:
            return
        cached = min(prompt, _count(usage, "prompt_cache_hit_tokens"))
        at = time.time() if at is None else at
        day = _day(at)
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO requests(at, day, source, session, provider, model, prompt, completion, cached) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (at, day, source or _SOURCE.get(), (session if session is not None else _SESSION.get())[:200],
                        provider[:80], model[:200], prompt, completion, cached))
        summary = self.day(day)
        if summary["over_budget"] and self.on_over_budget is not None and self._first_over(day, at):
            self.on_over_budget(summary)

    def _first_over(self, day: str, at: float) -> bool:
        with closing(self._connect()) as db, db:
            return db.execute("INSERT OR IGNORE INTO over_budget(day, at) VALUES (?, ?)", (day, at)).rowcount == 1

    def day(self, day: str | None = None) -> dict[str, Any]:
        """One day's tokens and cost by model and source, and how it stands against the budget."""
        day = day or _day(time.time())
        prices = self.preferences.prices() if self.preferences else {}
        with closing(self._connect()) as db:
            rows = db.execute("SELECT model, source, SUM(prompt) p, SUM(completion) c, SUM(cached) h, COUNT(*) n "
                              "FROM requests WHERE day=? GROUP BY model, source", (day,)).fetchall()
        models: dict[str, dict[str, Any]] = {}
        sources: dict[str, int] = {}
        for row in rows:
            entry = models.setdefault(row["model"], {"model": row["model"], "prompt": 0, "completion": 0, "cached": 0,
                                                     "requests": 0, "cost": None})
            entry["prompt"] += row["p"]
            entry["completion"] += row["c"]
            entry["cached"] += row["h"]
            entry["requests"] += row["n"]
            sources[row["source"]] = sources.get(row["source"], 0) + row["p"] + row["c"]
        for entry in models.values():
            entry["cost"] = request_cost(prices.get(entry["model"], {}), entry["prompt"], entry["completion"],
                                         entry["cached"])
        tokens = sum(entry["prompt"] + entry["completion"] for entry in models.values())
        priced = [entry["cost"] for entry in models.values() if entry["cost"] is not None]
        cost = sum(priced) if priced else None
        budget = self.preferences.budget() if self.preferences else {}
        over = bool(budget.get("daily_tokens") and tokens >= budget["daily_tokens"]) or bool(
            budget.get("daily_cost") and cost is not None and cost >= budget["daily_cost"])
        return {"day": day, "tokens": tokens, "cost": cost, "unpriced": [entry["model"] for entry in models.values()
                                                                         if entry["cost"] is None],
                "currency": self.preferences.currency() if self.preferences else "¥",
                "models": sorted(models.values(), key=lambda entry: -(entry["prompt"] + entry["completion"])),
                "sources": sources, "budget": budget, "over_budget": over}

    def session(self, session: str) -> dict[str, int]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(SUM(prompt), 0) p, COALESCE(SUM(completion), 0) c, COUNT(*) n "
                             "FROM requests WHERE session=?", (session,)).fetchone()
        return {"prompt": row["p"], "completion": row["c"], "requests": row["n"]}


def default_settings_path() -> Path:
    """The shared settings.json, resolved as agent.cli.model_preferences resolves it."""
    import os

    configured = os.environ.get("AGENT_SETTINGS_PATH", "").strip()
    return Path(configured).expanduser() if configured else state_path("settings.json")


def budget_exceeded() -> bool:
    """Whether today's usage is over the person's budget. A process without a ledger of its own only looks."""
    try:
        ledger = _LEDGER
        if ledger is None:
            if not usage_db_path().exists():
                return False
            ledger = UsageLedger(preferences=UsagePreferences(default_settings_path()))
        return bool(ledger.day()["over_budget"])
    except (OSError, sqlite3.Error):
        return False


def install_usage_ledger(ledger: UsageLedger | None) -> None:
    global _LEDGER
    with _LOCK:
        _LEDGER = ledger


def installed_usage_ledger() -> UsageLedger | None:
    return _LEDGER


def report_usage(provider: str, model: str, usage: dict[str, Any] | None) -> None:
    """Called by the model client after each answered request; never lets accounting break a request."""
    ledger = _LEDGER
    if ledger is None or not isinstance(usage, dict):
        return
    try:
        ledger.record(provider=provider, model=model, usage=usage)
    except Exception:  # noqa: BLE001 - accounting must not fail the conversation
        import logging

        logging.getLogger(__name__).exception("could not record model usage")
