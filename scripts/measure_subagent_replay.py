#!/usr/bin/env python3
"""Offline T0 evidence: canonical session sizes and deterministic compaction.

No model requests are made. Historical tool schemas cannot be reconstructed
from session logs; --tool-catalog can provide a reviewed current registry as a
JSON list of {schema: <OpenAI tool schema>, risk, result_persistence} objects.
Without it, unknown tools are protected as writes and schema cost is reported
as unmeasured. Output contains aggregate sizes only, never conversation text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.runtime.paths import sessions_dir
from agent.runtime.session_store import SessionStore
from agent.runtime.subagent_replay import _prepare_context
from agent.runtime.token_estimator import estimate_messages_tokens


class _OfflineEstimator:
    """Only local token estimation; no provider/configuration is instantiated."""

    @staticmethod
    def estimate_tokens(messages):
        return estimate_messages_tokens(messages)


class _Catalog:
    def __init__(self, rows):
        self.schemas = []
        self.tools = {}
        for row in rows:
            schema = row["schema"]
            name = schema["function"]["name"]
            risk = row.get("risk", "write")
            if risk not in {"read", "network", "write", "execute", "secret"}:
                raise ValueError("Tool catalog contains an invalid risk.")
            self.schemas.append(schema)
            self.tools[name] = SimpleNamespace(
                risk=risk, result_persistence=row.get("result_persistence", "durable"),
            )

    def get(self, name):
        return self.tools.get(name)

    def to_openai_tools(self):
        return self.schemas


def _canonical_paths(store):
    return [store.legacy_path, store.jsonl_path, store.snapshot_path, store.header_path]


def _raw_bytes(store):
    return sum(path.stat().st_size for path in _canonical_paths(store) if path.is_file())


def _stores(source: Path):
    candidates = [source] if source.is_file() else source.rglob("*")
    stores = {}
    for path in candidates:
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        if path.name.endswith((".header.json", ".snapshot.json")):
            path = path.with_name(path.name.rsplit(".", 2)[0] + ".json")
        # Sidecar events are intentionally non-canonical and must never be
        # mistaken for SessionStore message histories.
        if ".artifacts" in path.parts and not path.name.endswith((".conv.json", ".conv.jsonl")):
            continue
        store = SessionStore(path)
        stores[str(store.legacy_path)] = store
    return sorted(stores.values(), key=_raw_bytes, reverse=True)


def _distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"median": 0, "p95": 0, "max": 0}
    return {
        "median": statistics.median(ordered),
        "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
        "max": ordered[-1],
    }


async def measure_sessions(source: Path, *, limit: int, budget: int, catalog=None):
    tools = _Catalog(catalog or [])
    stores = _stores(source)
    samples = []
    for index, store in enumerate(stores[:limit], start=1):
        # This replays snapshot/message/replace/header semantics exactly and
        # suppresses recover_interrupted() writes on the source session.
        data = store.load(readonly=True)
        history = data.get("messages", [])
        messages = list(history)
        if data.get("system_prompt"):
            messages.insert(0, {"role": "system", "content": data["system_prompt"]})
        sizes = [len(str(message.get("content", "")).encode("utf-8"))
                 for message in history if message.get("role") == "tool"]
        sample = {
            "sample": index,
            "raw_bytes": _raw_bytes(store),
            "effective_message_count": len(history),
            "tool_result_bytes": _distribution(sizes),
        }
        started = time.perf_counter()
        try:
            context, current, measure, tools_tokens = _prepare_context(
                messages, registry=tools, llm=_OfflineEstimator(), budget=budget,
            )
            before = measure()
            # No compressor service: only the actual deterministic layers run.
            # preserve_on_failure disables the legacy hard-truncation fallback.
            await context.compress_if_needed(preserve_on_failure=True, measure_tokens=measure)
            after = measure()
            sample.update(
                tokens_before=before, tokens_after=after, schema_tokens=tools_tokens,
                compressed_message_count=len(context.messages),
                mechanical_status="within_budget" if after <= budget else "over_budget",
                compaction=context._last_compaction_report,
            )
        except (ValueError, TypeError, KeyError) as error:
            # Do not expose malformed source content in CLI diagnostics.
            sample.update(mechanical_status="invalid_history", error_type=type(error).__name__)
        sample["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        samples.append(sample)
    return {
        "version": 1,
        "mode": "offline_deterministic_only",
        "budget_tokens": budget,
        "canonical_source_count": len(stores),
        "schema_accounting": "provided_catalog" if catalog is not None else "unmeasured_lower_bound",
        "unknown_tool_policy": "preserve_as_write",
        "semantic_validation": "unverified",
        "semantic_probes": [
            "current task progress", "conclusions supported by evidence",
            "next remaining work", "previously excluded hypotheses",
        ],
        "decision": "NEEDS_SEMANTIC_VALIDATION" if samples else "NO_SAMPLES",
        "samples": samples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=Path, default=sessions_dir())
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--budget", type=int, default=64_000)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--tool-catalog", type=Path)
    args = parser.parse_args()
    if args.limit <= 0 or args.budget <= 0:
        parser.error("--limit and --budget must be positive")
    if not args.sessions.exists():
        parser.error("--sessions does not exist")
    catalog = json.loads(args.tool_catalog.read_text()) if args.tool_catalog else None
    report = asyncio.run(measure_sessions(
        args.sessions, limit=args.limit, budget=args.budget, catalog=catalog,
    ))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("sample  raw_bytes  effective_messages  tokens_before  tokens_after  status")
    for sample in report["samples"]:
        print(f"{sample['sample']:>6}  {sample['raw_bytes']:>9}  "
              f"{sample['effective_message_count']:>18}  "
              f"{sample.get('tokens_before', '-'):>13}  "
              f"{sample.get('tokens_after', '-'):>12}  {sample['mechanical_status']}")
    print(f"DECISION: {report['decision']}; semantic probes unverified; "
          f"schema accounting: {report['schema_accounting']}")


if __name__ == "__main__":
    main()
