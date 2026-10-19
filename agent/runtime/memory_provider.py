"""Provider contract for built-in and external long-term memory backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable

from .memory_records import MemoryRecord


@dataclass(frozen=True)
class MemoryProviderCapabilities:
    recall: bool
    retain: bool
    reflect: bool
    provenance: bool
    supersede: bool


@dataclass(frozen=True)
class MemoryProviderHealth:
    state: str
    detail: str
    capabilities: MemoryProviderCapabilities


class MemoryProvider(ABC):
    name: str
    capabilities: MemoryProviderCapabilities

    @abstractmethod
    async def recall(self, query: str, *, kinds: Iterable[str] = (), limit: int = 8) -> list[MemoryRecord]:
        raise NotImplementedError

    @abstractmethod
    async def retain(self, *, kind: str, content: str, **kwargs: Any) -> MemoryRecord:
        raise NotImplementedError

    @abstractmethod
    async def supersede(self, record_id: str, *, content: str, **kwargs: Any) -> MemoryRecord:
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> MemoryProviderHealth:
        raise NotImplementedError


class BuiltinMemoryProvider(MemoryProvider):
    name = "builtin-sqlite"
    capabilities = MemoryProviderCapabilities(
        recall=True,
        retain=True,
        reflect=False,
        provenance=True,
        supersede=True,
    )

    def __init__(self, store):
        self.store = store

    async def recall(self, query: str, *, kinds: Iterable[str] = (), limit: int = 8) -> list[MemoryRecord]:
        return self.store.recall_records(query, kinds=kinds, limit=limit)

    async def retain(self, *, kind: str, content: str, **kwargs: Any) -> MemoryRecord:
        return self.store.add_record(kind=kind, content=content, **kwargs)

    async def supersede(self, record_id: str, *, content: str, **kwargs: Any) -> MemoryRecord:
        return self.store.supersede_record(record_id, content=content, **kwargs)

    async def health(self) -> MemoryProviderHealth:
        stats = self.store.record_store.stats()
        return MemoryProviderHealth(
            state="available",
            detail=f"records={stats['total']}; fts5={'enabled' if stats['fts_enabled'] else 'fallback-like'}",
            capabilities=self.capabilities,
        )
