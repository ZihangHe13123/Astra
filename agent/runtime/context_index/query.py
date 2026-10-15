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
_CJK_PHRASE_STOP = {term for term in _SEARCH_STOP if "\u3400" <= term[0] <= "\u9fff"} | {
    "帮我", "帮忙", "请问", "麻烦", "看看", "目前", "现在"}
_CJK_SEARCH_STOP = re.compile("|".join(sorted(_CJK_PHRASE_STOP, key=lambda term: (-len(term), term))))
# Once runs split into words, a common word alone would match most history.
_CJK_WORD_STOP = re.compile("|".join(sorted(_CJK_PHRASE_STOP | {
    "多少", "为什么", "怎么样", "怎么办", "觉得", "知道", "可以", "需要", "应该", "问题", "时候",
    "东西", "事情", "怎样", "一些", "有点", "有些", "可能", "已经", "然后", "因为", "所以", "但是",
    "如果", "或者", "就是", "只是", "自己", "大家", "一起", "告诉", "其实", "真的", "具体", "情况",
    "感觉", "好像", "一直", "刚刚", "一共", "总共",
    # Question words about time leave a stray character (哪天 → 天) when split.
    "哪天", "哪几天", "哪一天", "几天", "几号", "几点", "几月", "多久",
    # A second relative date stays in the query; the window holds the first.
    # Not 上周 or 上星期: they occur inside 晚上周末 and 早上星期一.
    "这周", "本周", "上个月", "这个月", "本月", "前几天"}, key=lambda term: (-len(term), term))))
# Particles, pronouns, copulas and question words join the words of a question
# rather than form them. Splitting there keeps a two-character word such as
# 牙医 or 车位 whole instead of cutting grams across the boundary (牙医是, 位在哪).
# Characters that commonly start or end content words (都, 就, 有, 更, 太) stay.
_CJK_FUNCTION = re.compile("[的了吗呢啊呀嘛哦啦是哪谁啥几么我你他她它们在还和与跟或也又再很最被把给让这那个不没]")


_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_COUNT = r"(\d{1,2}|[一二两三四五六七八九十]{1,3})"
# 晚上周末 and 早上月初 are not 上周 / 上月.
_LAST = r"(?<![早晚马路身线网楼桌手])上(?:个)?"
_WEEK = r"(?:周|星期|礼拜)"
# Capitalized only: "may 5 people" is not a date.
_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EN_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _count(text: str) -> int:
    if text.isdigit():
        return int(text)
    tens, ten, ones = text.partition("十")
    if not ten:
        return _CN_DIGITS.get(text, 0)
    return _CN_DIGITS.get(tens, 1 if not tens else 0) * 10 + _CN_DIGITS.get(ones, 0)


def _relative_window(text: str, now: datetime) -> tuple[datetime, datetime | None, tuple[int, int]] | None:
    """Calendar window for a relative or month-day date, and the phrase's span.

    Weeks start on Monday. ``None`` as the end means "until now".
    """
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)

    def months_back(count: int) -> datetime:
        index = month.year * 12 + month.month - 1 - count
        return month.replace(year=index // 12, month=index % 12 + 1)

    def on(value: datetime, match: re.Match[str]) -> tuple[datetime, datetime, tuple[int, int]]:
        return value, value + timedelta(days=1), match.span()

    if match := re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text) or re.search(
            r"\b(" + "|".join(_EN_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text):
        first = match.group(1)
        number = int(first) if first.isdigit() else _EN_MONTHS.index(first) + 1
        try:
            date = day.replace(month=number, day=int(match.group(2)))
            # A month-day later than today means last year's.
            return on(date if date <= day else date.replace(year=date.year - 1), match)
        except ValueError:
            return None
    if match := re.search(r"大前天", text):
        return on(day - timedelta(days=3), match)
    if match := re.search(_COUNT + r"\s*天(?:之)?前", text) or re.search(r"\b(\d{1,2}) days? ago\b", text, re.I):
        return on(day - timedelta(days=_count(match.group(1))), match)
    if match := re.search(_LAST + _WEEK + "([一二三四五六日天])", text):
        offset = "一二三四五六".find(match.group(1))
        return on(week - timedelta(days=7) + timedelta(days=offset if offset >= 0 else 6), match)
    if match := re.search(r"\blast (" + "|".join(_EN_DAYS) + r")\b", text, re.I):
        return on(week - timedelta(days=7) + timedelta(days=_EN_DAYS.index(match.group(1).lower())), match)
    if match := re.search(_COUNT + r"\s*(?:个)?" + _WEEK + r"(?:之)?前", text) or re.search(
            r"\b(\d{1,2}) weeks? ago\b", text, re.I):
        start = week - timedelta(days=7 * _count(match.group(1)))
        return start, start + timedelta(days=7), match.span()
    if match := re.search(_LAST + _WEEK + r"|\blast week\b", text, re.I):
        return week - timedelta(days=7), week, match.span()
    if match := re.search(r"(?:这|本)(?:个)?" + _WEEK + r"|\bthis week\b", text, re.I):
        return week, None, match.span()
    if match := re.search(_COUNT + r"\s*个月(?:之)?前", text) or re.search(r"\b(\d{1,2}) months? ago\b", text, re.I):
        count = _count(match.group(1))
        return months_back(count), months_back(count - 1), match.span()
    if match := re.search(_LAST + r"月|\blast month\b", text, re.I):
        return months_back(1), month, match.span()
    if match := re.search(r"(?:这|本)(?:个)?月|\bthis month\b", text, re.I):
        return month, None, match.span()
    if match := re.search(r"前几天|几天(?:之)?前|\b(?:a )?few days ago\b", text, re.I):
        return day - timedelta(days=7), day, match.span()
    if match := re.search(r"最近一周|过去一周|近七天|\bpast week\b|\blast 7 days\b", text, re.I):
        return now - timedelta(days=7), None, match.span()
    if match := re.search(r"(?:最近|过去|近)(?:一个月|30\s*天|三十天)|\bpast month\b|\blast 30 days\b", text, re.I):
        return now - timedelta(days=30), None, match.span()
    return None


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


def retrieval_terms(text: str, *, word_split: bool = True) -> tuple[str, ...]:
    """Bounded Unicode terms, including CJK phrases without a tokenizer package.

    ``word_split`` splits CJK runs at function characters, which leaves
    two-character words; only sources with a pair index can look those up
    without scanning.
    """
    value = unicodedata.normalize("NFKC", str(text)[:900]).casefold()
    # Split conversational phrases before generating character terms. Removing
    # only final stop terms leaves spurious boundary grams such as "个报".
    value = (_CJK_WORD_STOP if word_split else _CJK_SEARCH_STOP).sub(" ", value)
    terms = []
    tokens = []
    for token in re.findall(r"[a-z0-9][a-z0-9_.-]*|[\u3400-\u9fff]+", value):
        pieces = _CJK_FUNCTION.split(token) if word_split else [token]
        # A lone character left between function words is not a search term.
        tokens.extend(piece for piece in pieces if len(piece) > 1 or (piece and len(pieces) == 1))
    for token in tokens:
        if word_split and "\u3400" <= token[0] <= "\u9fff" and len(token) in (3, 4):
            # Usually two words (课程讲师): trigrams would straddle them.
            terms.extend(token[i:i + 2] for i in range(len(token) - 1))
        elif "\u3400" <= token[0] <= "\u9fff" and len(token) > 2:
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
    # The window carries a relative date; its words are not search terms.
    searchable = original
    if dates:
        try:
            first = datetime.fromisoformat(dates[0]).replace(tzinfo=now.tzinfo)
            last = datetime.fromisoformat(dates[-1]).replace(tzinfo=now.tzinfo)
            start, end = first.timestamp(), (last + timedelta(days=1)).timestamp()
        except ValueError:
            pass
    elif window := _relative_window(original, now):
        start, end = window[0].timestamp(), window[1].timestamp() if window[1] else 0.0
        searchable = original[:window[2][0]] + " " + original[window[2][1]:]
    elif re.search(r"前天|\bday before yesterday\b", original, re.I):
        start, end = (day - timedelta(days=2)).timestamp(), (day - timedelta(days=1)).timestamp()
    elif re.search(r"昨天|\byesterday\b", original, re.I):
        start, end = (day - timedelta(days=1)).timestamp(), day.timestamp()
    elif re.search(r"今天|\btoday\b", original, re.I):
        start, end = day.timestamp(), (day + timedelta(days=1)).timestamp()
    temporal = bool(start or end)
    if temporal and not resume:
        intent = "temporal"
    # Only anaphoric/resume turns inherit older task terms. A new independent
    # question must not be contaminated by the previous topic.
    query = searchable[:900]
    if resume:
        meaningful = [
            re.sub(r"^(?:Request|Objective|Result|Criteria):\s*", "", line.strip())
            for line in task_text.splitlines()
            if line.strip().startswith(("Request:", "Objective:", "Result:", "Criteria:"))
        ]
        task_text = " ".join(meaningful) if meaningful else re.sub(r"<[^>]+>", " ", task_text)
        query = " ".join(part for part in (searchable[:240], task_text[:320], recent_text[-320:]) if part)
        if (
            not task_text
            and not recent_text
            and re.fullmatch(r"(?:继续|接着|恢复)(?:吧)?|continue|resume", original, re.I)
        ):
            query = ""
    if recent and intent == "lookup":
        intent = "history"
    return QueryPlan(query, original, intent, enabled, resume or temporal or recent, now.timestamp(), start, end)
