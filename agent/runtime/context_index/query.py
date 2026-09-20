"""Bounded, deterministic task queries. No model, service, or extra dependency."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

_TRIVIAL = frozenset(
    {
        "好",
        "好的",
        "行",
        "可以",
        "收到",
        "明白",
        "谢谢",
        "你好",
        "您好",
        "ok",
        "okay",
        "thanks",
        "thank you",
        "hi",
        "hello",
    }
)
# Match whole greetings only; particles are bounded to one explicit character.
# A greeting followed by a request must remain eligible for recall.
_GREETING = re.compile(
    r"(?:你好|您好|早上好|上午好|中午好|下午好|晚上好|早安|午安|晚安)[呀啊]?"
    r"|(?:hi|hello|good\s+(?:morning|afternoon|evening|night))",
    re.I,
)
# Whole, unambiguous arithmetic only. Bare dates, versions and substantive
# questions containing an expression must still be eligible for recall.
_ARITHMETIC = re.compile(r"\d+(?:\.\d+)?(?:\s*[+*×÷]\s*\d+(?:\.\d+)?)+")
_RESUME = re.compile(r"继续|接着|恢复|上次做到|进度|\b(?:continue|resume|progress)\b", re.I)
_HISTORY = re.compile(r"记得|之前|上次|以前|曾经|历史|\b(?:remember|previous|history|last time)\b", re.I)
_PREFERENCE = re.compile(r"偏好|习惯|喜欢|讨厌|\b(?:prefer|preference|habit)\b", re.I)
_RECENT = re.compile(r"最近|刚才|刚刚|\b(?:recent|recently|latest|just now)\b", re.I)


DEFAULT_MAX_ITEMS = 4
_SEARCH_STOP = frozenset({
    "the", "and", "for", "this", "that", "with", "from", "what", "how",
    "continue", "resume", "previous", "yesterday", "today", "remember",
    "之前", "上次", "继续", "一下", "这个", "那个", "什么", "怎么", "如何",
    "昨天", "今天", "前天", "最近", "刚才", "记得",
})
_CJK_SEARCH_STOP = re.compile("|".join(sorted(
    {term for term in _SEARCH_STOP if "\u3400" <= term[0] <= "\u9fff"}
    | {"帮我", "帮忙", "请问", "麻烦", "看看", "目前", "现在"},
    key=lambda term: (-len(term), term),
)))


@dataclass(frozen=True)
class QueryPlan:
    query: str
    original: str
    intent: str
    should_recall: bool
    include_recent: bool
    cutoff: float
    time_start: float = 0.0
    time_end: float = 0.0
    max_items: int = DEFAULT_MAX_ITEMS
    message_id: int | None = None

    def contains(self, timestamp: float) -> bool:
        return self.time_start <= timestamp <= self.cutoff and (not self.time_end or timestamp < self.time_end)


def text_features(text: str) -> frozenset[str]:
    value = unicodedata.normalize("NFKC", str(text)[:6000]).casefold()
    words = set(re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", value))
    for run in re.findall(r"[\u3400-\u9fff]{2,}", value):
        words.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(
        words - {"the", "and", "for", "this", "that", "with", "from", "之前", "上次", "继续", "一下", "这个", "什么"}
    )


def retrieval_terms(text: str) -> tuple[str, ...]:
    """Bounded Unicode terms, including CJK phrases without a tokenizer package."""
    value = unicodedata.normalize("NFKC", str(text)[:900]).casefold()
    # Split conversational phrases before generating character terms. Removing
    # only final stop terms leaves spurious boundary grams such as "个报".
    value = _CJK_SEARCH_STOP.sub(" ", value)
    terms = []
    tokens = re.findall(r"[a-z0-9][a-z0-9_.-]*|[\u3400-\u9fff]+", value)
    for token in tokens:
        if "\u3400" <= token[0] <= "\u9fff" and len(token) > 2:
            # Match Astra's existing FTS5 trigram index. Bigrams force full
            # scans of long archives and carry little specificity by themselves.
            terms.extend(token[i:i + 3] for i in range(len(token) - 2))
        elif "\u3400" <= token[0] <= "\u9fff" and len(token) == 1 and len(tokens) > 1:
            continue
        else:
            terms.append(token)
    return tuple(term for term in dict.fromkeys(terms) if term not in _SEARCH_STOP)[:12]


def lexical_match_sql(column: str, terms: tuple[str, ...]) -> tuple[str, tuple[str, ...], int]:
    """Shared Session/Record coverage rule; column is an internal SQL expression.

    Multi-term lookups need two distinct term matches; a single useful term is
    sufficient for a short query. Rank coverage before truncating either pool.
    """
    score = " + ".join(f"(CASE WHEN {column} LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)" for _ in terms)
    params = tuple(
        "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for term in terms
    )
    return score or "0", params, min(2, len(terms))


def plan_query(
    text: str,
    now: datetime,
    *,
    recent_text: str = "",
    task_text: str = "",
) -> QueryPlan:
    original = str(text).strip()
    normalized = re.sub(r"[\s!?。！？,.，]+", " ", original.casefold()).strip()
    arithmetic = unicodedata.normalize("NFKC", original).rstrip("!?。！？= ").strip()
    greeting = _GREETING.fullmatch(original.strip(" \t\r\n!?。！？,.，~～"))
    enabled = (bool(normalized) and normalized not in _TRIVIAL and not original.startswith("/")
               and not greeting and not _ARITHMETIC.fullmatch(arithmetic))
    resume = bool(_RESUME.search(original))
    recent = bool(_RECENT.search(original))
    intent = (
        "resume"
        if resume
        else "preference"
        if _PREFERENCE.search(original)
        else "history"
        if _HISTORY.search(original)
        else "lookup"
    )
    start = end = 0.0
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", original)[:2]
    if dates:
        try:
            first = datetime.fromisoformat(dates[0]).replace(tzinfo=now.tzinfo)
            last = datetime.fromisoformat(dates[-1]).replace(tzinfo=now.tzinfo)
            start, end = first.timestamp(), (last + timedelta(days=1)).timestamp()
        except ValueError:
            pass
    elif re.search(r"前天|\bday before yesterday\b", original, re.I):
        start, end = (day - timedelta(days=2)).timestamp(), (day - timedelta(days=1)).timestamp()
    elif re.search(r"昨天|\byesterday\b", original, re.I):
        start, end = (day - timedelta(days=1)).timestamp(), day.timestamp()
    elif re.search(r"今天|\btoday\b", original, re.I):
        start, end = day.timestamp(), (day + timedelta(days=1)).timestamp()
    elif re.search(r"最近一周|过去一周|近七天|\bpast week\b|\blast 7 days\b", original, re.I):
        start = (now - timedelta(days=7)).timestamp()
    temporal = bool(start or end)
    if temporal and not resume:
        intent = "temporal"
    # Only anaphoric/resume turns inherit older task terms. A new independent
    # question must not be contaminated by the previous topic.
    query = original[:900]
    if resume:
        meaningful = [
            re.sub(r"^(?:Request|Objective|Result|Criteria):\s*", "", line.strip())
            for line in task_text.splitlines()
            if line.strip().startswith(("Request:", "Objective:", "Result:", "Criteria:"))
        ]
        task_text = " ".join(meaningful) if meaningful else re.sub(r"<[^>]+>", " ", task_text)
        query = " ".join(part for part in (original[:240], task_text[:320], recent_text[-320:]) if part)
        if (
            not task_text
            and not recent_text
            and re.fullmatch(r"(?:继续|接着|恢复)(?:吧)?|continue|resume", original, re.I)
        ):
            query = ""
    if recent and intent == "lookup":
        intent = "history"
    return QueryPlan(query, original, intent, enabled, resume or temporal or recent, now.timestamp(), start, end)
