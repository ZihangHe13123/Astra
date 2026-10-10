"""Conclave tool — run a multi-expert research panel from a tool call."""

from __future__ import annotations

from agent.runtime.paths import state_path

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

logger = logging.getLogger(__name__)
DEFAULT_TRANSCRIPT_DIR = state_path("conclave")


def _persist_discussion_transcript(result, directory: str | Path | None = None) -> Path:
    """Persist one complete, human-readable Conclave run atomically."""
    target_dir = Path(directory) if directory is not None else DEFAULT_TRANSCRIPT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"conclave-{stamp}-{uuid.uuid4().hex[:8]}.md"
    sections = [
        "# Conclave 研讨会逐字稿",
        "",
        f"- 问题：{result.question}",
        f"- 意图：{result.intent}",
        f"- 专家：{', '.join(result.experts_selected)}",
        f"- 耗时：{result.duration_ms / 1000:.1f}s",
        "",
        "## 专家初始报告",
    ]
    for expert in result.experts_selected:
        sections.extend(("", f"### {expert}", "", result.expert_reports.get(expert, "（无报告）")))
    sections.extend(("", "## 交叉讨论逐字稿", ""))
    if result.discussion_contributions:
        for expert in result.experts_selected:
            contribution = result.discussion_contributions.get(expert)
            if contribution is not None:
                sections.extend((f"### {expert}", "", contribution, ""))
    else:
        sections.append(result.cross_discussion or "（无讨论记录）")
    sections.extend(("", "## 主席总结陈词", "", result.synthesis or "（无主席总结）", ""))
    temporary = target.with_suffix(".md.tmp")
    try:
        temporary.write_text("\n".join(sections), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def _parse_search_results(output: str) -> list[dict[str, str]]:
    """Parse the stable, human-readable ``search_web`` result contract."""
    findings: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        match = re.match(r"^\[(\d+)\]\s+(.+)$", line)
        if match:
            if current and current.get("url"):
                findings.append(current)
            current = {"title": match.group(2).strip(), "url": "", "snippet": ""}
        elif current is not None and line.startswith("URL:"):
            current["url"] = line[4:].strip()
        elif current is not None and line.startswith(("Snippet:", "Page content:")):
            value = line.split(":", 1)[1].strip()
            if value:
                current["snippet"] = " ".join(
                    item for item in (current.get("snippet", ""), value) if item
                )
    if current and current.get("url"):
        findings.append(current)
    return findings


def register_conclave_tools(
    registry: ToolRegistry,
    llm_getter: Callable[[], Any] | None = None,
    transcript_dir: str | Path | None = None,
) -> None:
    """Register the Conclave research panel as an Astra tool."""

    async def _run_conclave(
        question: str,
        discussion: bool | None = None,
        _progress=None,
    ) -> str | ToolFailure:
        """Run the Conclave multi-expert research panel.

        Gathers findings from multiple search engines (Google, Bing, GitHub, arXiv,
        academic, Chinese sources, etc), then compiles a comprehensive report.

        Args:
            question: The research question or topic to investigate.
            discussion: Whether the chairperson should run an extra cross-source
                discussion before the final synthesis.  ``None`` uses the saved
                Conclave configuration.
        """
        from ..conclave.config import ConclaveConfig
        from ..conclave.core import Conclave, INTENT_ROUTES, unique_source_count
        from ..conclave.llm import chairperson_chat

        config = ConclaveConfig.load()
        conclave = Conclave()
        active_llm = llm_getter() if llm_getter is not None else None
        llm_chat = (
            chairperson_chat(
                active_llm,
                config.chairperson_model,
                config.max_tokens,
            )
            if active_llm is not None
            else None
        )
        expert_chat = (
            chairperson_chat(
                active_llm,
                config.chairperson_model,
                config.expert_max_tokens,
                request_role="expert",
            )
            if active_llm is not None
            else None
        )
        search_errors: list[str] = []

        async def search_with_runtime_provider(expert, query: str, limit: int):
            name, engine, category, guidance = expert
            category_name = (
                "science" if category == "学术论文"
                else "news" if category == "新闻资讯"
                else "it" if category in {"代码仓库", "技术问答"}
                else "general"
            )
            # Use the already-configured web provider waterfall (including Exa,
            # proxy routing, cache and bounded timeouts) instead of requiring a
            # separate localhost SearXNG service just for Conclave.
            search_tool = registry.get("search_web")
            if search_tool is None:
                raise RuntimeError("search_web is not registered")
            response = await registry.execute(
                "search_web",
                {
                    "query": f"{query} {guidance}",
                    "max_results": limit,
                    "provider": "auto",
                    "language": "auto",
                    "category": category_name,
                    "engine": engine,
                },
            )
            if response.get("error"):
                search_errors.append(str(response["error"]))
                raise RuntimeError(str(response["error"]))
            findings = _parse_search_results(str(response.get("output") or ""))
            if not findings:
                logger.warning("Conclave expert returned no sources name=%s", name)
            return findings

        async def stream_progress(event: dict) -> None:
            if _progress is None:
                return
            event_type = str(event.get("type") or "")
            if event_type == "conclave_phase":
                _progress(
                    f"phase_{event.get('phase', 0)}",
                    message=str(event.get("label") or ""),
                )
            elif event_type == "conclave_findings":
                _progress(
                    "sources_collected",
                    current=int(event.get("total_sources") or 0),
                    unit="sources",
                )
            elif event_type == "conclave_expert_started":
                _progress(
                    "expert_started",
                    message=str(event.get("expert") or ""),
                )
            elif event_type == "conclave_expert_completed":
                _progress(
                    "expert_completed",
                    message=(
                        f"{event.get('expert')}: {event.get('sources', 0)} sources, "
                        f"{event.get('searches', 0)} searches"
                    ),
                )
            elif event_type == "conclave_discussion_contribution":
                _progress(
                    "discussion",
                    current=int(event.get("index") or 0),
                    total=int(event.get("total") or 0),
                    unit="experts",
                    message=str(event.get("expert") or ""),
                )

        runtime_search = search_with_runtime_provider if registry.get("search_web") else None
        cross_discussion = (
            config.cross_discussion
            if discussion is None
            else discussion
        )
        result = await conclave.run(
            question=question,
            llm_chat=llm_chat,
            stream_events=stream_progress,
            experts=config.experts,
            max_sources_per_expert=config.max_sources_per_expert,
            cross_discussion=cross_discussion,
            search=runtime_search,
            expert_llm_chat=expert_chat,
            max_search_rounds=config.max_search_rounds,
            max_parallel_experts=config.max_parallel_experts,
        )

        if cross_discussion and (
            result.discussion_contributions or result.cross_discussion
        ):
            try:
                result.transcript_path = str(
                    _persist_discussion_transcript(result, transcript_dir)
                )
            except OSError as exc:
                logger.warning("Conclave transcript persistence failed error=%s", exc)

        intent_label = INTENT_ROUTES.get(result.intent, ("全面调研", []))[0]
        total_sources = unique_source_count(result.findings)
        searches = int(result.stats.get("search_calls", 0))
        failed_searches = int(result.stats.get("search_failures", 0))
        if searches and failed_searches == searches and not total_sources:
            # A synthesis written from no evidence at all is not a research result.
            return ToolFailure(
                code="conclave_search_failed",
                message=(
                    f"Conclave found no sources: all {searches} searches failed"
                    + (f" (last error: {search_errors[-1][:300]})" if search_errors else "")
                    + "."
                ),
                retryable=False,
                recovery_hint=(
                    "Call search_web once with the question to see whether web search works. If it does "
                    "not, answer from other evidence and say that the panel could not search."
                ),
            )
        lines = [result.synthesis.rstrip(), "", "---"]
        lines.append(
            f"*{intent_label} · {len(result.experts_selected)} 位专家 · "
            f"{total_sources} 条来源 · "
            f"{searches} 次检索"
            + (f"（其中 {failed_searches} 次失败）" if failed_searches else "")
            + " · "
            f"{sum(result.stats.get(key, 0) for key in ('expert_llm_calls', 'discussion_llm_calls', 'chairperson_llm_calls'))} 次模型调用 · "
            f"{'含专家交叉讨论' if cross_discussion else '专家报告后主席总结'} · "
            f"{result.duration_ms/1000:.1f}s*"
        )
        if result.transcript_path:
            lines.append(f"*讨论逐字稿：{result.transcript_path}*")
        return "\n".join(lines)

    registry.register(ToolDef(
        name="conclave",
        description=(
            "Run a multi-expert research panel (Conclave) to investigate a question. "
            "The panel picks 3 to 5 experts from keywords in the question (or uses the user's "
            "configured list of up to 8) from a pool covering Google, Bing, "
            "GitHub, arXiv, academic papers, Chinese sources (知乎/百度/B站), Wikipedia, "
            "news, StackOverflow, and Reddit. Each expert runs at least two distinct searches "
            "through the configured web provider, deduplicates evidence, and writes an independent "
            "report before the chairperson synthesizes all reports. "
            "Use when you need thorough, multi-angle research. discussion=true adds an ordered "
            "round of expert rebuttals and supplements (one more model call per expert): set it only "
            "when conflicting evidence, competing designs, trade-offs, or important omissions need "
            "it. discussion=false skips that round. Omitting discussion uses the user's saved "
            "Conclave setting, which may be on; pass false for straightforward research to reduce "
            "latency and cost. A run can take several minutes and is stopped after 10."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The research question or topic to investigate",
                },
                "discussion": {
                    "type": "boolean",
                    "description": (
                        "是否在专家报告之后增加一轮按顺序进行的专家交叉讨论。仅当问题存在重要分歧、"
                        "需要比较冲突证据或审视遗漏时设为 true；设为 false 则不讨论；省略时沿用用户"
                        "保存的 Conclave 配置（可能是开启），普通事实调研请明确传 false。"
                        "开启后每位专家各多一次模型调用，延迟随之增加。"
                    ),
                },
            },
            "required": ["question"],
        },
        fn=_run_conclave,
        risk="network",
        approval="never",
        group="conclave",
        sandboxed=False,
        timeout=600.0,
        # /conclave runs the panel directly; the model-facing tool is opt-in
        # (AGENT_TOOL_EXPOSE=conclave) because its schema ships on every request.
        expose_by_default=False,
    ))
