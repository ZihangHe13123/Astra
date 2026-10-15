"""Provider contract for built-in and external long-term memory backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
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


class FederatedMemoryProvider(MemoryProvider):
    """Keep builtin memory authoritative while adding optional external recall."""

    name = "federated-memory"

    def __init__(self, authority: MemoryProvider, external: MemoryProvider, *,
                 external_timeout: float | None = None):
        self.authority = authority
        self.external = external
        # A slow external service must not take the builtin results with it: it gets
        # its own deadline inside the caller's interactive budget.
        self.external_timeout = external_timeout
        self.capabilities = MemoryProviderCapabilities(
            recall=authority.capabilities.recall or external.capabilities.recall,
            retain=authority.capabilities.retain,
            reflect=external.capabilities.reflect,
            provenance=authority.capabilities.provenance and external.capabilities.provenance,
            supersede=authority.capabilities.supersede,
        )
        self.last_external_error = ""

    async def recall(
        self,
        query: str,
        *,
        kinds: Iterable[str] = (),
        limit: int = 8,
    ) -> list[MemoryRecord]:
        external_recall = self.external.recall(query, kinds=kinds, limit=limit)
        if self.external_timeout is not None:
            external_recall = asyncio.wait_for(external_recall, timeout=self.external_timeout)
        authority_result, external_result = await asyncio.gather(
            self.authority.recall(query, kinds=kinds, limit=limit),
            external_recall,
            return_exceptions=True,
        )
        if isinstance(authority_result, BaseException):
            raise authority_result
        if isinstance(external_result, BaseException):
            self.last_external_error = f"{type(external_result).__name__}: {external_result}"
            external_records: list[MemoryRecord] = []
        else:
            self.last_external_error = ""
            external_records = external_result

        merged: list[MemoryRecord] = []
        seen: set[str] = set()
        for record in [*authority_result, *external_records]:
            key = " ".join(record.content.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(record)
            if len(merged) >= max(1, min(int(limit), 50)):
                break
        return merged

    async def retain(self, *, kind: str, content: str, **kwargs: Any) -> MemoryRecord:
        return await self.authority.retain(kind=kind, content=content, **kwargs)

    async def supersede(self, record_id: str, *, content: str, **kwargs: Any) -> MemoryRecord:
        return await self.authority.supersede(record_id, content=content, **kwargs)

    async def health(self) -> MemoryProviderHealth:
        authority_health, external_health = await asyncio.gather(
            self.authority.health(),
            self.external.health(),
        )
        state = "available" if authority_health.state == "available" else authority_health.state
        detail = (
            f"authority={authority_health.state}; external={external_health.state}; "
            f"{external_health.detail}"
        )
        return MemoryProviderHealth(state, detail, self.capabilities)
