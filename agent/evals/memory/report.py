"""Compact Markdown tables for a memory evaluation report."""

from __future__ import annotations

from typing import Any

from .fixture import CATEGORIES


def _cell(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return lines


def mode_table(modes: dict[str, Any]) -> list[str]:
    header = ["mode", "recall", "recall(vis)", "full hit", "false inj", "stale inj", "stale(vis)", "dated",
              "inj tok/req", "core tok/req", "fresh tok/req", "core share", "cum tok/conv", "msgs/req",
              "prefix stable", "p50 ms", "p95 ms"]
    rows = []
    for mode, data in modes.items():
        o = data["summary"]["overall"]
        rows.append([
            mode, o["evidence_recall"], o["evidence_recall_visible"], o["full_hit_rate"], o["false_injection_rate"],
            o["stale_injection_rate"], o["stale_visible_rate"], o["dated_evidence_rate"], o["mean_injected_tokens"],
            o["mean_core_tokens"], o["mean_fresh_tokens"], o["core_token_share"], o["mean_cumulative_injected_tokens"],
            o["mean_injected_messages"], o["prefix_stable_rate"], o["latency_p50_ms"], o["latency_p95_ms"],
        ])
    return _table(header, rows)


def category_table(modes: dict[str, Any]) -> list[str]:
    header = ["category", "mode", "n", "recall", "recall(vis)", "false inj", "stale inj", "dated",
              "unexpected items", "inj tok", "core tok", "fresh tok", "msgs", "p50 ms", "p95 ms"]
    rows = []
    for category in CATEGORIES:
        for mode, data in modes.items():
            c = data["summary"]["by_category"].get(category)
            if c is None:
                continue
            rows.append([
                category, mode, c["probes"], c["evidence_recall"], c["evidence_recall_visible"],
                c["false_injection_rate"], c["stale_injection_rate"], c["dated_evidence_rate"],
                c["mean_unexpected_items"], c["mean_injected_tokens"], c["mean_core_tokens"], c["mean_fresh_tokens"],
                c["mean_injected_messages"], c["latency_p50_ms"], c["latency_p95_ms"],
            ])
    return _table(header, rows)


def live_tables(modes: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    header = ["mode", "scored", "pass", "stale answers", "write-path pass", *CATEGORIES]
    rows = []
    for mode, data in modes.items():
        answers = data["answers"]
        rows.append([
            mode, answers["scored"], answers["pass_rate"], answers["stale_answer_rate"],
            data["write_path"]["pass_rate"], *(answers["by_category"].get(category) for category in CATEGORIES),
        ])
    lines.extend(_table(header, rows))
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    lines = [
        f"# Astra memory evaluation ({meta['tier']})",
        "",
        f"Fixture `{meta['fixture']}` sha256 `{meta['fixture_sha256'][:12]}`: {meta['scenarios']} scenarios, "
        f"{meta['probes']} probes; modes {', '.join(report['modes'])}; generated {meta['generated_at']}; "
        f"git {meta.get('git_head', '?')}; embeddings off.",
        "",
        "recall = expected evidence found in the turn's current injected context (fragment match); "
        "recall(vis) = anywhere in injected text the request carries; false inj = unrelated/abstention probes "
        "given any scenario memory beyond core; stale = a superseded value injected; dated = share of recalled "
        "records/messages shown with their own date; tokens are estimates per request (core = core-file entry text); "
        "cum = injected tokens summed over a conversation's requests; latency = turn start to provider call. "
        "Mode all differs from session only by the activity source, which synthetic worlds do not have.",
        "",
        "## Modes",
        "",
        *mode_table(report["modes"]),
        "",
        "## Categories",
        "",
        *category_table(report["modes"]),
    ]
    if meta["tier"] == "live":
        lines.extend(["", f"## Live answers ({report.get('model', '?')})", "", *live_tables(report["modes"])])
    violations = report.get("isolation", {}).get("violations", [])
    lines.extend(["", f"Isolation violations: {len(violations)}"])
    return "\n".join(lines) + "\n"


__all__ = ["category_table", "mode_table", "render_markdown"]
