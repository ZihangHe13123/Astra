"""Lossless local-image tiling and confined derived-image caching."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from agent.core.msg import ContentBlock

from .vision_policy import VisionPreprocessPolicy

Image.MAX_IMAGE_PIXELS = 100_000_000
MAX_CACHE_ARTIFACT_INLINE_BYTES = 41_943_040
MAX_TILES_PER_SOURCE = 256
MAX_REQUEST_IMAGES = 32
MAX_REQUEST_INLINE_BYTES = 41_943_040
MAX_DATA_URL_ENCODED_BYTES = MAX_REQUEST_INLINE_BYTES
MAX_DATA_URL_DECODED_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_CACHE_BYTES = 512 * 1024 * 1024

_CACHE_POLICY_VERSION = 2
_MAX_RECENT_EXPIRED_TILE_SETS = 1_024
_MAX_RECENT_RELEASED_REQUESTS = 1_024
_OVERVIEW_MAX_SIZE = (800, 800)
_PNG_DATA_URL_PREFIX_BYTES = len("data:image/png;base64,")
_SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})
_CACHE_DIRECTORY_PATTERN = re.compile(r"[0-9a-f]{64}")
_STAGING_DIRECTORY_PATTERN = re.compile(r"[0-9a-f]{32}")
_DATA_URL_PATTERN = re.compile(
    r"\Adata:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]*={0,2})\Z"
)
_MIME_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

logger = logging.getLogger(__name__)


class VisionPreprocessError(RuntimeError):
    """A recoverable failure that prevents safe original-pixel tiling."""


class VisionTileSelectionError(RuntimeError):
    """A sanitized request-binding or tile-selection failure."""


def tile_origins(length: int, tile_size: int, overlap: int) -> tuple[int, ...]:
    """Return deterministic starts, anchoring the final tile to the far edge."""
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (length, tile_size, overlap)):
        raise ValueError("tile geometry must use integers")
    if length <= 0 or tile_size <= 0:
        raise ValueError("image and tile dimensions must be positive")
    if overlap < 0 or overlap >= tile_size:
        raise ValueError("overlap must be non-negative and smaller than tile size")
    if length <= tile_size:
        return (0,)
    stride = tile_size - overlap
    values = list(range(0, max(1, length - tile_size + 1), stride))
    far_edge = length - tile_size
    if values[-1] != far_edge:
        values.append(far_edge)
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class TileDescriptor:
    tile_id: str
    bounds: tuple[int, int, int, int]
    path: Path
    inline_bytes: int
    sha256: str


@dataclass(frozen=True)
class TileSetBinding:
    tile_set_id: str
    session_id: str
    request_id: str
    source_ordinal: int
    normalized_size: tuple[int, int]
    overview_path: Path
    overview_sha256: str
    tiles: tuple[TileDescriptor, ...]


@dataclass(frozen=True)
class PreparedPath:
    source_path: Path
    direct: bool
    binding: TileSetBinding | None


@dataclass(frozen=True)
class PreparedVisionBundle:
    chat_blocks: tuple[ContentBlock, ...]
    storage_blocks: tuple[ContentBlock, ...]
    status: str
    protected: bool
    protected_local_images: int
    unprotected_external_images: int


@dataclass(frozen=True)
class TileSelection:
    data_urls: tuple[str, ...]
    labels: tuple[str, ...]
    unserved_ids: tuple[str, ...]
    remaining_images: int
    remaining_inline_bytes: int


@dataclass(frozen=True)
class _PreparedBundleRecord:
    bundle: PreparedVisionBundle
    added_images: int
    added_inline_bytes: int
    source_count_before: int
    source_count_after: int
    bindings: tuple[TileSetBinding, ...]


@dataclass
class _RequestBudget:
    max_images: int
    max_inline_bytes: int
    used_images: int = 0
    used_inline_bytes: int = 0
    source_count: int = 0
    bindings: list[TileSetBinding] = field(default_factory=list)
    prepared_bundles: dict[str, _PreparedBundleRecord] = field(default_factory=dict)
    occurrence_content_keys: dict[str, str] = field(default_factory=dict)
    tile_hashes: dict[tuple[str, str], str] = field(default_factory=dict)
    selected_ids: set[str] = field(default_factory=set)


@dataclass
class _RequestLockRecord:
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0
    released: bool = False


class VisionPreprocessor:
    """Prepare deterministic overview and direct-crop artifacts for one image."""

    def __init__(
        self,
        cache_root: Path | str,
        *,
        max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
        sweep_stale_staging: bool = True,
    ):
        if (
            isinstance(max_cache_bytes, bool)
            or not isinstance(max_cache_bytes, int)
            or max_cache_bytes < 0
        ):
            raise ValueError("max_cache_bytes must be a non-negative integer")
        self.cache_root = Path(cache_root).expanduser()
        self.max_cache_bytes = max_cache_bytes
        self._requests: dict[tuple[str, str], _RequestBudget] = {}
        self._bindings: dict[str, TileSetBinding] = {}
        self._expired_tile_sets: OrderedDict[str, None] = OrderedDict()
        self._released_requests: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._state_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._request_locks: dict[tuple[str, str], _RequestLockRecord] = {}
        self._pending_staging_paths: set[Path] = set()
        self._cleanup_unhealthy = False
        if sweep_stale_staging:
            try:
                self._sweep_stale_staging()
            except VisionPreprocessError:
                self._cleanup_unhealthy = True
                logger.warning("Stale inline image staging cleanup did not complete safely.")

    def prepare_blocks(
        self,
        blocks: list[ContentBlock] | tuple[ContentBlock, ...],
        policy: VisionPreprocessPolicy,
        enabled: bool,
        session_id: str,
        request_id: str,
        *,
        occurrence_id: str | None = None,
    ) -> PreparedVisionBundle:
        """Prepare one bounded request bundle while preserving storage content."""
        key = (str(session_id), str(request_id))
        with self._request_guard(key) as request_record, self._cache_lock:
            if request_record.released:
                raise self._error("The vision tile request has expired")
            self._retry_staging_cleanup_health()
            return self._prepare_blocks_locked(
                blocks,
                policy,
                enabled,
                session_id,
                request_id,
                occurrence_id=occurrence_id,
            )

    def _prepare_blocks_locked(
        self,
        blocks: list[ContentBlock] | tuple[ContentBlock, ...],
        policy: VisionPreprocessPolicy,
        enabled: bool,
        session_id: str,
        request_id: str,
        *,
        occurrence_id: str | None = None,
    ) -> PreparedVisionBundle:
        key = (str(session_id), str(request_id))
        with self._state_lock:
            if key in self._released_requests:
                raise self._error("The vision tile request has expired")
        original = tuple(blocks)
        image_indexes = [index for index, block in enumerate(original) if block.type == "image_url"]
        if not enabled:
            return PreparedVisionBundle(
                chat_blocks=original,
                storage_blocks=original,
                status="Vision tiles are disabled; image content is unchanged.",
                protected=False,
                protected_local_images=0,
                unprotected_external_images=0,
            )

        max_images = min(policy.max_images_per_request, MAX_REQUEST_IMAGES)
        max_inline_bytes = min(policy.max_inline_body_bytes, MAX_REQUEST_INLINE_BYTES)
        if len(image_indexes) > max_images:
            raise self._error(
                f"The request contains more than {max_images} images; split the request into fewer images"
            )
        if not image_indexes:
            return PreparedVisionBundle(
                chat_blocks=original,
                storage_blocks=original,
                status="No image content required vision tiling.",
                protected=False,
                protected_local_images=0,
                unprotected_external_images=0,
            )

        with self._state_lock:
            state = self._requests.get(key)
        if state is None:
            state = _RequestBudget(max_images=max_images, max_inline_bytes=max_inline_bytes)
        else:
            max_images = min(max_images, state.max_images)
            max_inline_bytes = min(max_inline_bytes, state.max_inline_bytes)
        content_key = self._bundle_key(original, policy)
        normalized_occurrence = ""
        if occurrence_id is not None:
            normalized_occurrence = str(occurrence_id).strip()
            if not normalized_occurrence:
                raise ValueError("occurrence_id must be a non-empty string when provided")
            previous_content_key = state.occurrence_content_keys.get(normalized_occurrence)
            if previous_content_key is not None and previous_content_key != content_key:
                raise self._error("An image attachment occurrence changed content during retry")
        bundle_key = (
            hashlib.sha256(f"{normalized_occurrence}\0{content_key}".encode()).hexdigest()
            if normalized_occurrence
            else content_key
        )
        previous_record = state.prepared_bundles.get(bundle_key)
        if previous_record is not None:
            with self._state_lock:
                bindings_are_active = all(
                    binding.tile_set_id in self._bindings for binding in previous_record.bindings
                )
            if bindings_are_active:
                return previous_record.bundle
            self._reset_expired_bundle(state, bundle_key, previous_record)

        prepared_by_index: dict[int, PreparedPath] = {}
        large_indexes: list[int] = []
        charged_indexes: list[int] = []
        source_count_before = state.source_count
        next_ordinal = source_count_before
        external_count = 0

        for index in image_indexes:
            block = original[index]
            next_ordinal += 1
            charged_indexes.append(index)
            source_path: Path | None = None
            verified_source_bytes: bytes | None = None
            materialized_path: Path | None = None
            url = str(block.data.get("url", "")).strip()
            if not url.startswith("data:"):
                external_count += 1
                continue
            _mime_type, decoded = self._validated_data_url(url)
            source_path = self._matching_local_source(block, decoded)
            if source_path is not None:
                verified_source_bytes = decoded
            else:
                materialized_path = self._materialize_data_url(url, validated=(_mime_type, decoded))
                source_path = materialized_path
            try:
                prepared = self.prepare_path(
                    source_path,
                    policy,
                    str(session_id),
                    str(request_id),
                    next_ordinal,
                    _verified_source_bytes=verified_source_bytes,
                )
            except Exception:
                if materialized_path is not None:
                    self._cleanup_materialized_path(materialized_path, fail_closed=False)
                raise
            if materialized_path is not None:
                self._cleanup_materialized_path(materialized_path, fail_closed=True)
            prepared_by_index[index] = prepared
            if prepared.binding is not None:
                large_indexes.append(index)

        if state.used_images + len(charged_indexes) > max_images:
            raise self._error(
                f"The request would exceed the {max_images}-image limit; split the request into fewer images"
            )

        direct_indexes = [index for index in charged_indexes if index not in large_indexes]
        direct_inline_bytes = sum(self._block_inline_bytes(original[index]) for index in direct_indexes)
        if state.used_inline_bytes + direct_inline_bytes > max_inline_bytes and not large_indexes:
            raise self._error("The inline image content exceeds the request budget; split the request")

        captured_overviews: dict[int, str] = {}
        for index in large_indexes:
            binding = prepared_by_index[index].binding
            assert binding is not None
            captured_overviews[index] = self._capture_artifact_data_url(
                binding.overview_path,
                expected_sha256=binding.overview_sha256,
                expected_size=self._overview_size(binding.normalized_size),
            )

        candidate_images = state.used_images + len(direct_indexes)
        candidate_bytes = state.used_inline_bytes + direct_inline_bytes
        for index in large_indexes:
            binding = prepared_by_index[index].binding
            assert binding is not None
            candidate_images += 1 + len(binding.tiles)
            candidate_bytes += len(captured_overviews[index].encode("ascii"))
            candidate_bytes += sum(tile.inline_bytes for tile in binding.tiles)
        potential_one_pass = bool(large_indexes) and (
            candidate_images <= max_images and candidate_bytes <= max_inline_bytes
        )
        captured_tiles: dict[int, tuple[str, ...]] = {}
        if potential_one_pass:
            for index in large_indexes:
                binding = prepared_by_index[index].binding
                assert binding is not None
                captured_tiles[index] = tuple(
                    self._capture_artifact_data_url(
                        tile.path,
                        expected_sha256=tile.sha256,
                        expected_size=(
                            tile.bounds[2] - tile.bounds[0],
                            tile.bounds[3] - tile.bounds[1],
                        ),
                        expected_inline_bytes=tile.inline_bytes,
                    )
                    for tile in binding.tiles
                )
        exact_candidate_bytes = (
            state.used_inline_bytes
            + direct_inline_bytes
            + sum(len(url.encode("ascii")) for url in captured_overviews.values())
            + sum(
                len(url.encode("ascii"))
                for urls in captured_tiles.values()
                for url in urls
            )
        )
        one_pass = potential_one_pass and exact_candidate_bytes <= max_inline_bytes

        chat_blocks: list[ContentBlock] = []
        new_bindings: list[TileSetBinding] = []
        for index, block in enumerate(original):
            prepared = prepared_by_index.get(index)
            if prepared is None or prepared.binding is None:
                chat_blocks.append(block)
                continue
            binding = prepared.binding
            if one_pass:
                chat_blocks.extend(
                    self._one_pass_blocks(
                        binding,
                        captured_overviews[index],
                        captured_tiles[index],
                    )
                )
            else:
                chat_blocks.extend(
                    self._overflow_blocks(binding, captured_overviews[index])
                )
                new_bindings.append(binding)

        added_images = sum(block.type == "image_url" for block in chat_blocks)
        added_inline_bytes = sum(
            self._block_inline_bytes(block)
            for block in chat_blocks
            if block.type == "image_url"
        )

        if state.used_images + added_images > max_images:
            raise self._error(
                f"The request would exceed the {max_images}-image limit; split the request into fewer images"
            )
        if state.used_inline_bytes + added_inline_bytes > max_inline_bytes:
            raise self._error("The encoded image content exceeds the request budget; split the request")

        new_tile_hashes: dict[tuple[str, str], str] = {}
        for binding in new_bindings:
            for tile in binding.tiles:
                new_tile_hashes[(binding.tile_set_id, tile.tile_id)] = tile.sha256

        with self._state_lock:
            state.max_images = max_images
            state.max_inline_bytes = max_inline_bytes
            state.used_images += added_images
            state.used_inline_bytes += added_inline_bytes
            state.source_count = next_ordinal
            for binding in new_bindings:
                state.bindings.append(binding)
                self._bindings[binding.tile_set_id] = binding
                self._expired_tile_sets.pop(binding.tile_set_id, None)
            state.tile_hashes.update(new_tile_hashes)

        if external_count and large_indexes:
            mode = (
                "annotated overview and original-pixel detail tiles"
                if one_pass
                else "annotated overview for bounded original-pixel detail selection"
            )
            status = (
                f"Prepared {len(large_indexes)} protected local image(s) with {mode}; "
                f"{external_count} unprotected external image(s) remain provider-managed."
            )
        elif one_pass:
            status = (
                f"Prepared {len(large_indexes)} local image(s) with annotated overview and "
                "original-pixel detail tiles. Neighboring tiles overlap; do not double-count overlap."
            )
        elif large_indexes:
            status = (
                f"Prepared {len(large_indexes)} local image overview(s) for bounded original-pixel "
                "detail selection with read_image_tiles."
            )
        elif external_count:
            status = "The external URL image content is unchanged and is not protected by local vision tiling."
        else:
            status = "Local images are within the direct-send dimensions and are unchanged."
        bundle = PreparedVisionBundle(
            chat_blocks=tuple(chat_blocks),
            storage_blocks=original,
            status=status,
            protected=bool(large_indexes) and external_count == 0,
            protected_local_images=len(large_indexes),
            unprotected_external_images=external_count,
        )
        with self._state_lock:
            state.prepared_bundles[bundle_key] = _PreparedBundleRecord(
                bundle=bundle,
                added_images=added_images,
                added_inline_bytes=added_inline_bytes,
                source_count_before=source_count_before,
                source_count_after=next_ordinal,
                bindings=tuple(new_bindings),
            )
            if normalized_occurrence:
                state.occurrence_content_keys[normalized_occurrence] = content_key
            self._requests[key] = state
        return bundle

    def bindings_for(self, session_id: str, request_id: str) -> tuple[TileSetBinding, ...]:
        """Return active bindings for diagnostics and request-local integration."""
        key = (str(session_id), str(request_id))
        with self._request_guard(key), self._state_lock:
            state = self._requests.get(key)
            return tuple(state.bindings) if state is not None else ()

    def validate_provider_prompt(
        self,
        messages: list[dict],
        policy: VisionPreprocessPolicy,
    ) -> tuple[int, int]:
        """Fail closed when the exact accumulated provider prompt exceeds vision limits."""
        max_images = min(policy.max_images_per_request, MAX_REQUEST_IMAGES)
        max_inline_bytes = min(policy.max_inline_body_bytes, MAX_REQUEST_INLINE_BYTES)
        image_count = 0
        inline_bytes = 0
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                image_count += 1
                image_value = part.get("image_url")
                url = (
                    image_value.get("url", "")
                    if isinstance(image_value, dict)
                    else image_value
                )
                if isinstance(url, str) and url.startswith("data:"):
                    try:
                        inline_bytes += len(url.encode("ascii"))
                    except UnicodeEncodeError as exc:
                        raise self._error("Provider image data is not valid ASCII") from exc
                if image_count > max_images:
                    raise self._error(
                        f"The accumulated provider prompt exceeds the {max_images}-image limit"
                    )
                if inline_bytes > max_inline_bytes:
                    raise self._error(
                        "The accumulated provider prompt exceeds the image byte budget"
                    )
        return image_count, inline_bytes

    def select_tiles(
        self,
        tile_set_id: str,
        tile_ids: list[str],
        session_id: str,
        request_id: str,
    ) -> TileSelection:
        """Resolve logical IDs through an active binding and enforce remaining budgets."""
        if not isinstance(tile_set_id, str) or not tile_set_id.strip():
            raise VisionTileSelectionError("The tile set ID must be a non-empty string.")
        opaque_id = tile_set_id.strip()
        if not isinstance(tile_ids, list) or not tile_ids:
            raise VisionTileSelectionError("At least one tile ID is required.")
        if any(not isinstance(tile_id, str) or not tile_id.strip() for tile_id in tile_ids):
            raise VisionTileSelectionError("Tile IDs must be non-empty strings.")
        normalized_ids = [tile_id.strip() for tile_id in tile_ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise VisionTileSelectionError("The tile request contains duplicate IDs.")

        with self._state_lock:
            binding = self._bindings.get(opaque_id)
        if binding is None:
            with self._state_lock:
                expired = opaque_id in self._expired_tile_sets
            if expired:
                raise VisionTileSelectionError("The tile set has expired for this request.")
            raise VisionTileSelectionError("The tile set ID is unknown.")
        key = (binding.session_id, binding.request_id)
        with self._request_guard(key), self._cache_lock:
            return self._select_tiles_locked(
                opaque_id,
                normalized_ids,
                session_id,
                request_id,
            )

    def _select_tiles_locked(
        self,
        opaque_id: str,
        normalized_ids: list[str],
        session_id: str,
        request_id: str,
    ) -> TileSelection:
        with self._state_lock:
            binding = self._bindings.get(opaque_id)
            expired = opaque_id in self._expired_tile_sets
        if binding is None:
            if expired:
                raise VisionTileSelectionError("The tile set has expired for this request.")
            raise VisionTileSelectionError("The tile set ID is unknown.")
        if binding.session_id != str(session_id):
            raise VisionTileSelectionError("The tile set belongs to a different session.")
        if binding.request_id != str(request_id):
            raise VisionTileSelectionError("The tile set belongs to a different request.")

        with self._state_lock:
            state = self._requests.get((binding.session_id, binding.request_id))
        if state is None or binding not in state.bindings:
            raise VisionTileSelectionError("The tile set has expired for this request.")
        tiles_by_id = {tile.tile_id: tile for tile in binding.tiles}
        unknown = [tile_id for tile_id in normalized_ids if tile_id not in tiles_by_id]
        if unknown:
            raise VisionTileSelectionError("The tile request contains an unknown tile ID.")
        if any(tile_id in state.selected_ids for tile_id in normalized_ids):
            raise VisionTileSelectionError("The tile request contains a duplicate already-selected ID.")

        selected: list[tuple[TileDescriptor, str]] = []
        unserved: list[str] = []
        remaining_images = max(0, state.max_images - state.used_images)
        remaining_bytes = max(0, state.max_inline_bytes - state.used_inline_bytes)
        for tile_id in normalized_ids:
            tile = tiles_by_id[tile_id]
            data_url = self._verified_tile_data_url(binding, tile, state)
            if data_url is None:
                self._expire_binding(binding, state)
                raise VisionTileSelectionError("The tile set has expired because its cache artifact changed.")
            attached_bytes = len(data_url.encode("ascii"))
            if remaining_images <= 0 or attached_bytes > remaining_bytes:
                unserved.append(tile_id)
                continue
            selected.append((tile, data_url))
            remaining_images -= 1
            remaining_bytes -= attached_bytes

        for tile, _data_url in selected:
            state.selected_ids.add(tile.tile_id)
        state.used_images += len(selected)
        state.used_inline_bytes += sum(
            len(data_url.encode("ascii")) for _tile, data_url in selected
        )
        return TileSelection(
            data_urls=tuple(data_url for _tile, data_url in selected),
            labels=tuple(self._tile_label(binding, tile) for tile, _data_url in selected),
            unserved_ids=tuple(unserved),
            remaining_images=max(0, state.max_images - state.used_images),
            remaining_inline_bytes=max(0, state.max_inline_bytes - state.used_inline_bytes),
        )

    def release_request(self, session_id: str, request_id: str) -> None:
        """Expire every opaque tile binding owned by one completed request."""
        key = (str(session_id), str(request_id))
        with self._request_guard(key) as request_record, self._cache_lock, self._state_lock:
            request_record.released = True
            state = self._requests.pop(key, None)
            if state is not None:
                self._remember_released_request(key)
                for binding in state.bindings:
                    self._bindings.pop(binding.tile_set_id, None)
                    self._remember_expired_tile_set(binding.tile_set_id)
        with self._cache_lock:
            self._retry_staging_cleanup_health()
            try:
                self.prune_cache(max_bytes=self.max_cache_bytes)
            except VisionPreprocessError:
                logger.warning("Automatic vision cache pruning failed after request release.")

    @contextmanager
    def _request_guard(self, key: tuple[str, str]) -> Iterator[_RequestLockRecord]:
        with self._state_lock:
            record = self._request_locks.get(key)
            if record is None:
                record = _RequestLockRecord()
                self._request_locks[key] = record
            record.users += 1
        try:
            with record.lock:
                yield record
        finally:
            with self._state_lock:
                record.users -= 1
                if (
                    record.users == 0
                    and key not in self._requests
                    and self._request_locks.get(key) is record
                ):
                    self._request_locks.pop(key, None)

    def _remember_released_request(self, key: tuple[str, str]) -> None:
        self._released_requests.pop(key, None)
        self._released_requests[key] = None
        while len(self._released_requests) > _MAX_RECENT_RELEASED_REQUESTS:
            self._released_requests.popitem(last=False)

    def _remember_expired_tile_set(self, tile_set_id: str) -> None:
        self._expired_tile_sets.pop(tile_set_id, None)
        self._expired_tile_sets[tile_set_id] = None
        while len(self._expired_tile_sets) > _MAX_RECENT_EXPIRED_TILE_SETS:
            self._expired_tile_sets.popitem(last=False)

    @staticmethod
    def _bundle_key(
        blocks: tuple[ContentBlock, ...],
        policy: VisionPreprocessPolicy,
    ) -> str:
        payload = {
            "policy": asdict(policy),
            "blocks": [{"type": block.type, "data": block.data} for block in blocks],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _verified_tile_data_url(
        self,
        binding: TileSetBinding,
        tile: TileDescriptor,
        state: _RequestBudget,
    ) -> str | None:
        expected_hash = state.tile_hashes.get((binding.tile_set_id, tile.tile_id))
        if (
            expected_hash is None
            or expected_hash != tile.sha256
            or not self._is_regular_file(tile.path)
        ):
            return None
        try:
            return self._capture_artifact_data_url(
                tile.path,
                expected_sha256=expected_hash,
                expected_size=(
                    tile.bounds[2] - tile.bounds[0],
                    tile.bounds[3] - tile.bounds[1],
                ),
                expected_inline_bytes=tile.inline_bytes,
            )
        except VisionPreprocessError:
            return None

    def _expire_binding(self, binding: TileSetBinding, state: _RequestBudget) -> None:
        with self._state_lock:
            state.bindings = [item for item in state.bindings if item.tile_set_id != binding.tile_set_id]
            self._bindings.pop(binding.tile_set_id, None)
            self._remember_expired_tile_set(binding.tile_set_id)
            for tile in binding.tiles:
                state.tile_hashes.pop((binding.tile_set_id, tile.tile_id), None)

    def _reset_expired_bundle(
        self,
        state: _RequestBudget,
        bundle_key: str,
        record: _PreparedBundleRecord,
    ) -> None:
        if state.source_count != record.source_count_after:
            raise self._error(
                "An earlier vision tile bundle expired after later request images were prepared; retry as a new request"
            )
        state.used_images = max(0, state.used_images - record.added_images)
        state.used_inline_bytes = max(0, state.used_inline_bytes - record.added_inline_bytes)
        state.source_count = record.source_count_before
        for binding in record.bindings:
            self._expire_binding(binding, state)
        state.prepared_bundles.pop(bundle_key, None)

    @staticmethod
    def _matching_local_source(block: ContentBlock, supplied: bytes) -> Path | None:
        source_path = str(block.data.get("source_path", "")).strip()
        if not source_path:
            return None
        try:
            candidate = Path(source_path).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            candidate = candidate.resolve()
            if not VisionPreprocessor._is_regular_file(candidate):
                return None
            local = candidate.read_bytes()
        except (OSError, RuntimeError):
            return None
        return candidate if len(local) == len(supplied) and local == supplied else None

    @staticmethod
    def _validated_data_url(url: str) -> tuple[str, bytes]:
        """Return strict MIME-checked bytes without consulting caller path metadata."""
        try:
            encoded_url = url.encode("ascii")
        except UnicodeEncodeError as exc:
            raise VisionPreprocessor._error("Inline image data is not valid base64") from exc
        if len(encoded_url) > MAX_DATA_URL_ENCODED_BYTES:
            raise VisionPreprocessor._error("Inline image data exceeds the encoded size limit")

        match = _DATA_URL_PATTERN.fullmatch(url)
        if match is None:
            raise VisionPreprocessor._error(
                "Inline image data must use a supported image MIME type and strict base64"
            )
        mime_type, payload = match.groups()
        padding = len(payload) - len(payload.rstrip("="))
        decoded_size = (len(payload) // 4) * 3 - padding
        if decoded_size > MAX_DATA_URL_DECODED_BYTES:
            raise VisionPreprocessor._error("Inline image data exceeds the decoded size limit")
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise VisionPreprocessor._error("Inline image data is not valid base64") from exc
        if not decoded:
            raise VisionPreprocessor._error("Inline image data is empty")
        if len(decoded) > MAX_DATA_URL_DECODED_BYTES:
            raise VisionPreprocessor._error("Inline image data exceeds the decoded size limit")

        expected_format = _MIME_FORMATS[mime_type]
        try:
            with Image.open(io.BytesIO(decoded), formats=list(_SUPPORTED_FORMATS)) as opened:
                if (opened.format or "").upper() != expected_format:
                    raise VisionPreprocessor._error(
                        "Inline image MIME type does not match the decoded image"
                    )
                opened.verify()
        except VisionPreprocessError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise VisionPreprocessor._error("Unable to decode inline image data") from exc
        return mime_type, decoded

    def _materialize_data_url(
        self,
        url: str,
        *,
        validated: tuple[str, bytes] | None = None,
    ) -> Path:
        """Validate and stage a bare inline image without trusting caller paths."""
        self._require_healthy_staging_cleanup()
        mime_type, decoded = validated or self._validated_data_url(url)

        materialized = self._safe_target(
            f".staging/{uuid.uuid4().hex}/source{_MIME_EXTENSIONS[mime_type]}",
            create_root=True,
        )
        try:
            staging_root = materialized.parent.parent
            self._assert_no_symlink(staging_root)
            staging_root.mkdir(parents=False, exist_ok=True, mode=0o700)
            self._assert_no_symlink(staging_root)
            self._ensure_private_directory(staging_root)
            materialized.parent.mkdir(parents=False, mode=0o700)
            self._assert_no_symlink(materialized.parent)
            self._ensure_private_directory(materialized.parent)
            self._atomic_write(materialized, decoded)
            return materialized
        except VisionPreprocessError:
            self._cleanup_materialized_path(materialized, fail_closed=False)
            raise
        except OSError as exc:
            self._cleanup_materialized_path(materialized, fail_closed=False)
            raise self._error("Unable to materialize inline image data safely") from exc

    def _cleanup_materialized_path(self, path: Path, *, fail_closed: bool) -> bool:
        try:
            root = self._resolved_root(create=False)
            staging_root = root / ".staging"
            if (
                path.is_symlink()
                or staging_root.is_symlink()
                or path.parent.is_symlink()
                or not path.resolve(strict=False).is_relative_to(staging_root)
            ):
                raise self._error("Inline image cleanup target escaped the staging root")
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except FileNotFoundError:
                pass
            try:
                staging_root.rmdir()
            except OSError:
                # Other confined staging entries may still be active or pending.
                pass
        except (OSError, RuntimeError, VisionPreprocessError) as exc:
            with self._state_lock:
                self._pending_staging_paths.add(path)
                self._cleanup_unhealthy = True
            if fail_closed:
                raise self._error("Unable to remove temporary inline image data safely") from exc
            logger.warning("Temporary inline image cleanup did not complete safely.")
            return False
        with self._state_lock:
            self._pending_staging_paths.discard(path)
            if not self._pending_staging_paths:
                self._cleanup_unhealthy = False
        return True

    def _retry_pending_staging_cleanup(self) -> bool:
        with self._state_lock:
            pending = tuple(self._pending_staging_paths)
        for path in pending:
            self._cleanup_materialized_path(path, fail_closed=False)
        with self._state_lock:
            return not self._pending_staging_paths

    def _retry_staging_cleanup_health(self) -> None:
        if not self._retry_pending_staging_cleanup():
            return
        with self._state_lock:
            retry_startup_sweep = self._cleanup_unhealthy
        if not retry_startup_sweep:
            return
        try:
            self._sweep_stale_staging()
        except VisionPreprocessError:
            return
        with self._state_lock:
            self._cleanup_unhealthy = False

    def _require_healthy_staging_cleanup(self) -> None:
        self._retry_staging_cleanup_health()
        with self._state_lock:
            unhealthy = self._cleanup_unhealthy or bool(self._pending_staging_paths)
        if unhealthy:
            raise self._error(
                "Inline image staging cleanup is unhealthy; retry after cleanup succeeds"
            )

    def _sweep_stale_staging(self) -> None:
        """Remove only confined, non-symlink staging directories from an earlier process."""
        try:
            self.cache_root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise self._error("Unable to inspect stale inline image staging safely") from exc
        root = self._resolved_root(create=False)
        staging_root = root / ".staging"
        try:
            metadata = staging_root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise self._error("Unable to inspect stale inline image staging safely") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise self._error("Stale inline image staging is not a confined directory")
        if not staging_root.resolve().is_relative_to(root):
            raise self._error("Stale inline image staging escaped the cache root")
        try:
            candidates = tuple(staging_root.iterdir())
            for candidate in candidates:
                candidate_metadata = candidate.stat(follow_symlinks=False)
                if (
                    not _STAGING_DIRECTORY_PATTERN.fullmatch(candidate.name)
                    or stat.S_ISLNK(candidate_metadata.st_mode)
                    or not stat.S_ISDIR(candidate_metadata.st_mode)
                    or not candidate.resolve().is_relative_to(staging_root)
                ):
                    raise self._error("Stale inline image staging contains an unsafe entry")
                pending = [candidate]
                while pending:
                    directory = pending.pop()
                    for entry in directory.iterdir():
                        entry_metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(entry_metadata.st_mode):
                            raise self._error("Stale inline image staging contains a symlink")
                        if stat.S_ISDIR(entry_metadata.st_mode):
                            pending.append(entry)
                        elif not stat.S_ISREG(entry_metadata.st_mode):
                            raise self._error("Stale inline image staging contains an unsafe entry")
                        if not entry.resolve().is_relative_to(staging_root):
                            raise self._error("Stale inline image staging escaped the cache root")
                shutil.rmtree(candidate)
            staging_root.rmdir()
        except VisionPreprocessError:
            raise
        except (OSError, RuntimeError) as exc:
            raise self._error("Unable to remove stale inline image staging safely") from exc

    @staticmethod
    def _block_inline_bytes(block: ContentBlock) -> int:
        url = str(block.data.get("url", ""))
        return len(url.encode()) if url.startswith("data:") else 0

    @classmethod
    def _one_pass_blocks(
        cls,
        binding: TileSetBinding,
        overview_data_url: str,
        tile_data_urls: tuple[str, ...],
    ) -> list[ContentBlock]:
        if len(tile_data_urls) != len(binding.tiles):
            raise cls._error("Captured vision tile count did not match its binding")
        blocks = [
            ContentBlock.text(
                f"Image {binding.source_ordinal} was preserved as an annotated locator overview followed by "
                "row-major original-pixel detail tiles. Neighboring tiles overlap; do not double-count objects "
                "visible in an overlap. "
                f"Each overview label rNcM maps to tile_id image{binding.source_ordinal}-rNcM."
            ),
            ContentBlock.image_url(overview_data_url, detail="original"),
        ]
        for tile, data_url in zip(binding.tiles, tile_data_urls):
            blocks.append(ContentBlock.text(cls._tile_label(binding, tile)))
            blocks.append(ContentBlock.image_url(data_url, detail="original"))
        return blocks

    @classmethod
    def _overflow_blocks(
        cls,
        binding: TileSetBinding,
        overview_data_url: str,
    ) -> list[ContentBlock]:
        manifest = ", ".join(
            f"{tile.tile_id}=[{tile.bounds[0]},{tile.bounds[2]})x[{tile.bounds[1]},{tile.bounds[3]})"
            for tile in binding.tiles
        )
        return [
            ContentBlock.text(
                "This annotated overview is a locator only. For precise OCR, small-text, local-object, or "
                "exact-position claims, call read_image_tiles before answering. Do not invent paths or IDs. "
                f"Use tile_set_id={binding.tile_set_id} with one or more listed tile_ids. "
                f"Image {binding.source_ordinal} normalized size={binding.normalized_size[0]}x"
                f"{binding.normalized_size[1]}; neighboring tiles overlap, so do not double-count overlap. "
                f"For example, overview label r1c1 maps to tile_id image{binding.source_ordinal}-r1c1. "
                f"Manifest: {manifest}"
            ),
            ContentBlock.image_url(overview_data_url, detail="original"),
        ]

    @staticmethod
    def _tile_label(binding: TileSetBinding, tile: TileDescriptor) -> str:
        x0, y0, x1, y1 = tile.bounds
        return (
            f"{tile.tile_id} — source image {binding.source_ordinal}, normalized "
            f"{binding.normalized_size[0]}x{binding.normalized_size[1]}; "
            f"bounds [{x0},{x1}) x [{y0},{y1}); neighboring tiles overlap. "
            "Do not double-count overlap."
        )

    def prepare_path(
        self,
        source_path: Path | str,
        policy: VisionPreprocessPolicy,
        session_id: str,
        request_id: str,
        source_ordinal: int,
        *,
        _verified_source_bytes: bytes | None = None,
    ) -> PreparedPath:
        with self._cache_lock:
            return self._prepare_path_locked(
                source_path,
                policy,
                session_id,
                request_id,
                source_ordinal,
                _verified_source_bytes=_verified_source_bytes,
            )

    def _prepare_path_locked(
        self,
        source_path: Path | str,
        policy: VisionPreprocessPolicy,
        session_id: str,
        request_id: str,
        source_ordinal: int,
        *,
        _verified_source_bytes: bytes | None = None,
    ) -> PreparedPath:
        try:
            source = Path(source_path).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise self._error("Unable to access image source") from exc
        if not source.is_file():
            raise self._error("Unable to decode image because the source is not a regular file")
        if isinstance(source_ordinal, bool) or not isinstance(source_ordinal, int) or source_ordinal <= 0:
            raise ValueError("source_ordinal must be a positive integer")

        normalized, animated = self._decode_normalized(
            source,
            verified_source_bytes=_verified_source_bytes,
        )
        width, height = normalized.size
        if (
            width <= policy.tile_width
            and height <= policy.tile_height
            and width * height <= policy.tile_width * policy.tile_height
        ):
            return PreparedPath(source_path=source, direct=True, binding=None)
        if animated:
            raise self._error("A large animated image cannot be tiled without dropping animation")
        if self._required_tile_count(normalized.size, policy) > MAX_TILES_PER_SOURCE:
            raise self._error(
                f"Image geometry requires more than {MAX_TILES_PER_SOURCE} detail tiles"
            )

        normalized = self._png_compatible(normalized)
        normalized_png = self._png_bytes(normalized)
        cache_key = self._cache_key(normalized_png, policy)
        try:
            derived_dir = self._safe_target(cache_key, create_root=True)
            self._assert_no_symlink(derived_dir)
            derived_dir.mkdir(parents=False, exist_ok=True, mode=0o700)
            self._assert_no_symlink(derived_dir)
            self._ensure_private_directory(derived_dir)

            manifest_path = self._safe_target(f"{cache_key}/manifest.json", create_root=True)
            if self._is_regular_file(manifest_path):
                self._ensure_private_file(manifest_path)
            manifest = self._read_manifest(manifest_path)
            binding = self._binding_from_manifest(
                manifest,
                derived_dir=derived_dir,
                normalized=normalized,
                policy=policy,
                session_id=session_id,
                request_id=request_id,
                source_ordinal=source_ordinal,
                expected_size=normalized.size,
            )
            if binding is not None:
                derived_dir.touch(exist_ok=True)
                return PreparedPath(source_path=source, direct=False, binding=binding)

            manifest = self._write_artifacts(normalized, policy, derived_dir)
            self._atomic_write(
                manifest_path,
                (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            )
            binding = self._binding_from_manifest(
                manifest,
                derived_dir=derived_dir,
                normalized=normalized,
                policy=policy,
                session_id=session_id,
                request_id=request_id,
                source_ordinal=source_ordinal,
                expected_size=normalized.size,
            )
            if binding is None:
                raise self._error("Generated vision cache manifest failed validation")
            return PreparedPath(source_path=source, direct=False, binding=binding)
        except VisionPreprocessError:
            raise
        except OSError as exc:
            raise self._error("Unable to write the confined vision cache") from exc

    def prune_cache(self, max_bytes: int = DEFAULT_MAX_CACHE_BYTES) -> int:
        """Remove least-recently-used derived directories until under quota."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        with self._cache_lock:
            try:
                return self._prune_cache(max_bytes)
            except VisionPreprocessError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._error("Unable to prune the vision cache safely") from exc

    def _prune_cache(self, max_bytes: int) -> int:
        try:
            self.cache_root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return 0
        root = self._resolved_root(create=False)
        with self._state_lock:
            active_directories = {
                binding.overview_path.parent.resolve(strict=False)
                for request in self._requests.values()
                for binding in request.bindings
            }
        entries: list[tuple[int, str, Path, int, bool]] = []
        for candidate in root.iterdir():
            if not _CACHE_DIRECTORY_PATTERN.fullmatch(candidate.name):
                continue
            metadata = candidate.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            size = self._directory_size(candidate)
            entries.append(
                (metadata.st_mtime_ns, candidate.name, candidate, size, resolved in active_directories)
            )

        total = sum(entry[3] for entry in entries)
        removed = 0
        for _mtime, _name, candidate, size, pinned in sorted(entries):
            if total <= max_bytes:
                break
            if pinned:
                continue
            self._assert_no_symlink(candidate)
            if not candidate.resolve().is_relative_to(root):
                raise self._error("Vision cache cleanup target escaped the cache root")
            shutil.rmtree(candidate)
            removed += size
            total -= size
        return removed

    def _decode_normalized(
        self,
        source: Path,
        *,
        verified_source_bytes: bytes | None = None,
    ) -> tuple[Image.Image, bool]:
        try:
            image_source: Path | io.BytesIO = (
                io.BytesIO(verified_source_bytes)
                if verified_source_bytes is not None
                else source
            )
            with Image.open(image_source) as opened:
                if (opened.format or "").upper() not in _SUPPORTED_FORMATS:
                    raise self._error(f"Unable to decode supported image format: {opened.format or 'unknown'}")
                self._enforce_pixel_limit(opened.size)
                animated = bool(
                    getattr(opened, "is_animated", False)
                    or getattr(opened, "n_frames", 1) > 1
                )
                normalized = ImageOps.exif_transpose(opened)
                self._enforce_pixel_limit(normalized.size)
                normalized.load()
                return normalized.copy(), animated
        except VisionPreprocessError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise self._error("Image exceeds the decoded pixel limit") from exc
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise self._error("Unable to decode image data") from exc

    @staticmethod
    def _enforce_pixel_limit(size: tuple[int, int]) -> None:
        limit = Image.MAX_IMAGE_PIXELS
        if limit is not None and size[0] * size[1] > limit:
            raise VisionPreprocessor._error("Image exceeds the decoded pixel limit")

    @staticmethod
    def _png_compatible(image: Image.Image) -> Image.Image:
        if image.mode in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16", "I;16B", "I;16L"}:
            return image
        return image.convert("RGBA" if "A" in image.getbands() else "RGB")

    @staticmethod
    def _png_bytes(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    @staticmethod
    def _cache_key(normalized_png: bytes, policy: VisionPreprocessPolicy) -> str:
        payload = {
            "policy_version": _CACHE_POLICY_VERSION,
            "policy": asdict(policy),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256()
        digest.update(normalized_png)
        digest.update(canonical)
        return digest.hexdigest()

    def _write_artifacts(
        self,
        normalized: Image.Image,
        policy: VisionPreprocessPolicy,
        derived_dir: Path,
    ) -> dict[str, object]:
        records: list[dict[str, object]] = []
        bounds_by_id: list[tuple[str, tuple[int, int, int, int]]] = []

        for logical_id, bounds, filename in self._expected_tiles(normalized.size, policy):
            target = self._safe_child(derived_dir, filename)
            encoded = self._png_bytes(normalized.crop(bounds))
            self._atomic_write(target, encoded)
            records.append(
                {
                    "tile_id": logical_id,
                    "bounds": list(bounds),
                    "filename": filename,
                    "inline_bytes": self._data_url_length(len(encoded)),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
            bounds_by_id.append((logical_id, bounds))

        overview = self._annotated_overview(normalized, bounds_by_id)
        overview_path = self._safe_child(derived_dir, "overview.png")
        overview_bytes = self._png_bytes(overview)
        self._atomic_write(overview_path, overview_bytes)
        return {
            "normalized_size": list(normalized.size),
            "policy_version": _CACHE_POLICY_VERSION,
            "overview": overview_path.name,
            "overview_sha256": hashlib.sha256(overview_bytes).hexdigest(),
            "tiles": records,
        }

    @staticmethod
    def _expected_tiles(
        normalized_size: tuple[int, int],
        policy: VisionPreprocessPolicy,
    ) -> tuple[tuple[str, tuple[int, int, int, int], str], ...]:
        width, height = normalized_size
        x_origins = tile_origins(width, policy.tile_width, policy.overlap)
        y_origins = tile_origins(height, policy.tile_height, policy.overlap)
        return tuple(
            (
                f"r{row}c{column}",
                (
                    x0,
                    y0,
                    min(x0 + policy.tile_width, width),
                    min(y0 + policy.tile_height, height),
                ),
                f"r{row}c{column}.png",
            )
            for row, y0 in enumerate(y_origins, start=1)
            for column, x0 in enumerate(x_origins, start=1)
        )

    @staticmethod
    def _required_tile_count(
        normalized_size: tuple[int, int],
        policy: VisionPreprocessPolicy,
    ) -> int:
        def axis_count(length: int, tile_size: int) -> int:
            if length <= tile_size:
                return 1
            stride = tile_size - policy.overlap
            if stride <= 0:
                raise ValueError("overlap must be smaller than each tile dimension")
            far_edge = length - tile_size
            regular_count = far_edge // stride + 1
            last_regular = (regular_count - 1) * stride
            return regular_count + int(last_regular != far_edge)

        return axis_count(normalized_size[0], policy.tile_width) * axis_count(
            normalized_size[1], policy.tile_height
        )

    @staticmethod
    def _overview_size(normalized_size: tuple[int, int]) -> tuple[int, int]:
        width, height = normalized_size
        scale = min(1.0, _OVERVIEW_MAX_SIZE[0] / width, _OVERVIEW_MAX_SIZE[1] / height)
        return (
            max(1, min(_OVERVIEW_MAX_SIZE[0], round(width * scale))),
            max(1, min(_OVERVIEW_MAX_SIZE[1], round(height * scale))),
        )

    @staticmethod
    def _annotated_overview(
        normalized: Image.Image,
        tiles: list[tuple[str, tuple[int, int, int, int]]],
    ) -> Image.Image:
        overview = normalized.convert("RGB")
        overview_size = VisionPreprocessor._overview_size(normalized.size)
        if overview.size != overview_size:
            overview = overview.resize(overview_size, Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(overview)
        font = ImageFont.load_default()
        scale_x = overview.width / normalized.width
        scale_y = overview.height / normalized.height
        line_width = max(1, round(min(overview.size) / 400))
        max_x = overview.width - 1
        max_y = overview.height - 1

        def clamp(value: int, upper: int) -> int:
            return max(0, min(upper, value))

        for logical_id, (x0, y0, x1, y1) in tiles:
            scaled_x0 = clamp(round(x0 * scale_x), max_x)
            scaled_y0 = clamp(round(y0 * scale_y), max_y)
            scaled = (
                scaled_x0,
                scaled_y0,
                clamp(max(scaled_x0, round(x1 * scale_x) - 1), max_x),
                clamp(max(scaled_y0, round(y1 * scale_y) - 1), max_y),
            )
            draw.rectangle(scaled, outline=(255, 255, 255), width=line_width)
            label_x = clamp(scaled[0] + 2, max_x)
            label_y = clamp(scaled[1] + 2, max_y)
            shadow = (clamp(label_x + 1, max_x), clamp(label_y + 1, max_y))
            draw.text(shadow, logical_id, fill=(0, 0, 0), font=font)
            draw.text((label_x, label_y), logical_id, fill=(255, 255, 255), font=font)
        return overview

    def _binding_from_manifest(
        self,
        manifest: object,
        *,
        derived_dir: Path,
        normalized: Image.Image,
        policy: VisionPreprocessPolicy,
        session_id: str,
        request_id: str,
        source_ordinal: int,
        expected_size: tuple[int, int],
    ) -> TileSetBinding | None:
        if not isinstance(manifest, dict):
            return None
        if manifest.get("policy_version") != _CACHE_POLICY_VERSION:
            return None
        if manifest.get("normalized_size") != list(expected_size):
            return None
        overview_name = manifest.get("overview")
        overview_sha256 = manifest.get("overview_sha256")
        tile_records = manifest.get("tiles")
        if overview_name != "overview.png" or not isinstance(tile_records, list):
            return None
        expected_tiles = self._expected_tiles(expected_size, policy)
        expected_overview = self._annotated_overview(
            normalized,
            [(logical_id, bounds) for logical_id, bounds, _filename in expected_tiles],
        )
        overview_path = self._safe_child(derived_dir, overview_name)
        if self._is_regular_file(overview_path):
            self._ensure_private_file(overview_path)
        if self._validated_png_bytes(
            overview_path,
            expected_size=self._overview_size(expected_size),
            expected_png_bytes=self._png_bytes(expected_overview),
            expected_sha256=overview_sha256,
            max_inline_bytes=MAX_CACHE_ARTIFACT_INLINE_BYTES,
        ) is None:
            return None

        if len(tile_records) != len(expected_tiles):
            return None
        tiles: list[TileDescriptor] = []
        for record, (expected_id, expected_bounds, expected_filename) in zip(tile_records, expected_tiles):
            if not isinstance(record, dict):
                return None
            logical_id = record.get("tile_id")
            bounds = record.get("bounds")
            filename = record.get("filename")
            if (
                logical_id != expected_id
                or bounds != list(expected_bounds)
                or filename != expected_filename
            ):
                return None
            path = self._safe_child(derived_dir, expected_filename)
            if self._is_regular_file(path):
                self._ensure_private_file(path)
            encoded = self._validated_png_bytes(
                path,
                expected_size=(expected_bounds[2] - expected_bounds[0], expected_bounds[3] - expected_bounds[1]),
                expected_png_bytes=self._png_bytes(normalized.crop(expected_bounds)),
                expected_sha256=record.get("sha256"),
                max_inline_bytes=MAX_CACHE_ARTIFACT_INLINE_BYTES,
            )
            if encoded is None:
                return None
            actual_inline_bytes = self._data_url_length(len(encoded))
            if record.get("inline_bytes") != actual_inline_bytes:
                return None
            tiles.append(
                TileDescriptor(
                    tile_id=f"image{source_ordinal}-{expected_id}",
                    bounds=expected_bounds,
                    path=path,
                    inline_bytes=actual_inline_bytes,
                    sha256=str(record["sha256"]),
                )
            )
        return TileSetBinding(
            tile_set_id=f"tiles-{uuid.uuid4().hex}",
            session_id=str(session_id),
            request_id=str(request_id),
            source_ordinal=source_ordinal,
            normalized_size=expected_size,
            overview_path=overview_path,
            overview_sha256=str(overview_sha256),
            tiles=tuple(tiles),
        )

    def _capture_artifact_data_url(
        self,
        path: Path,
        *,
        expected_sha256: str,
        expected_size: tuple[int, int],
        expected_inline_bytes: int | None = None,
    ) -> str:
        """Atomically read and validate one immutable provider attachment."""
        try:
            self._ensure_private_file(path)
            encoded = path.read_bytes()
            if hashlib.sha256(encoded).hexdigest() != expected_sha256:
                raise self._error("A derived image artifact changed before attachment")
            data_url = f"data:image/png;base64,{base64.b64encode(encoded).decode('ascii')}"
            actual_inline_bytes = len(data_url.encode("ascii"))
            if actual_inline_bytes > MAX_CACHE_ARTIFACT_INLINE_BYTES:
                raise self._error("A derived image artifact exceeds the safe inline limit")
            if (
                expected_inline_bytes is not None
                and actual_inline_bytes != expected_inline_bytes
            ):
                raise self._error("A derived image artifact size changed before attachment")
            with Image.open(io.BytesIO(encoded), formats=["PNG"]) as image:
                if (
                    image.format != "PNG"
                    or image.size != expected_size
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise self._error("A derived image artifact failed attachment validation")
                image.load()
            return data_url
        except VisionPreprocessError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise self._error("Unable to capture a derived image artifact safely") from exc

    @staticmethod
    def _validated_png_bytes(
        path: Path,
        *,
        expected_size: tuple[int, int],
        expected_png_bytes: bytes,
        expected_sha256: object,
        max_inline_bytes: int,
    ) -> bytes | None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return None
        if not VisionPreprocessor._is_regular_file(path):
            return None
        try:
            if VisionPreprocessor._data_url_length(path.stat(follow_symlinks=False).st_size) > max_inline_bytes:
                return None
            encoded = path.read_bytes()
            if hashlib.sha256(encoded).hexdigest() != expected_sha256:
                return None
            with Image.open(io.BytesIO(encoded), formats=["PNG"]) as image:
                if image.format != "PNG" or image.size != expected_size or getattr(image, "n_frames", 1) != 1:
                    return None
                image.load()
            if encoded != expected_png_bytes:
                return None
            return encoded
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ):
            return None

    @staticmethod
    def _read_manifest(path: Path) -> object:
        if not VisionPreprocessor._is_regular_file(path):
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def _resolved_root(self, *, create: bool) -> Path:
        if self.cache_root.is_symlink():
            raise self._error("Configured vision cache root is a symlink and may escape the cache root")
        if create:
            try:
                self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise self._error("Unable to create the vision cache root") from exc
        if not self.cache_root.exists() or not self.cache_root.is_dir():
            raise self._error("Configured vision cache root is not a directory")
        if self.cache_root.is_symlink():
            raise self._error("Configured vision cache root is a symlink and may escape the cache root")
        self._ensure_private_directory(self.cache_root)
        return self.cache_root.resolve()

    def _safe_target(self, relative: str, *, create_root: bool) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise self._error("Vision cache target escaped the cache root")
        root = self._resolved_root(create=create_root)
        target = root / relative_path
        current = root
        for component in relative_path.parts:
            current = current / component
            if current.is_symlink():
                raise self._error("Symlinked vision cache component may escape the cache root")
        if not target.resolve(strict=False).is_relative_to(root):
            raise self._error("Vision cache target escaped the cache root")
        return target

    def _safe_child(self, directory: Path, filename: str) -> Path:
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise self._error("Vision cache manifest target escaped the cache root")
        root = self._resolved_root(create=False)
        self._assert_no_symlink(directory)
        target = directory / filename
        if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
            raise self._error("Vision cache manifest target escaped the cache root")
        return target

    @staticmethod
    def _assert_no_symlink(path: Path) -> None:
        if path.is_symlink():
            raise VisionPreprocessor._error("Symlinked vision cache component may escape the cache root")

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            return not path.is_symlink() and stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
        except OSError:
            return False

    def _atomic_write(self, target: Path, content: bytes) -> None:
        root = self._resolved_root(create=False)
        if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
            raise self._error("Vision cache write target escaped the cache root")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        if not temporary.resolve(strict=False).is_relative_to(root):
            raise self._error("Vision cache temporary target escaped the cache root")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            self._ensure_private_file(temporary)
            temporary.replace(target)
            self._ensure_private_file(target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        VisionPreprocessor._ensure_private_mode(path, 0o700, directory=True)

    @staticmethod
    def _ensure_private_file(path: Path) -> None:
        VisionPreprocessor._ensure_private_mode(path, 0o600, directory=False)

    @staticmethod
    def _ensure_private_mode(path: Path, mode: int, *, directory: bool) -> None:
        if os.name != "posix":
            return
        try:
            metadata = path.stat(follow_symlinks=False)
            expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
            if not expected_type or stat.S_ISLNK(metadata.st_mode):
                raise VisionPreprocessor._error("Vision cache permissions could not be secured")
            if stat.S_IMODE(metadata.st_mode) != mode:
                path.chmod(mode, follow_symlinks=False)
                metadata = path.stat(follow_symlinks=False)
            if stat.S_IMODE(metadata.st_mode) != mode:
                raise VisionPreprocessor._error("Vision cache permissions could not be secured")
        except VisionPreprocessError:
            raise
        except (NotImplementedError, OSError, RuntimeError) as exc:
            raise VisionPreprocessor._error("Vision cache permissions could not be secured") from exc

    @staticmethod
    def _directory_size(directory: Path) -> int:
        total = 0
        pending = [directory]
        while pending:
            current = pending.pop()
            for entry in current.iterdir():
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
                elif stat.S_ISDIR(metadata.st_mode):
                    pending.append(entry)
        return total

    @staticmethod
    def _data_url_length(file_size: int) -> int:
        return _PNG_DATA_URL_PREFIX_BYTES + 4 * ((file_size + 2) // 3)

    @staticmethod
    def _error(reason: str) -> VisionPreprocessError:
        return VisionPreprocessError(
            f"{reason}. Astra did not silently send a provider-downscaled substitute; "
            "disable vision tiles explicitly to restore direct sending."
        )
