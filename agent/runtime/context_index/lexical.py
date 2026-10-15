"""Bounded literal-target matching and overlap-aware query coverage.

SQL scores term masks before source limits; fusion uses the same coverage units.
Overlapping CJK grams share query character positions, rather than independent
votes. This is a lexical heuristic, not named-entity recognition or semantics.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .query import retrieval_terms

_CJK = re.compile(r"[\u3400-\u9fff]")
_ASCII = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_REQUEST_WORDS = frozenset({
    "a", "an", "are", "as", "at", "be", "by", "can", "could", "do", "does",
    "have", "help", "in", "is", "it", "of", "on", "or", "please", "should",
    "to", "using", "was", "were", "when", "where", "which", "who", "why", "would", "you",
})
_DESTINATION_ITEM = r"(?:`[^`\r\n]*`|\"[^\"\r\n]*\"|'[^'\r\n]*'|[a-z0-9_.:-]+(?:[/\\][a-z0-9_.:-]+)*)"
_DESTINATION_SEPARATOR = r"\s*(?:以及|和|及|、|[,，&]|\b(?:and|or)\b)\s*"
# Complete earlier list items before the current token or path segment. A
# following instruction (e.g. "再检查 NUS" / "then inspect NUS") breaks this
# grammar, so its subject remains a retrieval target. Lookback stays bounded.
_DESTINATION_PREFIX = (
    r"\s*(?:" + _DESTINATION_ITEM + _DESTINATION_SEPARATOR
    + r")*(?:[`\"'][^`\"']*|(?:[a-z0-9_.:-]+[/\\])*)$"
)
_OUTPUT_DESTINATION = re.compile(
    r"(?:写入|写进|写到|更新进|更新到|保存到|保存为|整理成|汇总到|记录到|同步到)" + _DESTINATION_PREFIX
    + r"|\b(?:write|save|update|record|summarize|export)\b[^。！？;\n]{0,60}"
    r"\b(?:to|into|as)\s+(?:the\s+)?" + _DESTINATION_PREFIX, re.I,
)


@dataclass(frozen=True)
class PairIndex:
    """A CJK character-pair FTS5 table complete up to ``through`` (internal names)."""

    table: str
    rows: str
    column: str
    through: int


def _is_pair(term: str) -> bool:
    return len(term) == 2 and all(_CJK.match(character) for character in term)


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass(frozen=True)
class LexicalQuery:
    terms: tuple[str, ...]
    anchors: tuple[str, ...]
    # Each mask identifies grams covering a query position; weight merges
    # identical masks. An ASCII word contributes three character units.
    positions: tuple[tuple[int, int], ...]
    semantic_anchors: tuple[str, ...] = ()

    @classmethod
    def from_text(cls, text: str, *, word_split: bool = True) -> LexicalQuery:
        """``word_split=False`` keeps character trigrams for sources without a pair index."""
        raw = unicodedata.normalize("NFKC", str(text)[:900])
        normalized = raw.casefold()
        mixed = bool(_CJK.search(raw))
        anchor_words = []
        semantic_words = []
        literals = list(re.finditer(r"`[^`]+`|\"[^\"]+\"|'[^']+'", raw))
        for match in _ASCII.finditer(raw):
            word = match.group().strip(".-")
            term = word.casefold()
            if len(term) < 2 or not re.search(r"[a-z]", term) or term in _REQUEST_WORDS:
                continue
            if not retrieval_terms(term):
                continue
            # An output artifact is not necessarily mentioned by the history
            # being summarized (e.g. "把今天的经验更新进 skill").
            if _OUTPUT_DESTINATION.search(raw[max(0, match.start() - 90):match.start()]):
                continue
            # Lexical search retains its precision heuristics. Semantic search
            # must not make a normal English word mandatory just because the
            # rest of the query is Chinese (e.g. "之前修复的 bug").
            identifier = "_" in term or "." in term or bool(
                re.search(r"[A-Za-z].*[A-Z]|[a-z].*\d|\d.*[a-z]", word))
            literal = any(span.start() < match.start() and match.end() < span.end() for span in literals)
            if identifier or literal:
                semantic_words.append(term)
            if mixed or identifier or literal:
                anchor_words.append(term)
        anchors = tuple(dict.fromkeys(anchor_words))[:12]
        # Preserve explicit targets even after a long Chinese query reaches
        # the gram cap. The total SQL term budget is still twelve.
        terms = tuple(dict.fromkeys((*anchors, *retrieval_terms(raw, word_split=word_split))))[:12]
        characters: dict[int, int] = {}
        positions: Counter[int] = Counter()
        for index, term in enumerate(terms):
            bit = 1 << index
            if _CJK.match(term):
                start = normalized.find(term)
                for offset in range(start, start + len(term)):
                    characters[offset] = characters.get(offset, 0) | bit
            else:
                positions[bit] += 3
        positions.update(characters.values())
        return cls(terms, anchors, tuple(sorted(positions.items())),
                   tuple(dict.fromkeys(semantic_words))[:12])

    @property
    def minimum(self) -> int:
        return min(2, len(self.terms))

    def mask_sql(self, column: str) -> tuple[str, tuple[str, ...]]:
        """Column names are internal expressions, never user input."""
        sql = " + ".join(
            f"(CASE WHEN {column} LIKE ? ESCAPE '\\' THEN {1 << i} ELSE 0 END)"
            for i in range(len(self.terms))
        )
        return sql or "0", tuple(_like(term) for term in self.terms)

    def count_sql(self, mask: str) -> str:
        return " + ".join(f"(({mask} & {1 << i}) != 0)" for i in range(len(self.terms))) or "0"

    def coverage_sql(self, mask: str) -> str:
        return " + ".join(f"(({mask} & {bits}) != 0) * {weight}" for bits, weight in self.positions) or "0"

    def anchor_sql(self, column: str) -> tuple[str, tuple[str, ...]]:
        # GLOB is literal for identifier underscores; padding supplies word
        # boundaries at the start/end. "nus" must not match "sinus" or "nus2".
        return (
            " OR ".join(f"(' ' || lower({column}) || ' ') GLOB ?" for _ in self.anchors) or "1",
            tuple(f"*[^a-z0-9_]{term}[^a-z0-9_]*" for term in self.anchors),
        )

    def anchor_fts_query(self) -> str:
        """Lossless trigram prefilter only when every allowed target is indexed."""
        if not self.anchors or any(len(term) < 3 for term in self.anchors):
            return ""
        return " OR ".join(f'"{term}"' for term in self.anchors)

    def indexed_masks_sql(
        self, table: str, pairs: PairIndex | None = None,
    ) -> tuple[str, tuple[object, ...]]:
        """Exact term masks from a trigram index; table is an internal name.

        Each posting is visited once per query term. Short terms cannot use
        trigram MATCH and must keep the literal scan path instead, unless a
        two-character CJK term has a pair index: its postings up to the
        watermark, plus a literal check of the rows written after it. In
        particular, do not truncate the posting lists before coverage or time
        filtering.
        """
        if not self.terms or any(len(term) < 3 and not (pairs and _is_pair(term)) for term in self.terms):
            return "", ()
        # A short target list is selective; repeating a broad OR of many
        # targets in every posting lookup adds work without useful pruning.
        anchors = self.anchor_fts_query() if len(self.anchors) <= 2 else ""
        postings: list[str] = []
        params: list[object] = []
        for i, term in enumerate(self.terms):
            if len(term) >= 3:
                postings.append(f"SELECT rowid AS id, {1 << i} AS bit FROM {table}(?)")
                params.append(f'"{term}" AND ({anchors})' if anchors else f'"{term}"')
            elif pairs is not None:
                # Targets are applied by the caller's row filter instead.
                postings.append(
                    f"SELECT rowid AS id, {1 << i} AS bit FROM {pairs.table}(?) WHERE rowid <= ? "
                    f"UNION ALL SELECT id, {1 << i} AS bit FROM {pairs.rows} "
                    f"WHERE id > ? AND {pairs.column} LIKE ? ESCAPE '\\'"
                )
                params.extend((f'"{term}"', pairs.through, pairs.through, _like(term)))
        return (
            "query_masks AS MATERIALIZED (SELECT id, sum(bit) AS query_match_mask FROM ("
            + " UNION ALL ".join(postings) + ") GROUP BY id HAVING count(*) >= ?)",
            (*params, self.minimum),
        )

    def bind_anchor_sql(self, connection: sqlite3.Connection, column: str) -> str:
        """One boundary scan instead of lower/copy/GLOB for every anchor."""
        if not self.anchors:
            return "1"
        pattern = re.compile(
            r"(?:" + "|".join(map(re.escape, self.anchors)) + r")(?![a-z0-9_])",
            re.IGNORECASE | re.ASCII,
        )
        def matches(value: str | None) -> int:
            text = str(value or "")
            position = 0
            while match := pattern.search(text, position):
                if match.start() == 0 or text[match.start() - 1] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_":
                    return 1
                position = match.start() + 1
            return 0

        connection.create_function(
            "context_index_anchors", 1, matches, deterministic=True,
        )
        return f"context_index_anchors({column})"

    def matches_anchors(self, text: str, *, semantic: bool = False) -> bool:
        normalized = str(text)[:6000].lower()
        anchors = self.semantic_anchors if semantic else self.anchors
        return not anchors or any(
            re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", normalized)
            for term in anchors
        )

    def coverage(self, text: str) -> float:
        normalized = str(text)[:6000].lower()
        mask = sum(1 << i for i, term in enumerate(self.terms) if term in normalized)
        return sum(weight for bits, weight in self.positions if mask & bits) / max(
            1, sum(weight for _, weight in self.positions)
        )
