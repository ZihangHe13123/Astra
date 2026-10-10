"""Session-owned turn-change snapshot area. Independent of file checkpoints;
never enters model requests.

M1 interface contract (frozen 2026-09-18; see
docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md §3.2).

Behavior notes that the frozen contract left to this module (all are covered
by ``tests/test_turn_change_store.py``):

- 主清单（``files``）只收"存在性/字节已确认"的条目；任一侧 ``uncaptured``
  一律进 ``unknown`` 区（review F5）。两侧都已确定、只是没算完计数的条目
  留在主清单并降级（``compare=none``、``added/removed=None``、
  ``reason=timeout|cancelled``）。
- ``note_paths`` 仅登记候选：从未取得快照的候选路径进 ``unknown``（reason=error），
  绝不作为已确认改动出现。
- tracked 行只有指纹、没有字节：``missing`` 侧记 ``absent``，有指纹侧记
  ``uncaptured``；确认变化进主清单且 ``compare=none``/``reason=tracked``。
  字节快照优先于指纹。
- 配额在捕获阶段生效（单文件/回合/会话），会话超量先 FIFO 淘汰最旧回合；
  任何 I/O 或 differ 失败只降级、不抛错。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import turn_diff
from .process_env import pid_alive
from .turn_diff import DiffStats

# 五态
STATE_MODIFIED = "modified"
STATE_ADDED = "added"
STATE_DELETED = "deleted"
STATE_UNCHANGED = "unchanged"
STATE_UNKNOWN = "unknown"

# 每侧快照状态（"原来不存在" vs "未捕获" 必须显式区分）
SIDE_CAPTURED = "captured"      # 有字节快照
SIDE_ABSENT = "absent"          # 确认当时不存在
SIDE_UNCAPTURED = "uncaptured"  # 未取得快照（失败/超限/配额）

# 对比可用性
COMPARE_FULL = "full"
COMPARE_COARSE = "coarse"
COMPARE_NONE = "none"

REASON_QUOTA = "quota"
REASON_TRACKED = "tracked"
REASON_ERROR = "error"


def _always_cancelled() -> bool:
    return True


_ALWAYS_CANCELLED: Callable[[], bool] = _always_cancelled

logger = logging.getLogger(__name__)

# 落盘布局（§3.2 行为要求 5）
MANIFEST_NAME = "manifest.json"
OWNER_NAME = "owner.json"
# 完成回合索引（M3 · review R1）：单个原子快照文件，窗口内 ≤ max_turns_retained 条记录
INDEX_NAME = "turns.json"
INDEX_VERSION = 1
# 查询失败语义（review R1 冻结）：写失败未落定 / 索引本身不可读
INDEX_REASON_UNSETTLED = "index-write-unsettled"
INDEX_REASON_UNAVAILABLE = "index-unavailable"
DEFAULT_ROOT_PARTS = (".astra", "turn-changes")

_SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_TRACKED_CONTENT_PREFIXES = ("file:", "symlink:")
_TURN_DIR_RE = re.compile(r"turn-[0-9]+")
_TRACKED_MISSING = "missing"
_TRACKED_DEFENSIVE = "<clean>"
_TRACKED_ERROR_PREFIX = "error:"

_CURRENT: ContextVar["TurnChangeStore | None"] = ContextVar("astra_turn_change_store", default=None)


@dataclass(frozen=True)
class TurnChangeLimits:
    """Budgets for the snapshot area and diff computation."""

    max_file_bytes: int = 2 * 1024 * 1024        # 单文件参与字节
    max_paths_per_turn: int = 500                # 每回合路径数
    max_turn_bytes: int = 32 * 1024 * 1024       # 单回合快照总字节
    max_session_bytes: int = 256 * 1024 * 1024   # 单会话快照区总字节
    max_turns_retained: int = 10                 # FIFO 保留回合数
    diff_deadline_ms: int = 100                  # 单文件 diff 时限
    compute_budget_ms: int = 500                 # 回合计算总预算


@dataclass
class FileChange:
    path: str
    display: str
    state: str
    before_state: str
    after_state: str
    added: int | None
    removed: int | None
    compare: str
    reason: str
    checkpoint_ids: list[str]


@dataclass
class TurnChangesManifest:
    session_id: str
    request_id: str
    turn_seq: int
    created_at: float
    files: list[FileChange]      # 已确认净改动（含 binary/coarse 条目）
    unknown: list[FileChange]    # 未能确认区（绝不与已确认并列）
    totals: dict[str, int]       # {"files": n, "added": x, "removed": y}


@dataclass(frozen=True)
class LoadedSides:
    before: bytes | None
    after: bytes | None
    before_state: str
    after_state: str   # captured / absent / uncaptured（读取失败=uncaptured）


@dataclass(frozen=True)
class CompletedTurn:
    """One finished turn as recorded in the bounded session index (M3 · review R1).

    ``empty`` 只用于"确定完成且净改动为空"的回合（此时 ``dir_name is None``，不落
    manifest）；快照已被字节配额淘汰或不可读的回合保留记录但 ``available=False``，
    与 ``empty`` 显式区分（"已淘汰" ≠ "没有改动"）。
    """

    turn_seq: int
    request_id: str
    created_at: float
    dir_name: str | None
    empty: bool
    files: int
    unknown: int
    available: bool


@dataclass(frozen=True)
class TurnIndexRead:
    """Result of :meth:`TurnChangeStore.completed_turns`, newest turn first.

    ``ok=False`` 表示"回合索引暂不可用"（索引写失败未落定、读失败/损坏、目录身份
    已变）：调用方必须区别于"没有完成回合"，也不得回退到更旧的回合充当 ``@1``
    （review R1）。
    """

    ok: bool
    records: list[CompletedTurn]
    reason: str = ""


@dataclass
class _PathEntry:
    """One touched path inside the active turn (mutable turn-scoped state)."""

    path: str
    resolved: Path
    # 目标目录身份（读取 after 前核验，防止跟随被替换的目录；R1/R4）
    anchor: tuple[int, int] | None = None
    before_state: str = ""            # "" = 尚未登记（仅候选）；SIDE_* = 已登记
    before_reason: str = ""
    before: bytes | None = None
    tracked: tuple[str | None, str | None] | None = None
    checkpoint_ids: list[str] = field(default_factory=list)


@dataclass
class _ResolvedEntry:
    change: FileChange
    before_bytes: bytes | None = None
    after_bytes: bytes | None = None


def _safe_session_name(session_id: str) -> str:
    """Deterministic, filesystem-safe directory name for one session id."""
    safe = _SAFE_SESSION_CHARS.sub("_", session_id).strip("._-") or "session"
    if safe == session_id and len(safe) <= 80:
        return safe
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:80]}-{digest}"


def session_history_evidence(workspace: str | Path, session_id: str) -> bool | None:
    """Read-only probe for ``/changes`` when no live store exists (review R5).

    ``True`` = the session directory carries evidence of completed turns
    (a ``turns.json`` or any ``turn-*`` snapshot directory); ``False`` = no
    such evidence (a fresh session); ``None`` = the state cannot be
    determined and callers must treat it conservatively.  Never creates the
    session directory and never writes anything.
    """
    try:
        session_dir = (
            Path(workspace).expanduser().resolve().joinpath(*DEFAULT_ROOT_PARTS)
            / _safe_session_name(session_id)
        )
        if (session_dir / INDEX_NAME).exists():
            return True
        if not session_dir.exists():
            return False
        if not session_dir.is_dir():
            return None
        for child in session_dir.iterdir():
            name = child.name
            if name.startswith("turn-") and name[len("turn-"):].isdigit():
                return True
        return False
    except OSError:
        return None


def _tracked_has_content(fingerprint: str | None) -> bool:
    return fingerprint is not None and fingerprint.startswith(_TRACKED_CONTENT_PREFIXES)


def _tracked_is_unusable(fingerprint: str | None) -> bool:
    return fingerprint is not None and (
        fingerprint.startswith(_TRACKED_ERROR_PREFIX) or fingerprint == _TRACKED_DEFENSIVE
    )


class TurnChangeStore:
    """Session-owned snapshot area; one instance per session (see plan §3.2)."""

    def __init__(
        self,
        workspace: str | Path,
        session_id: str,
        *,
        limits: TurnChangeLimits | None = None,
        root: str | Path | None = None,
        differ: Callable[..., DiffStats] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        raw_workspace = Path(workspace).expanduser()
        if not raw_workspace.is_absolute():
            raw_workspace = Path.cwd() / raw_workspace
        # Refuse a swapped final symlink; identity is re-checked before every
        # write so a later swap degrades instead of escaping the workspace (R1).
        self._workspace_raw = raw_workspace
        self._anchor_identity = _open_dir_anchor(raw_workspace)
        self.workspace = raw_workspace.resolve()
        self.session_id = str(session_id)
        self.limits = limits if limits is not None else TurnChangeLimits()
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else self.workspace.joinpath(*DEFAULT_ROOT_PARTS).resolve()
        )
        self.session_dir = self.root / _safe_session_name(self.session_id)
        self._differ = differ
        self._clock = clock
        self._lock = threading.RLock()
        self._active: dict[str, _PathEntry] | None = None
        self._request_id = ""
        self._turn_bytes = 0
        self._truncated = False
        # 本实例所有权令牌：owner.json 记录 (pid, token, session_id)（review R7）
        self._owner_token = os.urandom(16).hex()
        # 接管决策时看到的 owner 快照：锁内复核"持有人未变"后才写入（F1）
        self._owner_expectation: Any = None
        # 最近一次合作式收尾是否在让出点收到了真实取消（review F4）
        self.seal_stopped_by_cancel = False
        # 收尾完成但取消已传播的清单，供调用方取回（review G2）
        self._stopped_manifest: TurnChangesManifest | None = None
        # 完成回合索引（M3 · review R1）：进程内序号高水位只增不减；已登记序号保证
        # 同一回合只登记一次；失败未落定的记录留在内存，随下一次成功写入一并带上。
        self._seq_high_water = 0
        self._registered_turns: set[int] = set()
        self._index_records: list[dict[str, Any]] = []
        self._index_loaded = False
        self._index_poisoned = False
        self._index_dirty = False
        self._index_unsettled = False
        # 本实例最后写入的 owner 内容：删除会话区前复核仍是它（review H1）
        self._owner_payload: dict[str, Any] | None = None
        # 存储目录链身份：workspace → root → session dir（review F2）
        self._root_anchor: tuple[int, int] | None = None
        self._session_anchor: tuple[int, int] | None = None
        # 会话目录创建后、写入任何快照内容前，立即写 owner.json（行为要求 10）
        self._ensure_session_dir()

    # -- 回合生命周期（react 主 agent 接线） --

    def begin_turn(self, request_id: str) -> None:
        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    "turn-change store: a turn is already active; seal it before begin_turn"
                )
            self._active = {}
            self._request_id = str(request_id)
            self._turn_bytes = 0
            self._truncated = False
            self._ensure_session_dir()

    def note_paths(self, paths: Iterable[str]) -> None:
        with self._lock:
            for raw_path in paths:
                self._register(raw_path)

    def note_capture(
        self,
        path: str | os.PathLike[str],
        before: bytes,
        *,
        checkpoint_id: str = "",
        display: str | None = None,
    ) -> None:
        """登记首份 before 字节；``display`` 为展示名（默认相对 store 工作区）.

        ``path`` 是可信的稳定身份：传绝对路径（如文件策略根解析出的
        ``captured.path``）时，before/after 都以它为目标（review R4）。
        """
        with self._lock:
            entry = self._register(path, display=display)
            if entry is None:
                return
            self._remember_checkpoint(entry, checkpoint_id)
            if entry.before_state:
                return  # 每文件每回合只存首份；首份语义即定
            data = bytes(before)
            state, reason = self._accept_snapshot(len(data))
            if state == SIDE_CAPTURED:
                entry.before = data
                entry.before_state = SIDE_CAPTURED
            else:
                # 配额在捕获阶段即生效：不复制该字节，记 uncaptured + quota
                entry.before_state = SIDE_UNCAPTURED
                entry.before_reason = reason

    def note_absent(
        self,
        path: str | os.PathLike[str],
        *,
        checkpoint_id: str = "",
        display: str | None = None,
    ) -> None:
        with self._lock:
            entry = self._register(path, display=display)
            if entry is None:
                return
            self._remember_checkpoint(entry, checkpoint_id)
            if entry.before_state:
                return  # 与 note_capture 互斥（新建 vs 修改）
            entry.before_state = SIDE_ABSENT

    def note_tracked(self, path: str, before_fp: str | None, after_fp: str | None) -> None:
        """登记一条 git 指纹变更；同一路径多次登记保留"首次 before + 末次 after".

        混合规则：字节快照（note_capture/note_absent）优先于指纹。同一路径既有
        快照又有 tracked 时，判定只走快照（首份 before 字节 vs 封存时读取），
        tracked 链不参与，登记顺序不影响判定。
        """
        with self._lock:
            entry = self._register(path)
            if entry is None:
                return
            if entry.tracked is None:
                entry.tracked = (before_fp, after_fp)
            else:
                # 回合首尾净变化语义：命令序列多次触碰同一路径时，比较首次
                # before 与最后一次 after（改回原样/删除后重建都取净结果）。
                entry.tracked = (entry.tracked[0], after_fp)

    def seal(self, *, cancelled: Callable[[], bool] | None = None) -> TurnChangesManifest | None:
        """净判定 + 配额 + 落盘 + FIFO；空回合返回 None。

        每文件 ``deadline = min(now + diff_deadline_ms, 回合截止时间)``；``cancelled``
        原样贯通给 differ。超预算/取消时不再读取 after（未确认条目进未知区），
        ``seal`` 不抛错。
        """
        state = self._begin_seal()
        if state is None:
            return None
        active, request_id, deadline, turn_seq = state
        resolved = self._resolve_entries(active, deadline, cancelled)
        return self._finish_seal(request_id, turn_seq, deadline, cancelled, resolved)

    async def seal_async(
        self, *, cancelled: Callable[[], bool] | None = None
    ) -> TurnChangesManifest | None:
        """合作式收尾：条目之间把控制权交还事件循环（review F4）.

        真实外部取消（事件循环上排期的 cancel）会在让出点得到投递；收到后停止
        剩余可放弃的读取/写入/淘汰，清单与落盘仍完成，并把
        ``seal_stopped_by_cancel`` 置 True 供调用方观察。
        """
        state = self._begin_seal()
        if state is None:
            return None
        active, request_id, deadline, turn_seq = state
        effective = cancelled
        resolved: list[_ResolvedEntry] = []
        for entry in active.values():
            if effective is not _ALWAYS_CANCELLED:
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    # 取消在让出点被处理：不再启动新的可放弃工作（review F4）
                    effective = _ALWAYS_CANCELLED
            outcome = self._resolve_entry(entry, deadline, effective)
            if outcome is not None:
                resolved.append(outcome)
        forced = effective is _ALWAYS_CANCELLED
        try:
            manifest = await self._finish_seal_async(
                request_id, turn_seq, deadline, effective, resolved
            )
        except asyncio.CancelledError:
            self.seal_stopped_by_cancel = True
            raise
        if forced:
            # 读取阶段的取消：清单已产出 → 暂存后继续传播取消语义（review G2）
            self.seal_stopped_by_cancel = True
            self._stopped_manifest = manifest
            raise asyncio.CancelledError()
        self.seal_stopped_by_cancel = False
        return manifest

    def _begin_seal(self) -> tuple[dict[str, _PathEntry], str, float, int] | None:
        """Take the active turn out of the store, allocate its seq, compute the deadline.

        序号在收尾开始时分配（``_next_seq``）：空回合同样占号，保证 ``@k`` 与稳定回合
        标识一一对应；没有活动回合时不分配任何序号（review R1）。
        """
        with self._lock:
            active = self._active
            if active is None:
                return None
            request_id = self._request_id
            self._active = None
            self._request_id = ""
            deadline = self._clock() + self.limits.compute_budget_ms / 1000.0
            turn_seq = self._next_seq()
        return active, request_id, deadline, turn_seq

    def _resolve_entries(
        self,
        active: dict[str, _PathEntry],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[_ResolvedEntry]:
        resolved: list[_ResolvedEntry] = []
        for entry in active.values():
            outcome = self._resolve_entry(entry, deadline, cancelled)
            if outcome is not None:
                resolved.append(outcome)
        return resolved

    def _resolve_entry(
        self,
        entry: _PathEntry,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry | None:
        outcome = self._resolve(entry, deadline, cancelled)
        if outcome is None:
            return None
        if outcome.change.state == STATE_UNCHANGED:
            # 净变化为零：主清单与未知区都不出现（行为要求 3，review R9）
            return None
        return outcome

    def _finish_seal(
        self,
        request_id: str,
        turn_seq: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
        resolved: list[_ResolvedEntry],
    ) -> TurnChangesManifest | None:
        manifest = self._build_manifest(request_id, resolved, turn_seq)
        if manifest is None:
            # 空回合（无操作/改回原样）：不落 manifest、不发事件、不进模型请求，
            # 但登记入索引并占号（review R1）
            self._finish_empty_turn(request_id, turn_seq, deadline, cancelled)
            return None
        self._register_completed_turn(
            request_id=request_id,
            turn_seq=turn_seq,
            created_at=manifest.created_at,
            dir_name=f"turn-{turn_seq}",
            files=len(manifest.files),
            unknown=len(manifest.unknown),
            empty=False,
        )
        self._persist(manifest, resolved, deadline=deadline, cancelled=cancelled)
        return manifest

    async def _finish_seal_async(
        self,
        request_id: str,
        turn_seq: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
        resolved: list[_ResolvedEntry],
    ) -> TurnChangesManifest | None:
        manifest = self._build_manifest(request_id, resolved, turn_seq)
        if manifest is None:
            self._finish_empty_turn(request_id, turn_seq, deadline, cancelled)
            return None
        self._register_completed_turn(
            request_id=request_id,
            turn_seq=turn_seq,
            created_at=manifest.created_at,
            dir_name=f"turn-{turn_seq}",
            files=len(manifest.files),
            unknown=len(manifest.unknown),
            empty=False,
        )
        try:
            await self._persist_async(manifest, resolved, deadline=deadline, cancelled=cancelled)
        except asyncio.CancelledError:
            # 落盘阶段收到真实取消：清单已按有界策略写完，暂存后继续传播（G2/G3）
            self._stopped_manifest = manifest
            raise
        return manifest

    def _build_manifest(
        self,
        request_id: str,
        resolved: list[_ResolvedEntry],
        turn_seq: int | None = None,
    ) -> TurnChangesManifest | None:
        if not resolved:
            return None
        files = [
            item.change
            for item in resolved
            if item.change.state not in (STATE_UNKNOWN, STATE_UNCHANGED)
        ]
        unknown = [item.change for item in resolved if item.change.state == STATE_UNKNOWN]
        totals = {
            "files": len(files),
            "added": sum(change.added for change in files if change.added is not None),
            "removed": sum(change.removed for change in files if change.removed is not None),
        }
        return TurnChangesManifest(
            session_id=self.session_id,
            request_id=request_id,
            turn_seq=self._next_seq() if turn_seq is None else int(turn_seq),
            created_at=self._clock(),
            files=files,
            unknown=unknown,
            totals=totals,
        )

    def take_stopped_manifest(self) -> TurnChangesManifest | None:
        """取回"收尾完成但取消已传播"的清单（review G2）."""
        manifest = self._stopped_manifest
        self._stopped_manifest = None
        return manifest

    # -- 读取（M2/M3 用；M1 供测试） --

    def manifest(self, turn_offset: int = 0) -> TurnChangesManifest | None:
        """兼容口径（M1 测试用）：按 turn-N 目录的 offset 读取。

        命令层不使用（review R1/R2）：offset 只数"落盘过的回合"，空回合不占 offset。
        """
        with self._lock:
            turn_dir = self._turn_dir(turn_offset)
            if turn_dir is None:
                return None
            payload = self._read_manifest_payload(turn_dir)
        if payload is None:
            return None
        return _manifest_from_payload(payload)

    def load_sides(self, turn_offset: int, path: str) -> LoadedSides:
        """兼容口径（M1 测试用）：按 offset + display **首条匹配** 读取。

        命令层不使用：同一 display 的多条目按名字选条会读错（M3 · review R2），
        ``/changes`` 走 :meth:`load_sides_for` 的 ``(turn_seq, entry_index)`` 绑定。
        """
        with self._lock:
            turn_dir = self._turn_dir(turn_offset)
            if turn_dir is None:
                return LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
            payload = self._read_manifest_payload(turn_dir)
            if payload is None:
                return LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
            _, display, _ = self._key(path)
            record: dict[str, Any] | None = None
            for raw in [*_payload_list(payload.get("files")), *_payload_list(payload.get("unknown"))]:
                if isinstance(raw, dict) and raw.get("path") == display:
                    record = raw
                    break
            if record is None:
                return LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
            before, before_state = self._load_side(turn_dir, record.get("before_file"), record.get("before_state"))
            after, after_state = self._load_side(turn_dir, record.get("after_file"), record.get("after_state"))
            return LoadedSides(before, after, before_state, after_state)

    # -- 完成回合索引（M3 · review R1） --

    def _index_window_size(self) -> int:
        return max(1, int(self.limits.max_turns_retained))

    def _trim_index(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """窗口 = 序号最大的 N 条；出窗记录从这里消失（对应目录随后一并淘汰）."""
        ordered = sorted(records, key=lambda item: _record_seq(item) or 0)
        return ordered[-self._index_window_size():]

    def _read_index_file(
        self, *, missing_after_history: bool = False
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Fresh disk read of ``turns.json``; ``ok=False`` means "暂不可用".

        不缓存结果：外部损坏/替换必须在下一次查询被看见（review R1）。目录身份被
        替换（锚不符）时同样返回不可用，不采信换入者的记录。

        ``missing_after_history``（只用于查询路径）：文件缺失时，若本会话曾有完成
        回合（内存记录/高水位/``turn-*`` 目录任一），说明"索引发布过又消失"，报
        暂不可用；写路径与淘汰决策保持"缺失=还没有索引"的语义（review R2）。
        """
        if not self._storage_ok():
            return False, INDEX_REASON_UNAVAILABLE, []
        try:
            raw = _read_all_bytes(
                self.session_dir / INDEX_NAME, anchors=self._storage_anchors()
            )
        except FileNotFoundError:
            if missing_after_history and self._has_history_evidence():
                logger.warning(
                    "turn-change store: completed-turn index vanished for %s",
                    self.session_dir,
                )
                return False, INDEX_REASON_UNAVAILABLE, []
            return True, "", []
        except OSError as exc:
            logger.warning("turn-change store: unreadable completed-turn index (%s)", exc)
            return False, INDEX_REASON_UNAVAILABLE, []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("turn-change store: corrupt completed-turn index (%s)", exc)
            return False, INDEX_REASON_UNAVAILABLE, []
        if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
            logger.warning("turn-change store: unsupported completed-turn index payload")
            return False, INDEX_REASON_UNAVAILABLE, []
        records = payload.get("records")
        if not isinstance(records, list) or not all(_valid_index_record(item) for item in records):
            logger.warning("turn-change store: malformed completed-turn index records")
            return False, INDEX_REASON_UNAVAILABLE, []
        return True, "", [dict(item) for item in records]

    def _has_history_evidence(self) -> bool:
        """True when this session had completed turns before (review R2).

        信号（任一）：本实例登记过索引记录或分配过序号；会话目录里仍有
        ``turn-*`` 快照目录。索引文件缺失而这里为 True 意味着"索引发布过又
        消失"——查询应报暂不可用，而不是"没有历史"。
        """
        if self._index_records or self._seq_high_water > 0:
            return True
        return bool(self._turn_dirs())

    def _index_records_for_read(self) -> tuple[bool, str, list[dict[str, Any]]]:
        """Records visible to queries: 未落定的写优先报"暂不可用"（review R1）."""
        if self._index_dirty or self._index_unsettled:
            return False, INDEX_REASON_UNSETTLED, []
        return self._read_index_file(missing_after_history=True)

    def _next_index_seq(self) -> int | None:
        """Highest seq the on-disk index knows about; ``None`` when unreadable."""
        ok, _reason, records = self._read_index_file()
        if not ok:
            return None
        seqs = [_record_seq(item) for item in records]
        return max([seq for seq in seqs if seq is not None], default=None)

    def _flush_index_locked(self) -> bool:
        """Atomic reorganisation of "旧记录 + 新记录取末 N 条" (review R1).

        复用 ``_write_json_atomic``（temp + ``os.replace``，经锚复核的父句柄）。写失败
        或磁盘索引不可读时只置"未落定"标记：查询报暂不可用、不写坏数据、也不丢内存
        里的记录（下一次成功写入把它们一并带上）。
        """
        ok, _reason, disk_records = self._read_index_file()
        if not ok:
            # 磁盘索引已损坏/不可读：不覆盖它，也不假装写成功（review R1）
            self._index_unsettled = True
            return False
        merged: dict[int, dict[str, Any]] = {}
        for record in [*self._index_records, *disk_records]:
            seq = _record_seq(record)
            if seq is not None:
                merged[seq] = dict(record)
        window = self._trim_index(list(merged.values()))
        try:
            _write_json_atomic(
                self.session_dir / INDEX_NAME,
                {"version": INDEX_VERSION, "records": window},
            )
        except Exception as exc:  # noqa: BLE001 - ledger never breaks the turn
            logger.warning(
                "turn-change store: cannot write completed-turn index (%s: %s)",
                type(exc).__name__,
                exc,
            )
            self._index_unsettled = True
            return False
        self._index_records = window
        self._index_dirty = False
        self._index_unsettled = False
        return True

    def _index_persist(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """Index-only persist for turns without a manifest (空回合) (review R1).

        与 ``_persist`` 相同的两段式复核：锁外先验一次，取得会话锁后再次
        ``_ensure_session_dir()``（内部锁内复核 owner 快照与目录身份）——
        取锁与写入之间失去所有权时保守停为未落定，不发布索引（review R1-fix）。
        """
        if not self._ensure_session_dir():
            self._index_unsettled = True
            return
        wait = None if deadline is None else max(0.0, deadline - self._clock())
        stop = self._stop_predicate(deadline, cancelled)
        with _session_guard(self._session_lock_path(), wait_seconds=wait, stop=stop) as locked:
            if not locked:
                logger.warning(
                    "turn-change store: session lock unavailable; completed-turn index stays unsettled"
                )
                self._index_unsettled = True
                return
            if not self._ensure_session_dir():
                logger.warning(
                    "turn-change store: session ownership changed while waiting; "
                    "completed-turn index stays unsettled"
                )
                self._index_unsettled = True
                return
            with _io_anchor_scope(self._storage_anchors()):
                self._flush_index_locked()

    def _register_completed_turn(
        self,
        *,
        request_id: str,
        turn_seq: int,
        created_at: float,
        dir_name: str | None,
        files: int,
        unknown: int,
        empty: bool,
    ) -> bool:
        """Idempotent registration: 同一回合（同一 turn_seq）只登记一次（review R1）.

        同步 ``seal``、``seal_async`` 与兜底路径共用这一个入口；重复收尾不产生第二条
        记录。序号在登记时抬高高水位，索引失败后也不回退、不复用。
        """
        with self._lock:
            if turn_seq in self._registered_turns:
                return False
            self._registered_turns.add(turn_seq)
            self._seq_high_water = max(self._seq_high_water, int(turn_seq))
            self._index_records = self._trim_index(
                [
                    *self._index_records,
                    {
                        "turn_seq": int(turn_seq),
                        "request_id": str(request_id),
                        "created_at": float(created_at),
                        "dir": dir_name,
                        "empty": bool(empty),
                        "files": int(files),
                        "unknown": int(unknown),
                    },
                ]
            )
            self._index_dirty = True
        return True

    def _finish_empty_turn(
        self,
        request_id: str,
        turn_seq: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        """空回合（含"改回原样"）登记入索引并占号；不发事件、不落 manifest（R1）."""
        self._register_completed_turn(
            request_id=request_id,
            turn_seq=turn_seq,
            created_at=self._clock(),
            dir_name=None,
            files=0,
            unknown=0,
            empty=True,
        )
        self._index_persist(deadline=deadline, cancelled=cancelled)
        stop = self._stop_predicate(deadline, cancelled)
        if stop is None or not stop():
            self._enforce_retention(stop=stop)

    def completed_turns(self) -> TurnIndexRead:
        """完成回合索引（新→旧）；``ok=False`` = 回合索引暂不可用（review R1）.

        查询只读：不修复、不写回、不成为模型回合。区分三种状态 —— 空回合
        （``empty``）、快照被淘汰/不可读（``available=False``）、索引不可用（``ok``）。
        """
        with self._lock:
            ok, reason, records = self._index_records_for_read()
            if not ok:
                return TurnIndexRead(False, [], reason)
            dir_names = {path.name for _seq, path in self._turn_dirs()}
            completed = [
                self._completed_from_record(record, dir_names)
                for record in sorted(records, key=lambda item: _record_seq(item) or 0)
            ]
        completed.reverse()
        return TurnIndexRead(True, completed, "")

    def _completed_from_record(
        self, record: dict[str, Any], dir_names: set[str]
    ) -> CompletedTurn:
        seq = int(_record_seq(record) or 0)
        raw_dir = record.get("dir")
        dir_name = str(raw_dir) if isinstance(raw_dir, str) and raw_dir else None
        empty = bool(record.get("empty")) or dir_name is None
        return CompletedTurn(
            turn_seq=seq,
            request_id=str(record.get("request_id") or ""),
            created_at=float(record.get("created_at") or 0.0),
            dir_name=dir_name,
            empty=empty,
            files=max(0, _optional_int(record.get("files")) or 0),
            unknown=max(0, _optional_int(record.get("unknown")) or 0),
            # 目录已被字节配额淘汰/不可读 = available False，与 empty 显式区分
            available=True if empty else dir_name in dir_names,
        )

    def _record_dir(self, turn_seq: int) -> str | None:
        """Directory name for ``turn_seq`` per the index; ``None`` = 无可用快照."""
        ok, _reason, records = self._index_records_for_read()
        if not ok:
            return None
        for record in records:
            if _record_seq(record) == int(turn_seq):
                raw_dir = record.get("dir")
                if isinstance(raw_dir, str) and raw_dir and not record.get("empty"):
                    return raw_dir
                return None
        return None

    def manifest_for(self, turn_seq: int) -> TurnChangesManifest | None:
        """Manifest addressed by stable ``turn_seq``（索引不可用/空回合 → None）."""
        with self._lock:
            dir_name = self._record_dir(turn_seq)
            if dir_name is None:
                return None
            payload = self._read_manifest_payload(self.session_dir / dir_name, expect_seq=turn_seq)
            if payload is None:
                return None
        return _manifest_from_payload(payload)

    def load_sides_for(self, turn_seq: int, entry_index: int) -> LoadedSides:
        """Read one entry by its identity ``(turn_seq, entry_index)`` (review R2).

        ``entry_index`` 是该回合条目**合并序**（files 在前、unknown 在后，与展示编号
        一致：展示编号 = ``entry_index + 1``）；``display`` 不参与选条。两侧都经
        ``_load_side``（handle-bound、锚/身份复核）读取持久化快照，不读实时目标文件。
        """
        missing = LoadedSides(None, None, SIDE_UNCAPTURED, SIDE_UNCAPTURED)
        if entry_index < 0:
            return missing
        with self._lock:
            dir_name = self._record_dir(turn_seq)
            if dir_name is None:
                return missing
            turn_dir = self.session_dir / dir_name
            payload = self._read_manifest_payload(turn_dir, expect_seq=turn_seq)
            if payload is None:
                return missing
            combined = [
                *_payload_list(payload.get("files")),
                *_payload_list(payload.get("unknown")),
            ]
            if entry_index >= len(combined):
                return missing
            record = combined[entry_index]
            if not isinstance(record, dict):
                return missing
            before, before_state = self._load_side(
                turn_dir, record.get("before_file"), record.get("before_state")
            )
            after, after_state = self._load_side(
                turn_dir, record.get("after_file"), record.get("after_state")
            )
            return LoadedSides(before, after, before_state, after_state)

    # -- 生命周期 --

    def close(self) -> None:
        """会话释放：仅在本实例仍持有该会话区时删除快照目录（之后可重新 begin_turn）.

        身份核验与删除都在会话锁内完成，避免与另一个实例的接管交错（F1）。
        """
        with self._lock:
            self._active = None
            self._request_id = ""
            self._turn_bytes = 0
            if not self._storage_ok():
                return
            with _session_guard(self._session_lock_path()) as locked:
                if not locked:
                    return
                if self._owner_is_ours():
                    self._remove_session_dir()

    def _remove_session_dir(self) -> None:
        """Delete the session area through verified handles (G1); the final child is re-verified (H1)."""
        if not _HANDLE_IO_OK:
            shutil.rmtree(self.session_dir, ignore_errors=True)
            self._session_anchor = None
            return
        try:
            parent_fd, name = _walk_open_parent(self.session_dir, self._storage_anchors())
        except OSError as exc:
            logger.warning(
                "turn-change store: cannot reach session area %s (%s)", self.session_dir, exc
            )
            return
        removed = False
        try:
            try:
                # 要删除的最终目录必须仍是此前核验过的那个（identity + owner，同一句柄）（review H1）
                with _remove_expectation_scope(self._session_anchor, self._owner_payload):
                    _remove_tree_at(parent_fd, name)
                removed = True
            except FileNotFoundError:
                removed = True
            except OSError as exc:
                logger.warning(
                    "turn-change store: cannot remove session area %s (%s)", self.session_dir, exc
                )
        finally:
            os.close(parent_fd)
        if removed:
            # 目录已删：下一次建立时重新锚定，避免 close 后无法重新 begin_turn
            self._session_anchor = None

    @staticmethod
    def cleanup_orphans(
        root: str | Path,
        *,
        is_alive: Callable[[int], bool] | None = None,
    ) -> list[str]:
        """只清理"确认无活跃持有者"的会话目录；无 owner.json 一律跳过（行为要求 10）.

        枚举、复核与删除都经根目录句柄完成：目录项被换成符号链接或路径被替换
        时不会跟随（review F2/G1）。
        """
        removed: list[str] = []
        base = Path(root).expanduser()
        probe = is_alive if is_alive is not None else _pid_alive
        if not _HANDLE_IO_OK:
            return _cleanup_orphans_path_based(base, probe)
        try:
            root_fd = os.open(base, _dir_open_flags())
        except OSError:
            return removed
        try:
            try:
                names = sorted(os.listdir(root_fd))
            except OSError:
                return removed
            for name in names:
                if name == _LOCK_DIR_NAME:
                    continue
                display = base / name
                try:
                    st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    # 符号链接目录项：绝不跟随（F2）
                    logger.warning("turn-change store: skip %s (symbolic link)", display)
                    continue
                if not stat.S_ISDIR(st.st_mode):
                    continue
                # 与接管/写入/淘汰共享同一会话锁；锁内复核持有人（review F1）
                with _session_guard(base / _LOCK_DIR_NAME / f"{name}.lock") as locked:
                    if not locked:
                        logger.warning(
                            "turn-change store: skip %s (session lock unavailable)", display
                        )
                        continue
                    if _remove_orphan_session(root_fd, name, display, probe):
                        removed.append(str(display))
        finally:
            os.close(root_fd)
        return removed

    # -- 内部：登记与收尾 --

    def _anchor_ok(self) -> bool:
        """True while the workspace path still resolves to the anchored directory."""
        try:
            st = os.stat(self._workspace_raw)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == self._anchor_identity

    def _session_lock_path(self) -> Path:
        """Per-session cross-instance lock shared by claim/write/evict/close/cleanup (F1)."""
        return self.root / _LOCK_DIR_NAME / f"{self.session_dir.name}.lock"

    def _ensure_session_dir(self) -> bool:
        """Create the session area and establish/take over its ownership marker.

        所有权身份 = (pid, token, session_id)：活跃占用（其他存活实例）一律
        拒绝写入；旧实例已退出则原子接管（review R7）。建立与校验绑定
        workspace → root → session 目录链身份，且建立/打开只经目录句柄完成
        （review F2/G1）。
        """
        if not self._anchor_ok():
            logger.warning(
                "turn-change store: workspace identity changed for %s; refusing to write",
                self._workspace_raw,
            )
            return False
        if self._root_anchor is not None and not self._root_ok():
            logger.warning(
                "turn-change store: snapshot root identity changed for %s; refusing to write",
                self.root,
            )
            return False
        if not _HANDLE_IO_OK:
            return self._ensure_session_dir_path_based()
        if not self._mkdir_chain(self.session_dir):
            return False
        try:
            root_fd, session_name = _walk_open_parent(self.session_dir, self._storage_anchors())
        except OSError as exc:
            logger.warning(
                "turn-change store: refusing to open snapshot area %s (%s)", self.session_dir, exc
            )
            return False
        created = False
        try:
            try:
                session_fd = os.open(
                    session_name,
                    _dir_open_flags() | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(session_name, dir_fd=root_fd)
                    created = True
                except FileExistsError:
                    created = False
                except OSError as exc:
                    logger.warning(
                        "turn-change store: cannot prepare snapshot area %s (%s)",
                        self.session_dir,
                        exc,
                    )
                    return False
                try:
                    session_fd = os.open(
                        session_name,
                        _dir_open_flags() | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_fd,
                    )
                except OSError as exc:
                    logger.warning(
                        "turn-change store: session path %s is not a plain directory (%s)",
                        self.session_dir,
                        exc,
                    )
                    return False
            except OSError as exc:
                logger.warning(
                    "turn-change store: session path %s is not a plain directory (%s)",
                    self.session_dir,
                    exc,
                )
                return False
            try:
                st = os.fstat(session_fd)
                current = (st.st_dev, st.st_ino)
                if created or self._session_anchor is None:
                    self._session_anchor = current
                elif current != self._session_anchor:
                    logger.warning(
                        "turn-change store: session directory identity changed for %s; refusing to write",
                        self.session_dir,
                    )
                    return False
            finally:
                os.close(session_fd)
        finally:
            os.close(root_fd)
        if self._root_anchor is None:
            self._root_anchor = _safe_dir_anchor(self.root)
        return self._claim_ownership()

    def _storage_anchors(self) -> dict[Path, tuple[int, int]]:
        """Recorded directory identities re-checked on every opened handle (F2/G1)."""
        anchors: dict[Path, tuple[int, int]] = {}
        if self._root_anchor is not None:
            anchors[self.root] = self._root_anchor
        if self._session_anchor is not None:
            anchors[self.session_dir] = self._session_anchor
        return anchors

    def _ensure_session_dir_path_based(self) -> bool:
        """Windows fallback: create/verify the session area by path (no dir handles)."""
        try:
            current = _open_dir_anchor(self.session_dir)
        except OSError:
            current = None
        if current is None:
            if os.path.lexists(self.session_dir):
                logger.warning(
                    "turn-change store: session path %s is not a plain directory; refusing to write",
                    self.session_dir,
                )
                return False
            try:
                self.session_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "turn-change store: cannot prepare snapshot area %s (%s)",
                    self.session_dir,
                    exc,
                )
                return False
        elif self._session_anchor is not None and current != self._session_anchor:
            logger.warning(
                "turn-change store: session directory identity changed for %s; refusing to write",
                self.session_dir,
            )
            return False
        if self._root_anchor is None:
            self._root_anchor = _safe_dir_anchor(self.root)
        self._session_anchor = (
            current if current is not None else _safe_dir_anchor(self.session_dir)
        )
        return self._claim_ownership()

    def _mkdir_chain(self, path: Path) -> bool:
        """Create ``path`` (and missing components) relative to verified handles (G1)."""
        return _ensure_dir_chain(path)

    def _root_ok(self) -> bool:
        if self._root_anchor is None:
            return True
        return _safe_dir_anchor(self.root) == self._root_anchor

    def _session_dir_ok(self) -> bool:
        if self._session_anchor is None:
            return True
        return _safe_dir_anchor(self.session_dir) == self._session_anchor

    def _storage_ok(self) -> bool:
        """Verify the snapshot path chain before storage I/O (F2)."""
        if not self._anchor_ok():
            logger.warning(
                "turn-change store: workspace identity changed for %s; refusing storage I/O",
                self._workspace_raw,
            )
            return False
        if not self._root_ok():
            logger.warning(
                "turn-change store: snapshot root identity changed for %s; refusing storage I/O",
                self.root,
            )
            return False
        if not self._session_dir_ok():
            logger.warning(
                "turn-change store: session directory identity changed for %s; refusing storage I/O",
                self.session_dir,
            )
            return False
        return True

    def _claim_ownership(self) -> bool:
        """Read the owner marker, then claim/take over under the session lock.

        读取旧 owner 与写入新 owner 之间可能被另一个实例插入；因此接管是
        "乐观判断 + 锁内复核"：``_write_owner`` 只在锁内确认持有人仍是本次
        决策看到的那个（或仍不存在）时才写入（review F1）。
        """
        owner_path = self.session_dir / OWNER_NAME
        readable, owner = self._read_owner_state(owner_path)
        if not readable:
            logger.warning(
                "turn-change store: unreadable owner marker %s; refusing writes", owner_path
            )
            return False
        if owner is None:
            # 尚无 owner.json：建立（锁内复核"仍不存在"）
            self._owner_expectation = None
            return self._write_owner(owner_path)
        if self._owner_matches(owner):
            return True
        pid = owner.get("pid") if isinstance(owner, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            logger.warning(
                "turn-change store: owner marker %s has no usable pid; refusing writes", owner_path
            )
            return False
        if pid == os.getpid() or _pid_alive(pid):
            logger.warning(
                "turn-change store: session area %s is actively owned by pid=%s; refusing writes",
                self.session_dir,
                pid,
            )
            return False
        # 旧实例已退出 → 尝试接管；锁内复核持有人未变才写入
        self._owner_expectation = owner
        return self._write_owner(owner_path)

    def _read_owner_state(self, owner_path: Path) -> tuple[bool, Any]:
        """Read the owner marker; ``(False, None)`` when it exists but is unusable."""
        try:
            raw = _read_all_bytes(owner_path, anchors=self._storage_anchors()).decode("utf-8")
        except FileNotFoundError:
            return True, None
        except (OSError, UnicodeDecodeError):
            return False, None
        try:
            return True, json.loads(raw)
        except ValueError:
            return False, None

    def _write_owner(self, owner_path: Path) -> bool:
        """Write the owner marker under the session lock after re-checking the holder (F1).

        只有锁内复核确认 owner 仍是决策时看到的快照（含"仍不存在"）才写入；
        否则说明另一个实例已经接管，直接拒绝，绝不覆盖活跃 token。
        """
        expected = self._owner_expectation
        with _session_guard(self._session_lock_path()) as locked:
            if not locked:
                logger.warning(
                    "turn-change store: session lock unavailable for %s; refusing takeover",
                    owner_path,
                )
                return False
            readable, current = self._read_owner_state(owner_path)
            if not readable or current != expected:
                logger.warning(
                    "turn-change store: owner marker %s changed while waiting; refusing takeover",
                    owner_path,
                )
                return False
            try:
                payload = {
                    "session_id": self.session_id,
                    "pid": os.getpid(),
                    "token": self._owner_token,
                    "created_at": time.time(),
                }
                _write_json_atomic(
                    owner_path,
                    payload,
                    anchors=self._storage_anchors(),
                )
            except OSError as exc:
                logger.warning(
                    "turn-change store: cannot write owner marker %s (%s)", owner_path, exc
                )
                return False
            self._owner_payload = payload
        return True

    def _owner_matches(self, owner: Any) -> bool:
        return (
            isinstance(owner, dict)
            and owner.get("session_id") == self.session_id
            and owner.get("pid") == os.getpid()
            and owner.get("token") == self._owner_token
        )

    def _owner_is_ours(self) -> bool:
        """True only while owner.json still records this instance's identity."""
        readable, owner = self._read_owner_state(self.session_dir / OWNER_NAME)
        return readable and owner is not None and self._owner_matches(owner)

    def _turn_dirs(self) -> list[tuple[int, Path]]:
        found: list[tuple[int, Path]] = []
        try:
            children = list(self.session_dir.iterdir())
        except OSError:
            return found
        for child in children:
            suffix = child.name[len("turn-"):] if child.name.startswith("turn-") else ""
            if not suffix.isdigit() or not child.is_dir():
                continue
            found.append((int(suffix), child))
        found.sort(key=lambda item: item[0])
        return found

    def _turn_dir(self, turn_offset: int) -> Path | None:
        if turn_offset < 0:
            return None
        turns = self._turn_dirs()
        if turn_offset >= len(turns):
            return None
        return turns[len(turns) - 1 - turn_offset][1]

    def _next_seq(self) -> int:
        """Allocate the next sequence without ever re-using one (M3 · review R1).

        ``max(索引内 seq, turn-N 目录, 进程内高水位) + 1``：空回合同样占号，索引
        失败后也不回退、不复用已分配序号（高水位只增不减）。
        """
        highest = self._seq_high_water
        for seq, _path in self._turn_dirs():
            if seq > highest:
                highest = seq
        for record in self._index_records:
            seq = _record_seq(record)
            if seq is not None and seq > highest:
                highest = seq
        if not self._index_records and not self._index_dirty:
            disk = self._next_index_seq()
            if disk is not None and disk > highest:
                highest = disk
        self._seq_high_water = max(self._seq_high_water, highest + 1)
        return self._seq_high_water

    def _session_bytes(self) -> int:
        total = 0
        for _seq, turn_dir in self._turn_dirs():
            for path in turn_dir.glob("*.bin"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _remove_turn(self, turn_dir: Path) -> bool:
        """Remove one stale turn area through verified handles; True when gone (G1)."""
        if not _HANDLE_IO_OK:
            try:
                shutil.rmtree(turn_dir)
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                logger.warning(
                    "turn-change store: cannot remove stale turn area %s (%s)", turn_dir, exc
                )
                return False
        try:
            parent_fd, name = _walk_open_parent(turn_dir, self._storage_anchors())
        except OSError as exc:
            logger.warning(
                "turn-change store: cannot reach stale turn area %s (%s)", turn_dir, exc
            )
            return False
        try:
            try:
                _remove_tree_at(parent_fd, name)
            except FileNotFoundError:
                return True
            except OSError as exc:
                logger.warning(
                    "turn-change store: cannot remove stale turn area %s (%s)", turn_dir, exc
                )
                return False
        finally:
            os.close(parent_fd)
        return True

    def _evict_until(
        self,
        condition: Callable[[], bool],
        *,
        stop: Callable[[], bool] | None = None,
        wait: float | None = None,
    ) -> bool:
        """Bounded FIFO eviction; stops as soon as a removal makes no progress.

        Locked or permission-protected directories degrade the caller (quota
        handling) instead of retrying the same failing entry forever. All
        removals happen under the session lock, whose wait follows the turn
        budget when one is known (review F1/G3/H3).
        """
        if not self._storage_ok():
            return condition()
        with _session_guard(
            self._session_lock_path(), wait_seconds=wait, stop=stop
        ) as locked:
            if not locked:
                return condition()
            while not condition():
                turns = self._turn_dirs()
                if not turns:
                    break
                if stop is not None and stop():
                    # 已取消/过期：淘汰同样属于可放弃工作（review F4）
                    break
                if not self._storage_ok():
                    # 目录链身份已变（被替换/换成符号链接）：不再删除任何回合目录（F2）
                    break
                if not self._owner_is_ours():
                    # 未核验到同一 owner 身份前，不删除任何回合目录（review R7）
                    break
                if not self._remove_turn(turns[0][1]):
                    break
                if len(self._turn_dirs()) >= len(turns):
                    break  # no measurable progress; stop instead of looping
            return condition()

    def _enforce_retention(self, *, stop: Callable[[], bool] | None = None) -> None:
        """FIFO: 只保留最近 max_turns_retained 个回合；会话超量先淘汰最旧回合.

        目录保留数量随索引窗口变化是本批的明示增量（review R1）：出窗记录（序号
        小于窗口内最小序号）对应的目录一并淘汰；窗口内但快照已淘汰/不可读的回合
        留在索引里 ``available=False``。索引不可用（读失败/写未落定）时不按窗口
        淘汰，避免误删刚登记的回合目录。
        """
        keep = max(1, int(self.limits.max_turns_retained))
        self._evict_until(lambda: len(self._turn_dirs()) <= keep, stop=stop)
        self._evict_until(
            lambda: len(self._turn_dirs()) <= 1
            or self._session_bytes() <= self.limits.max_session_bytes,
            stop=stop,
        )
        self._evict_until(
            lambda: not self._index_bound_turn_dirs(stop=stop), stop=stop
        )

    def _index_bound_turn_dirs(self, *, stop: Callable[[], bool] | None = None) -> list[Path]:
        """Directory names strictly older than the index window; ``[]`` when unknown.

        保守口径：索引不可用、窗口为空、或索引里没有"比某目录更旧"的记录时不产出
        候选（返回 ``[]``），绝不按不确定信息删目录。
        """
        if self._index_dirty or self._index_unsettled:
            return []
        ok, _reason, records = self._read_index_file()
        if not ok:
            return []
        seqs = [seq for seq in (_record_seq(record) for record in records) if seq is not None]
        if not seqs:
            return []
        oldest_retained = min(seqs)
        stale = [
            turn_dir for seq, turn_dir in self._turn_dirs() if seq < oldest_retained
        ]
        if stop is not None and stop():
            return []
        return stale

    def _read_manifest_payload(
        self, turn_dir: Path, *, expect_seq: int | None = None
    ) -> dict[str, Any] | None:
        """Read one turn manifest; ``expect_seq`` binds it to the indexed turn (R2).

        索引说 ``turn_seq`` 对应 ``turn-N``，而目录里的 manifest 自报别的回合时，
        该目录内容不可信（换入/串号），一律当作不可用，不返回其中的字节。
        """
        try:
            payload = json.loads(
                _read_all_bytes(
                    turn_dir / MANIFEST_NAME, anchors=self._storage_anchors()
                ).decode("utf-8")
            )
        except (OSError, ValueError, TypeError, UnicodeDecodeError) as exc:
            logger.warning("turn-change store: unreadable turn manifest %s (%s)", turn_dir, exc)
            return None
        if not isinstance(payload, dict):
            return None
        if expect_seq is not None and _optional_int(payload.get("turn_seq")) != int(expect_seq):
            logger.warning(
                "turn-change store: turn manifest %s does not belong to turn %s",
                turn_dir,
                expect_seq,
            )
            return None
        return payload

    def _load_side(self, turn_dir: Path, name: Any, state: Any) -> tuple[bytes | None, str]:
        if state == SIDE_ABSENT:
            return None, SIDE_ABSENT
        if state != SIDE_CAPTURED:
            return None, SIDE_UNCAPTURED
        if not isinstance(name, str) or not name:
            return None, SIDE_UNCAPTURED
        try:
            return (
                _read_all_bytes(
                    turn_dir / Path(name).name, anchors=self._storage_anchors()
                ),
                SIDE_CAPTURED,
            )
        except OSError as exc:
            logger.warning("turn-change store: unreadable snapshot %s (%s)", name, exc)
            return None, SIDE_UNCAPTURED

    def _persist(
        self,
        manifest: TurnChangesManifest,
        entries: list[_ResolvedEntry],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """尽力落盘；写入门控与锁等待按剩余预算完成（review F1/G3）."""
        # 先核验目录链与持有人再触碰锁目录：目录被替换时连锁都不落（F2/F1）
        if not self._ensure_session_dir():
            return
        wait = None if deadline is None else max(0.0, deadline - self._clock())
        stop = self._stop_predicate(deadline, cancelled)
        with _session_guard(self._session_lock_path(), wait_seconds=wait, stop=stop) as locked:
            if not locked:
                logger.warning(
                    "turn-change store: session lock unavailable; skipping persist of turn %s",
                    manifest.turn_seq,
                )
                return
            self._persist_locked(manifest, entries, deadline=deadline, cancelled=cancelled)

    async def _persist_async(
        self,
        manifest: TurnChangesManifest,
        entries: list[_ResolvedEntry],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """Cooperative persist: bounded lock wait, then yielding writes (review G3)."""
        if not self._ensure_session_dir():
            return
        wait = None if deadline is None else max(0.0, deadline - self._clock())
        stop = self._stop_predicate(deadline, cancelled)
        with _session_guard(self._session_lock_path(), wait_seconds=wait, stop=stop) as locked:
            if not locked:
                logger.warning(
                    "turn-change store: session lock unavailable; skipping persist of turn %s",
                    manifest.turn_seq,
                )
                return
            await self._persist_locked_async(
                manifest, entries, deadline=deadline, cancelled=cancelled
            )

    def _persist_iter(
        self,
        manifest: TurnChangesManifest,
        entries: list[_ResolvedEntry],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
        forced: Callable[[], bool] | None = None,
    ) -> Iterator[None]:
        """可放弃落盘的步骤序列：每次快照写入后 yield 一次（review G3）.

        驱动方可以同步跑完（``seal``），也可以在让出点交还事件循环
        （``seal_async``），让排期的真实取消得到投递。每次写入前都重新看一次
        取消/预算，停止后不再启动新的可放弃写入；清单仍会写完。
        """
        turn_dir = self.session_dir / f"turn-{manifest.turn_seq}"
        if not self._ensure_session_dir():
            return
        self._mkdir_chain(turn_dir)
        stop = self._stop_predicate(deadline, cancelled)

        def stopped() -> bool:
            if forced is not None and forced():
                return True
            return stop is not None and stop()

        by_identity = {id(item.change): item for item in entries}
        payload_entries: list[dict[str, Any]] = []
        for index, change in enumerate([*manifest.files, *manifest.unknown]):
            item = by_identity.get(id(change))
            sides: dict[str, str | None] = {"before": None, "after": None}
            if item is not None and item.before_bytes is not None and not stopped():
                sides["before"] = f"before.{index}.bin"
                _write_bytes(turn_dir / sides["before"], item.before_bytes)
                yield
            if item is not None and item.after_bytes is not None and not stopped():
                sides["after"] = f"after.{index}.bin"
                _write_bytes(turn_dir / sides["after"], item.after_bytes)
                yield
            payload_entries.append(
                _change_payload(
                    change,
                    sides,
                    before_state=(
                        SIDE_UNCAPTURED
                        if change.before_state == SIDE_CAPTURED and sides["before"] is None
                        else change.before_state
                    ),
                    after_state=(
                        SIDE_UNCAPTURED
                        if change.after_state == SIDE_CAPTURED and sides["after"] is None
                        else change.after_state
                    ),
                )
            )
        _write_json_atomic(
            turn_dir / MANIFEST_NAME,
            {
                "session_id": manifest.session_id,
                "request_id": manifest.request_id,
                "turn_seq": manifest.turn_seq,
                "created_at": manifest.created_at,
                "files": payload_entries[: len(manifest.files)],
                "unknown": payload_entries[len(manifest.files):],
                "totals": dict(manifest.totals),
            },
        )
        # 完成回合索引与本次快照在同一会话锁内落定（review R1）：登记已在收尾时入内存，
        # 这里按"旧记录 + 新记录取末 N 条"重组并原子写；写失败只让查询"暂不可用"，
        # 不改变本回合的成功/取消结果，也不复用已分配序号。
        self._flush_index_locked()
        if not stopped():
            self._enforce_retention(stop=stop)

    def _persist_locked(
        self,
        manifest: TurnChangesManifest,
        entries: list[_ResolvedEntry],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """尽力落盘；任何失败只降级（调用方仍拿到内存 manifest）."""
        turn_dir = self.session_dir / f"turn-{manifest.turn_seq}"
        try:
            with _io_anchor_scope(self._storage_anchors()):
                for _ in self._persist_iter(
                    manifest, entries, deadline=deadline, cancelled=cancelled
                ):
                    pass
        except Exception as exc:  # noqa: BLE001 - ledger never breaks the turn
            logger.warning(
                "turn-change store: cannot persist turn %s (%s: %s)",
                manifest.turn_seq,
                type(exc).__name__,
                exc,
            )
            self._remove_turn(turn_dir)

    async def _persist_locked_async(
        self,
        manifest: TurnChangesManifest,
        entries: list[_ResolvedEntry],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """合作式落盘：每次写入之间让出事件循环，取消在让出点被处理（review G3）."""
        turn_dir = self.session_dir / f"turn-{manifest.turn_seq}"
        flag = {"stop": False}
        with _io_anchor_scope(self._storage_anchors()):
            steps = self._persist_iter(
                manifest,
                entries,
                deadline=deadline,
                cancelled=cancelled,
                forced=lambda: flag["stop"],
            )
            try:
                for _ in steps:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                # 剩余可放弃写入已被拦下；同步收口（清单照写）后再传播取消（G2/G3）
                flag["stop"] = True
                try:
                    for _ in steps:
                        pass
                except Exception as exc:  # noqa: BLE001 - ledger never breaks the turn
                    logger.warning(
                        "turn-change store: cannot finish persist of turn %s (%s: %s)",
                        manifest.turn_seq,
                        type(exc).__name__,
                        exc,
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - ledger never breaks the turn
                logger.warning(
                    "turn-change store: cannot persist turn %s (%s: %s)",
                    manifest.turn_seq,
                    type(exc).__name__,
                    exc,
                )
                self._remove_turn(turn_dir)

    def _stop_predicate(
        self, deadline: float | None, cancelled: Callable[[], bool] | None
    ) -> Callable[[], bool] | None:
        """收尾期间是否已取消/过期（None = 不做门控）（review F4）."""
        if deadline is None:
            return None
        return lambda: bool(self._stop_reason(deadline, cancelled))

    def _require_active(self) -> dict[str, _PathEntry]:
        if self._active is None:
            raise RuntimeError(
                "turn-change store: no active turn; call begin_turn before note_*"
            )
        return self._active

    def _register(
        self,
        raw_path: str | os.PathLike[str],
        *,
        display: str | None = None,
    ) -> _PathEntry | None:
        active = self._require_active()
        key, derived_display, resolved = self._key(raw_path)
        existing = active.get(key)
        if existing is not None:
            return existing
        if len(active) >= max(1, int(self.limits.max_paths_per_turn)):
            if not self._truncated:
                self._truncated = True
                logger.warning(
                    "turn-change store: path budget %d reached; further touched paths are ignored",
                    self.limits.max_paths_per_turn,
                )
            return None
        entry = _PathEntry(path=display or derived_display, resolved=resolved)
        # 记录目标目录身份；读取 after 前核验，目录被替换/换成符号链接时降级
        try:
            entry.anchor = _open_dir_anchor(resolved.parent)
        except OSError:
            entry.anchor = None
        active[key] = entry
        return entry

    def _key(self, raw_path: str | os.PathLike[str]) -> tuple[str, str, Path]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - Path.resolve is tolerant on 3.11+
            resolved = Path(candidate.absolute())
        key = resolved.as_posix()
        try:
            display = resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            display = key
        return key, display, resolved

    @staticmethod
    def _remember_checkpoint(entry: _PathEntry, checkpoint_id: str) -> None:
        if checkpoint_id and checkpoint_id not in entry.checkpoint_ids:
            entry.checkpoint_ids.append(checkpoint_id)

    def _accept_snapshot(
        self,
        size: int,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, str]:
        """Reserve ``size`` snapshot bytes, or report why they are not stored.

        回合预算/取消贯通到配额淘汰：预算用尽后不再启动淘汰旧回合，当前捕获
        按停止原因降级（review H3）。
        """
        if size > self.limits.max_file_bytes:
            return SIDE_UNCAPTURED, REASON_QUOTA
        if self._turn_bytes + size > self.limits.max_turn_bytes:
            return SIDE_UNCAPTURED, REASON_QUOTA
        stop = self._stop_predicate(deadline, cancelled)
        if deadline is not None and stop is not None and stop():
            return SIDE_UNCAPTURED, self._stop_reason(deadline, cancelled)
        if not self._session_has_room(size, deadline=deadline, stop=stop):
            if deadline is not None and stop is not None and stop():
                return SIDE_UNCAPTURED, self._stop_reason(deadline, cancelled)
            return SIDE_UNCAPTURED, REASON_QUOTA
        self._turn_bytes += size
        return SIDE_CAPTURED, ""

    def _session_has_room(
        self,
        extra: int,
        *,
        deadline: float | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> bool:
        """会话超量先 FIFO 淘汰最旧回合；仍超则由调用方内部降级（行为要求 6）."""
        limit = self.limits.max_session_bytes
        if self._session_bytes() + self._turn_bytes + extra <= limit:
            return True
        wait = None if deadline is None else max(0.0, deadline - self._clock())
        self._evict_until(
            lambda: not self._turn_dirs()
            or self._session_bytes() + self._turn_bytes + extra <= limit,
            stop=stop,
            wait=wait,
        )
        return self._session_bytes() + self._turn_bytes + extra <= limit

    def _resolve(
        self,
        entry: _PathEntry,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry | None:
        if entry.before_state or entry.tracked is None:
            # 该路径会读取 after；取消/过期后停止新的可放弃捕获（review R6）
            stop_reason = self._stop_reason(deadline, cancelled)
            if stop_reason:
                return self._degraded_without_read(entry, stop_reason)
        if entry.before_state:
            return self._resolve_snapshot(entry, deadline, cancelled)
        if entry.tracked is not None:
            return self._resolve_tracked(entry)
        # 仅候选：before 从未取得快照 → 未能确认区（带原因）
        after_state, after_bytes, _ = self._read_after(
            entry, deadline=deadline, cancelled=cancelled
        )
        return self._unknown_entry(entry, SIDE_UNCAPTURED, REASON_ERROR, after_state, after_bytes)

    def _read_after(
        self,
        entry: _PathEntry,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, bytes | None, str]:
        if not self._anchor_ok():
            logger.warning(
                "turn-change store: workspace identity changed for %s; skipping read",
                self._workspace_raw,
            )
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if entry.anchor is not None:
            try:
                current = _open_dir_anchor(entry.resolved.parent)
            except OSError:
                current = None
            if current != entry.anchor:
                logger.warning(
                    "turn-change store: target directory identity changed for %s; skipping read",
                    entry.path,
                )
                return SIDE_UNCAPTURED, None, REASON_ERROR
        path = entry.resolved
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return SIDE_ABSENT, None, ""
        except OSError as exc:
            logger.warning("turn-change store: cannot stat %s (%s)", entry.path, exc)
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if stat.S_ISLNK(st.st_mode):
            # 最终组件被换成符号链接：不跟随（F2）
            logger.warning(
                "turn-change store: %s is a symbolic link; refusing to follow", entry.path
            )
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if not stat.S_ISREG(st.st_mode):
            logger.warning("turn-change store: %s is not a regular file", entry.path)
            return SIDE_UNCAPTURED, None, REASON_ERROR
        size = st.st_size
        state, reason = self._accept_snapshot(
            size, deadline=deadline, cancelled=cancelled
        )
        if state != SIDE_CAPTURED:
            # 超限的 after 字节绝不落盘
            return SIDE_UNCAPTURED, None, reason
        try:
            # 读取绑定到已验证的目标目录身份（经句柄复核；review G1）
            with _io_anchor_scope({path.parent: entry.anchor} if entry.anchor else None):
                data = _read_bytes_bounded(path, self.limits.max_file_bytes)
        except OSError as exc:
            self._turn_bytes -= size
            logger.warning("turn-change store: cannot read %s (%s)", entry.path, exc)
            return SIDE_UNCAPTURED, None, REASON_ERROR
        if len(data) > self.limits.max_file_bytes:
            # 读取期间增长越过单文件上限：有界读取兜底，超限字节不落盘
            self._turn_bytes -= size
            logger.warning(
                "turn-change store: %s grew past the byte limit while reading", entry.path
            )
            return SIDE_UNCAPTURED, None, REASON_QUOTA
        if len(data) != size:  # 读取期间被改写：按实际字节数重新结算
            self._turn_bytes -= size
            state, reason = self._accept_snapshot(
                len(data), deadline=deadline, cancelled=cancelled
            )
            if state != SIDE_CAPTURED:
                return SIDE_UNCAPTURED, None, reason
        return SIDE_CAPTURED, data, ""

    def _resolve_snapshot(
        self,
        entry: _PathEntry,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry | None:
        before_state = entry.before_state
        after_state, after_bytes, after_reason = self._read_after(
            entry, deadline=deadline, cancelled=cancelled
        )
        if before_state == SIDE_CAPTURED and after_state == SIDE_CAPTURED:
            if entry.before == after_bytes:
                # 两侧字节相同（含"改了又改回"）→ unchanged；seal 契约排除该状态
                return _ResolvedEntry(
                    self._no_change(entry, before_state, after_state),
                    entry.before,
                    after_bytes,
                )
            return self._diff_entry(
                entry, entry.before, after_bytes, STATE_MODIFIED, before_state, after_state, deadline, cancelled
            )
        if before_state == SIDE_ABSENT and after_state == SIDE_ABSENT:
            return None  # 原来不存在、现在也不存在 → 无净改动
        if before_state == SIDE_ABSENT and after_state == SIDE_CAPTURED:
            return self._diff_entry(
                entry, None, after_bytes, STATE_ADDED, before_state, after_state, deadline, cancelled
            )
        if before_state == SIDE_CAPTURED and after_state == SIDE_ABSENT:
            return self._diff_entry(
                entry, entry.before, None, STATE_DELETED, before_state, after_state, deadline, cancelled
            )
        reason = after_reason or entry.before_reason or REASON_ERROR
        return self._unknown_entry(entry, before_state, reason, after_state, after_bytes)

    def _resolve_tracked(self, entry: _PathEntry) -> _ResolvedEntry | None:
        before_fp, after_fp = entry.tracked or (None, None)
        before_state = SIDE_ABSENT if before_fp == _TRACKED_MISSING else SIDE_UNCAPTURED
        after_state = SIDE_ABSENT if after_fp == _TRACKED_MISSING else SIDE_UNCAPTURED
        if _tracked_is_unusable(before_fp) or _tracked_is_unusable(after_fp):
            logger.warning(
                "turn-change store: unusable tracked fingerprint for %s (%r vs %r)",
                entry.path,
                before_fp,
                after_fp,
            )
            return self._unknown_entry(entry, SIDE_UNCAPTURED, REASON_TRACKED, SIDE_UNCAPTURED)
        state = _tracked_state(before_fp, after_fp)
        if state is None:
            return None
        if state == STATE_UNKNOWN:
            return self._unknown_entry(entry, before_state, REASON_TRACKED, after_state)
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=before_state,
                after_state=after_state,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=REASON_TRACKED,
                checkpoint_ids=list(entry.checkpoint_ids),
            )
        )

    def _diff_entry(
        self,
        entry: _PathEntry,
        before: bytes | None,
        after: bytes | None,
        state: str,
        before_state: str,
        after_state: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> _ResolvedEntry:
        stop_reason = self._stop_reason(deadline, cancelled)
        if stop_reason:
            return self._degraded_entry(entry, before, after, state, before_state, after_state, stop_reason)
        differ = self._differ if self._differ is not None else turn_diff.diff_bytes
        per_file_deadline = min(self._clock() + self.limits.diff_deadline_ms / 1000.0, deadline)
        try:
            stats = differ(
                before,
                after,
                max_bytes=self.limits.max_file_bytes,
                deadline=per_file_deadline,
                cancelled=cancelled,
            )
        except Exception as exc:  # noqa: BLE001 - differ 失败只降级本条
            logger.warning(
                "turn-change store: diff failed for %s (%s: %s)", entry.path, type(exc).__name__, exc
            )
            return self._degraded_entry(entry, before, after, state, before_state, after_state, REASON_ERROR)
        # 行数（added/removed）仅是展示数据：零行差不代表字节未变。净状态由
        # 存在性与字节决定（两侧字节相同已在 _resolve_snapshot 判为 unchanged）。
        if stats.quality == turn_diff.DIFF_COARSE:
            compare = COMPARE_COARSE
            added, removed = stats.added, stats.removed
        elif stats.quality == turn_diff.DIFF_FULL:
            compare = COMPARE_FULL
            added, removed = stats.added, stats.removed
        else:
            compare = COMPARE_NONE
            added, removed = None, None
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=before_state,
                after_state=after_state,
                added=added,
                removed=removed,
                compare=compare,
                reason=stats.reason or "",
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=before,
            after_bytes=after,
        )

    def _stop_reason(self, deadline: float, cancelled: Callable[[], bool] | None) -> str:
        """返回空串表示可继续，否则给出降级原因（取消优先于超预算）."""
        if cancelled is not None:
            try:
                if cancelled():
                    return turn_diff.REASON_CANCELLED
            except Exception as exc:  # noqa: BLE001 - 取消信号出错按"已取消"处理
                logger.warning("turn-change store: cancel callback failed (%s); stopping diff work", exc)
                return turn_diff.REASON_CANCELLED
        if self._clock() >= deadline:
            return turn_diff.REASON_TIMEOUT
        return ""

    def _degraded_entry(
        self,
        entry: _PathEntry,
        before: bytes | None,
        after: bytes | None,
        state: str,
        before_state: str,
        after_state: str,
        reason: str,
    ) -> _ResolvedEntry:
        """超时/取消/错误：保留已确认状态，只丢掉计数（仅路径+大小）."""
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=state,
                before_state=before_state,
                after_state=after_state,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=reason or REASON_ERROR,
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=before,
            after_bytes=after,
        )

    def _degraded_without_read(self, entry: _PathEntry, reason: str) -> _ResolvedEntry:
        """取消/过期：停止可放弃捕获且 after 未读取 → 进未知区（review F5）.

        未读到最终状态、也没有可信最终指纹时，绝不能把"可能发生过写操作"报成
        modified/added；保留 before 侧与停止原因即可。
        """
        return self._unknown_entry(
            entry, entry.before_state or SIDE_UNCAPTURED, reason, SIDE_UNCAPTURED, None
        )

    def _no_change(self, entry: _PathEntry, before_state: str, after_state: str) -> FileChange:
        return FileChange(
            path=entry.path,
            display=entry.path,
            state=STATE_UNCHANGED,
            before_state=before_state,
            after_state=after_state,
            added=0,
            removed=0,
            compare=COMPARE_FULL,
            reason="",
            checkpoint_ids=list(entry.checkpoint_ids),
        )

    def _unknown_entry(
        self,
        entry: _PathEntry,
        before_state: str,
        reason: str,
        after_state: str,
        after_bytes: bytes | None = None,
    ) -> _ResolvedEntry:
        return _ResolvedEntry(
            change=FileChange(
                path=entry.path,
                display=entry.path,
                state=STATE_UNKNOWN,
                before_state=before_state,
                after_state=after_state,
                added=None,
                removed=None,
                compare=COMPARE_NONE,
                reason=reason,
                checkpoint_ids=list(entry.checkpoint_ids),
            ),
            before_bytes=entry.before,
            after_bytes=after_bytes,
        )


def _tracked_state(before_fp: str | None, after_fp: str | None) -> str | None:
    """Frozen ``note_tracked`` decision table (§3.2 behavior 4)."""
    if before_fp is not None and after_fp is not None:
        if before_fp == after_fp:
            return None  # unchanged → 不出现
        if before_fp == _TRACKED_MISSING and _tracked_has_content(after_fp):
            return STATE_MODIFIED  # 重建
        if _tracked_has_content(before_fp) and after_fp == _TRACKED_MISSING:
            return STATE_DELETED
        return STATE_MODIFIED
    if before_fp is None:
        if after_fp == _TRACKED_MISSING:
            return STATE_DELETED
        if _tracked_has_content(after_fp):
            return STATE_MODIFIED
        return STATE_UNKNOWN  # None → None：无从确认
    if after_fp is None:
        return STATE_MODIFIED
    return STATE_UNKNOWN  # pragma: no cover - 穷尽分支


def _dir_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


def _check_dir_anchor(
    prefix: Path, fd: int, anchors: dict[Path, tuple[int, int]] | None
) -> None:
    """Refuse a directory whose already-opened handle no longer matches its anchor."""
    if not anchors:
        return
    expected = anchors.get(prefix)
    if expected is None:
        return
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != expected:
        raise OSError(f"directory identity changed for {prefix}")


def _walk_open_parent(
    path: Path, anchors: dict[Path, tuple[int, int]] | None = None
) -> tuple[int, str]:
    """Open ``path.parent`` component-wise, refusing symlinked components.

    Every component is opened with ``O_NOFOLLOW`` relative to the previous one
    and any component with a recorded anchor must still match it, so the fd the
    caller then uses for real I/O cannot be redirected by a later swap
    (review F2/G1). Returns ``(parent_fd, name)``; the caller closes the fd.
    """
    if os.name == "nt":  # Windows 无可比的 O_NOFOLLOW/O_DIRECTORY：退化为普通打开
        return os.open(str(path.parent), os.O_RDONLY), path.name
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    parts = path.parent.parts
    fd = os.open(parts[0], _dir_open_flags())
    prefix = Path(parts[0])
    try:
        _check_dir_anchor(prefix, fd, anchors)
        for part in parts[1:]:
            next_fd = os.open(part, _dir_open_flags() | no_follow, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            prefix = prefix / part
            _check_dir_anchor(prefix, fd, anchors)
    except BaseException:
        os.close(fd)
        raise
    return fd, path.name


def _read_bounded_fd(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_bounded_path(path: Path, limit: int) -> bytes:
    """Path-based bounded read used where directory handles are unavailable."""
    # Windows opens in text mode unless asked otherwise: "\r\n" would be read
    # as "\n" and the read would end at the first 0x1A byte.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        return _read_bounded_fd(fd, limit)
    finally:
        os.close(fd)


def _read_bytes_bounded(
    path: Path, limit: int, *, anchors: dict[Path, tuple[int, int]] | None = None
) -> bytes:
    """Read at most ``limit + 1`` bytes bound to verified directory handles.

    ``path.parent`` is opened component-wise (no symlinked component, anchors
    re-checked on the opened objects), the file itself is opened relative to
    that handle with ``O_NOFOLLOW``, and the bytes come from the same fd, so a
    swap after earlier checks cannot redirect the read (review F2/G1/R6).
    """
    if not _HANDLE_IO_OK:
        return _read_bounded_path(path, limit)
    parent_fd, name = _walk_open_parent(path, _effective_anchors(anchors))
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            return _read_bounded_fd(fd, limit)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _read_all_bytes(
    path: Path, *, anchors: dict[Path, tuple[int, int]] | None = None
) -> bytes:
    """Read a snapshot file fully through verified directory handles (G1)."""
    if not _HANDLE_IO_OK:
        return path.read_bytes()
    parent_fd, name = _walk_open_parent(path, _effective_anchors(anchors))
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _read_text_at(dir_fd: int, name: str) -> str:
    """Read one file relative to an already-verified directory handle (G1)."""
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def _write_all_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _write_bytes(
    path: Path, data: bytes, *, anchors: dict[Path, tuple[int, int]] | None = None
) -> None:
    """Write a snapshot file through a verified parent handle (G1)."""
    if not _HANDLE_IO_OK:
        path.write_bytes(data)
        return
    parent_fd, name = _walk_open_parent(path, _effective_anchors(anchors))
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            _write_all_fd(fd, data)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _write_json_atomic(
    path: Path,
    payload: dict[str, Any],
    *,
    anchors: dict[Path, tuple[int, int]] | None = None,
) -> None:
    """Atomic JSON write (temp + ``os.replace``) through one parent handle (G1)."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not _HANDLE_IO_OK:
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_bytes(body)
        os.replace(temp, path)
        return
    parent_fd, name = _walk_open_parent(path, _effective_anchors(anchors))
    temp_name = f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                _write_all_fd(fd, body)
            finally:
                os.close(fd)
            os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(parent_fd)


def _remove_children(fd: int) -> None:
    """Delete everything inside an already-opened directory (files + subdirs)."""
    for entry in os.listdir(fd):
        st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            child_fd = os.open(entry, _dir_open_flags() | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            try:
                _remove_children(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry, dir_fd=fd)
        else:
            os.unlink(entry, dir_fd=fd)


def _fd_identity(fd: int) -> tuple[int, int] | None:
    """Identity ``(dev, ino)`` of an already-open directory; ``None`` if unreadable."""
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _name_identity(parent_fd: int, name: str) -> tuple[int, int] | None:
    """Identity of ``name`` under an open parent handle, without following links."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


@contextmanager
def _remove_expectation_scope(
    identity: tuple[int, int] | None, owner: dict[str, Any] | None
) -> Iterator[None]:
    """Bind the verified identity/owner a removal must still match (review H1).

    Same pattern as the I/O anchors: the expectation travels in the context so
    fault-injection wrappers around ``_remove_tree_at(parent_fd, name)`` keep
    their signature while the real deletion still re-verifies the opened child.
    """
    token = _REMOVE_EXPECT.set((identity, owner))
    try:
        yield
    finally:
        _REMOVE_EXPECT.reset(token)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one directory tree relative to a verified parent handle (G1/H1).

    The opened child is re-verified against the caller's expectation through the
    same handle (identity and, when known, the owner marker), and the directory
    entry is only removed while ``name`` still resolves to that same inode — a
    directory moved into place between the checks is left untouched.
    """
    fd = os.open(name, _dir_open_flags() | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        expected_identity, expected_owner = _REMOVE_EXPECT.get() or (None, None)
        if expected_identity is not None and _fd_identity(fd) != expected_identity:
            raise OSError(
                f"{name!r} is not the previously verified directory; refusing removal"
            )
        if expected_owner is not None:
            try:
                current_owner = json.loads(_read_text_at(fd, OWNER_NAME))
            except (OSError, ValueError, TypeError):
                current_owner = None
            if current_owner != expected_owner:
                raise OSError(f"owner of {name!r} is not ours anymore; refusing removal")
        _remove_children(fd)
        if _fd_identity(fd) != _name_identity(parent_fd, name):
            # 目录项已被替换：绝不按名字删除换入的目录（review H1）
            raise OSError(f"{name!r} was replaced while being removed; refusing removal")
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _open_deepest_ancestor(path: Path) -> tuple[int, list[str]]:
    """Open the deepest existing ancestor of ``path`` component-wise (G1).

    Returns ``(fd, missing_names)`` where ``missing_names`` are the components
    that still have to be created below the opened directory.  Symlinked
    components raise instead of being followed.
    """
    if os.name == "nt":  # Windows 退化为逐级打开
        missing: list[str] = []
        fd = os.open(path.anchor or "/", os.O_RDONLY)
        for part in path.parts[1:]:
            if missing:
                missing.append(part)
                continue
            try:
                next_fd = os.open(part, os.O_RDONLY, dir_fd=fd)
            except FileNotFoundError:
                missing.append(part)
                continue
            os.close(fd)
            fd = next_fd
        return fd, missing
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    parts = path.parts
    fd = os.open(parts[0], _dir_open_flags())
    missing = []
    try:
        for part in parts[1:]:
            if missing:
                missing.append(part)
                continue
            try:
                next_fd = os.open(part, _dir_open_flags() | no_follow, dir_fd=fd)
            except FileNotFoundError:
                missing.append(part)
                continue
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd, missing


def _payload_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _record_seq(record: Any) -> int | None:
    """``turn_seq`` of one index record; ``None`` when it is unusable (R1)."""
    if not isinstance(record, dict):
        return None
    return _optional_int(record.get("turn_seq"))


def _valid_index_record(record: Any) -> bool:
    """A record is trusted only when its shape is exactly what the store writes."""
    if _record_seq(record) is None:
        return False
    if not isinstance(record.get("empty"), bool):
        return False
    if not isinstance(record.get("request_id"), str):
        return False
    dir_name = record.get("dir")
    if dir_name is not None:
        if not isinstance(dir_name, str) or not _TURN_DIR_RE.fullmatch(dir_name):
            return False
    created_at = record.get("created_at")
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        return False
    return all(_optional_int(record.get(field)) is not None for field in ("files", "unknown"))


def _manifest_from_payload(payload: dict[str, Any]) -> TurnChangesManifest:
    """Rebuild a manifest from its persisted payload (single read path)."""
    totals = payload.get("totals")
    return TurnChangesManifest(
        session_id=str(payload.get("session_id") or ""),
        request_id=str(payload.get("request_id") or ""),
        turn_seq=int(payload.get("turn_seq") or 0),
        created_at=float(payload.get("created_at") or 0.0),
        files=[_change_from_payload(item) for item in _payload_list(payload.get("files"))],
        unknown=[_change_from_payload(item) for item in _payload_list(payload.get("unknown"))],
        totals=(
            {str(key): int(value) for key, value in totals.items()}
            if isinstance(totals, dict)
            else {}
        ),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _change_payload(
    change: FileChange,
    sides: dict[str, str | None],
    *,
    before_state: str | None = None,
    after_state: str | None = None,
) -> dict[str, Any]:
    return {
        "path": change.path,
        "display": change.display,
        "state": change.state,
        "before_state": before_state if before_state is not None else change.before_state,
        "after_state": after_state if after_state is not None else change.after_state,
        "added": change.added,
        "removed": change.removed,
        "compare": change.compare,
        "reason": change.reason,
        "checkpoint_ids": list(change.checkpoint_ids),
        "before_file": sides.get("before"),
        "after_file": sides.get("after"),
    }


def _change_from_payload(raw: Any) -> FileChange:
    if not isinstance(raw, dict):
        raise TypeError("manifest entry must be an object")
    checkpoint_ids = raw.get("checkpoint_ids")
    return FileChange(
        path=str(raw.get("path") or ""),
        display=str(raw.get("display") or raw.get("path") or ""),
        state=str(raw.get("state") or STATE_UNKNOWN),
        before_state=str(raw.get("before_state") or SIDE_UNCAPTURED),
        after_state=str(raw.get("after_state") or SIDE_UNCAPTURED),
        added=_optional_int(raw.get("added")),
        removed=_optional_int(raw.get("removed")),
        compare=str(raw.get("compare") or COMPARE_NONE),
        reason=str(raw.get("reason") or ""),
        checkpoint_ids=[str(item) for item in checkpoint_ids] if isinstance(checkpoint_ids, list) else [],
    )


def _safe_dir_anchor(path: Path) -> tuple[int, int] | None:
    """Best-effort directory identity; ``None`` when it cannot be read safely."""
    try:
        return _open_dir_anchor(path)
    except OSError:
        return None


_LOCK_DIR_NAME = ".locks"
# 目录句柄式 I/O（O_NOFOLLOW / dir_fd / flock）只在 POSIX 可用；
# Windows 退化为带既有弱点的路径式实现，而不是直接不可用（G1）。
_HANDLE_IO_OK = os.name != "nt"
# 会话锁的重入只允许"同一执行所有者 + 同一把锁"的嵌套调用；并行 asyncio task、
# 其他 session 的锁都必须各自获取文件锁（review H2）。子任务会继承创建时的
# 上下文，因此还要比对执行所有者，不能只看"上下文里有没有标记"。
_GUARD_HELD: ContextVar[tuple[str, object, str, int] | None] = ContextVar(
    "turn_change_guard_held", default=None
)


def _guard_owner() -> tuple[str, object]:
    """Execution owner of the current call: the asyncio task, else the OS thread."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return ("task", task)
    return ("thread", threading.get_ident())
_IO_ANCHORS: ContextVar[dict[Path, tuple[int, int]] | None] = ContextVar(
    "turn_change_io_anchors", default=None
)
_REMOVE_EXPECT: ContextVar[
    tuple[tuple[int, int] | None, dict[str, Any] | None] | None
] = ContextVar("turn_change_remove_expect", default=None)


@contextmanager
def _io_anchor_scope(
    anchors: dict[Path, tuple[int, int]] | None,
) -> Iterator[None]:
    """Bind the verified directory identities for the current I/O scope (G1).

    Passed through a context variable instead of extra call arguments so that
    wrappers around the low-level I/O helpers (tests, probes, instrumentation)
    keep working while the real open still verifies every opened directory.
    """
    token = _IO_ANCHORS.set(anchors or None)
    try:
        yield
    finally:
        _IO_ANCHORS.reset(token)


def _effective_anchors(
    anchors: dict[Path, tuple[int, int]] | None,
) -> dict[Path, tuple[int, int]] | None:
    return anchors if anchors is not None else _IO_ANCHORS.get()


def _ensure_dir_chain(path: Path) -> bool:
    """Create ``path`` (and missing components) relative to verified handles (G1)."""
    if not _HANDLE_IO_OK:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("turn-change store: cannot create %s (%s)", path, exc)
            return False
        return True
    try:
        fd, missing = _open_deepest_ancestor(path)
    except OSError as exc:
        logger.warning("turn-change store: cannot reach %s (%s)", path, exc)
        return False
    try:
        for name in missing:
            try:
                os.mkdir(name, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning("turn-change store: cannot create %s (%s)", path, exc)
                return False
            next_fd = os.open(
                name, _dir_open_flags() | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
    except OSError as exc:
        logger.warning("turn-change store: cannot create %s (%s)", path, exc)
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _lock_file_handle(
    lock_path: Path, timeout: float = 2.0, stop: Callable[[], bool] | None = None
) -> Any | None:
    """Advisory exclusive lock on ``lock_path``; ``None`` when unavailable.

    ``flock`` on POSIX keeps separate open file descriptions apart, so two
    threads of one process exclude each other as well; Windows uses
    ``msvcrt.locking``. The lock file is opened through verified directory
    handles (G1); the retry loop is bounded by ``timeout`` and stops as soon as
    ``stop()`` reports cancellation/expiry (G3).
    """
    if _HANDLE_IO_OK:
        try:
            parent_fd, name = _walk_open_parent(lock_path)
        except OSError:
            return None
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT, dir_fd=parent_fd)
        except OSError:
            return None
        finally:
            os.close(parent_fd)
        try:
            handle = os.fdopen(fd, "r+b")
        except OSError:
            os.close(fd)
            return None
    else:
        try:
            handle = open(lock_path, "a+b")
        except OSError:
            return None
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return handle
            except OSError:
                if stop is not None and stop():
                    handle.close()
                    return None
                if time.monotonic() >= deadline:
                    handle.close()
                    return None
                time.sleep(0.01)
    import fcntl

    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            # 先试一次非阻塞获取；重试才受取消/预算约束（review G3）
            if stop is not None and stop():
                handle.close()
                return None
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.01)


def _unlock_file_handle(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


@contextmanager
def _session_guard(
    lock_path: Path,
    *,
    wait_seconds: float | None = None,
    stop: Callable[[], bool] | None = None,
) -> Iterator[bool]:
    """Cross-instance exclusion for one session area (review F1/G3/H2).

    真正的嵌套（同一执行所有者 + 同一把锁）直接复用外层锁；并行 asyncio task、
    其他 session 的锁都必须各自获取文件锁。等待受 ``wait_seconds``（通常是回合
    剩余预算）和 ``stop()`` 约束；取不到锁时产出 ``False``，调用方保守降级
    （拒绝写入/删除）。
    """
    key = str(lock_path)
    owner = _guard_owner()
    held = _GUARD_HELD.get()
    if held is not None and held[0] == owner[0] and held[1] == owner[1] and held[2] == key:
        _GUARD_HELD.set((owner[0], owner[1], key, held[3] + 1))
        try:
            yield True
        finally:
            _GUARD_HELD.set(held)
        return
    total = 2.0 if wait_seconds is None else max(0.0, float(wait_seconds))
    deadline = time.monotonic() + total
    handle: Any = None
    token: Any = None
    try:
        try:
            if _ensure_dir_chain(lock_path.parent):
                handle = _lock_file_handle(
                    lock_path,
                    timeout=max(0.0, deadline - time.monotonic()),
                    stop=stop,
                )
        except OSError as exc:
            logger.warning("turn-change store: cannot lock %s (%s)", lock_path, exc)
        if handle is None:
            logger.warning(
                "turn-change store: session lock unavailable for %s; degrading", lock_path
            )
        else:
            # 只有文件锁真正到手才算"持有"，嵌套调用据此复用（review H2）
            token = _GUARD_HELD.set((owner[0], owner[1], key, 1))
        yield handle is not None
    finally:
        if token is not None:
            _GUARD_HELD.reset(token)
        if handle is not None:
            _unlock_file_handle(handle)


def _cleanup_orphans_path_based(base: Path, probe: Callable[[int], bool]) -> list[str]:
    """Windows fallback for :meth:`TurnChangeStore.cleanup_orphans` (no dir_fd)."""
    removed: list[str] = []
    try:
        children = sorted(path for path in base.iterdir() if path.is_dir())
    except OSError:
        return removed
    for child in children:
        if child.name == _LOCK_DIR_NAME:
            continue
        if child.is_symlink():
            logger.warning("turn-change store: skip %s (symbolic link)", child)
            continue
        with _session_guard(base / _LOCK_DIR_NAME / f"{child.name}.lock") as locked:
            if not locked:
                logger.warning("turn-change store: skip %s (session lock unavailable)", child)
                continue
            if _remove_orphan_session_path(child, probe):
                removed.append(str(child))
    return removed


def _remove_orphan_session_path(child: Path, probe: Callable[[int], bool]) -> bool:
    """Windows fallback for :func:`_remove_orphan_session` (path-based)."""
    try:
        raw = (child / OWNER_NAME).read_text(encoding="utf-8")
        owner = json.loads(raw)
    except (OSError, ValueError, TypeError):
        logger.warning("turn-change store: skip %s (no readable owner.json)", child)
        return False
    pid = owner.get("pid") if isinstance(owner, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        logger.warning("turn-change store: skip %s (owner.json has no usable pid)", child)
        return False
    try:
        alive = bool(probe(pid))
    except Exception as exc:  # noqa: BLE001 - 探活失败即视为"无法确认"
        logger.warning("turn-change store: skip %s (liveness probe failed: %s)", child, exc)
        return False
    if alive:
        return False
    try:
        current = (child / OWNER_NAME).read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != raw:
        logger.warning("turn-change store: skip %s (owner changed before removal)", child)
        return False
    try:
        shutil.rmtree(child)
    except OSError as exc:
        logger.warning("turn-change store: cannot remove orphan %s (%s)", child, exc)
        return False
    return True


def _remove_orphan_session(
    root_fd: int, name: str, display: Path, probe: Callable[[int], bool]
) -> bool:
    """Remove one orphaned session area relative to the root handle (F1/G1).

    读取、复核与删除都绑定在同一个已打开目录句柄上：路径被替换不会让清理
    走到别处，删除前也仍复核同一 owner 身份（review R7/G1）。
    """
    try:
        child_fd = os.open(
            name, _dir_open_flags() | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd
        )
    except OSError as exc:
        logger.warning("turn-change store: skip %s (unreadable area: %s)", display, exc)
        return False
    try:
        try:
            raw = _read_text_at(child_fd, OWNER_NAME)
        except (OSError, UnicodeDecodeError):
            # 可能是其他实例刚建目录、尚未写入 owner 的窗口期：宁可不清理
            logger.warning("turn-change store: skip %s (no readable owner.json)", display)
            return False
        try:
            owner = json.loads(raw)
        except ValueError:
            logger.warning("turn-change store: skip %s (owner.json unreadable)", display)
            return False
        pid = owner.get("pid") if isinstance(owner, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            logger.warning("turn-change store: skip %s (owner.json has no usable pid)", display)
            return False
        try:
            alive = bool(probe(pid))
        except Exception as exc:  # noqa: BLE001 - 探活失败即视为"无法确认"
            logger.warning("turn-change store: skip %s (liveness probe failed: %s)", display, exc)
            return False
        if alive:
            return False
        # 删除前复核同一 owner 身份：读取后、删除前所有权易主则跳过（review R7）
        try:
            current = _read_text_at(child_fd, OWNER_NAME)
        except (OSError, UnicodeDecodeError):
            current = None
        if current != raw:
            logger.warning("turn-change store: skip %s (owner changed before removal)", display)
            return False
        if _fd_identity(child_fd) != _name_identity(root_fd, name):
            # 目录项在核验后被替换：不按名字删除换入的目录（review H1）
            logger.warning("turn-change store: skip %s (entry replaced before removal)", display)
            return False
        try:
            _remove_children(child_fd)
            os.rmdir(name, dir_fd=root_fd)
        except OSError as exc:
            logger.warning("turn-change store: cannot remove orphan %s (%s)", display, exc)
            return False
        return True
    finally:
        os.close(child_fd)


def _open_dir_anchor(path: Path) -> tuple[int, int]:
    """Directory identity of ``path`` without following a swapped final symlink.

    Returns ``(st_dev, st_ino)`` of the physical directory. Raises ``OSError``
    when the final component is a symbolic link (the shape of the audit
    path-swap attack), so callers degrade instead of writing through the swap.
    """
    if os.name != "nt":
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_fd = os.open(str(path.parent), dir_flags)
        try:
            last_fd = os.open(
                path.name, dir_flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
            )
        finally:
            os.close(parent_fd)
        try:
            st = os.fstat(last_fd)
            return (st.st_dev, st.st_ino)
        finally:
            os.close(last_fd)
    if stat.S_ISLNK(os.lstat(path).st_mode):
        raise OSError(f"workspace path is a symbolic link: {path}")
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe; anything uncertain counts as alive."""
    if pid == os.getpid():
        return True
    try:
        return pid_alive(pid)
    except Exception:
        return True


def current_turn_change_store() -> TurnChangeStore | None:
    """回合上下文中的会话 store（files.py 旁路与 react 接线共用的单一来源）."""
    return _CURRENT.get()


@contextmanager
def turn_store_scope(store: TurnChangeStore | None) -> Iterator[None]:
    """Bind ``store`` for the current turn; nesting restores the previous value."""
    token = _CURRENT.set(store)
    try:
        yield
    finally:
        _CURRENT.reset(token)
