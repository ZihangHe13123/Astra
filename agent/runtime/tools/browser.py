"""Browser tools: model-callable interface over BrowserSessionManager.

Phase 3 ROADMAP item — wire the durable browser state machine into the tool
registry so the model can drive the execution ladder:

    browser_open / browser_snapshot / browser_extract
    browser_click / browser_type
    browser_handoff / browser_resume

These close the gap declared in capabilities.py (browser.interactive was
listed as a target contract but had no registered tools). The concrete page
transport is a pluggable BrowserBackend; with only the legacy read-only
adapter configured, interactive tools fail with a clear capability error
rather than pretending to work.

Security invariants (inherited from browser_session):
  - Snapshots are sanitized before they reach the model or disk.
  - Interactive tools never accept raw credentials; selectors/text only.
  - profile_ref is an opaque label, never a secret.
"""

from __future__ import annotations

import json
import hashlib
import inspect
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, cast
from ..browser_control_transport import BROWSER_ENDPOINT_RECOVERY, BrowserEndpointOwnedError

from ..browser_session import (
    BrowserMode,
    BrowserSessionManager,
    LegacyBrowserExtractBackend,
    format_session,
    sanitize_snapshot,
)
from ..tool_failure import ToolFailure
from ..browser_lifecycle import BrowserLifecycle
from .approval import ScopedApprovalStore, normalized_origin
from .registry import ToolDef, ToolRegistry

logger = logging.getLogger(__name__)


async def _await_backend_result(result: object) -> object:
    """Narrow optional transport hooks without weakening the core protocol."""
    if not inspect.isawaitable(result):
        raise TypeError("browser backend operation must be awaitable")
    return await result


def register_browser_tools(
    registry: ToolRegistry,
    *,
    manager: BrowserSessionManager | None = None,
    backend=None,
    extract_fn=None,
    status_fn=None,
    enable_extension: bool = False,
    workdir: str = ".",
    filesystem_policy=None,
) -> BrowserSessionManager:
    """Register browser tools and return the session manager.

    If no manager is supplied, one is created. Backend priority:
    1. Explicit ``backend`` object (e.g. CdpBrowserBackend)
    2. ``extract_fn`` + ``status_fn`` wrapped in LegacyBrowserExtractBackend
    """
    if manager is None:
        manager = BrowserSessionManager()
    if manager.backend is None:
        if backend is not None:
            manager.backend = backend
        elif extract_fn is not None and status_fn is not None:
            manager.backend = LegacyBrowserExtractBackend(extract_fn, status_fn)

    if enable_extension and manager.backend is not None:
        from ..browser_backend_router import BrowserBackendRouter
        from ..extension_browser_backend import ExtensionBrowserBackend
        from ..browser_autoconnect import auto_browser_options
        manager.backend = BrowserBackendRouter(manager.backend, ExtensionBrowserBackend, **auto_browser_options())

    lifecycle = BrowserLifecycle(manager)
    active = lifecycle.active
    registry.hooks.on_session_end(lifecycle.end_session)
    registry.hooks.on_before_tool(lifecycle.before_tool)

    def register(tool: ToolDef) -> None:
        tool.fn = lifecycle.wrap(tool.fn)
        # Cached results cannot establish ownership or fresh remote references.
        tool.cache_results = False
        registry.register(tool)

    browser_approvals = ScopedApprovalStore(
        enabled=lambda: registry.approval_handler is not None,
        approved_scopes=registry.approved_permission_scopes,
    )
    from .files import FilesystemPolicy
    from ..browser_upload import inspect_files
    file_access = filesystem_policy or FilesystemPolicy.load(workdir)
    # A generic per-origin write grant must never authorize arbitrary local files.
    upload_approvals = ScopedApprovalStore(approved_scopes=registry.approved_permission_scopes)

    def _upload_context(args):
        if not args.get("tab_id") or not str(args.get("selector", "")).startswith("ref:"):
            raise ValueError("browser_upload requires an explicit tab_id and a fresh file input ref")
        tab, error = _resolve_tab(args["tab_id"])
        if error:
            raise ValueError(error)
        files = inspect_files(args.get("paths"), file_access.workspace)
        origin = normalized_origin(tab.url)
        if not origin:
            raise ValueError("Upload requires an HTTP(S) origin")
        binding = [origin, tab.tab_id, args["selector"], args.get("frame_ref", ""),
                   [(str(f.path), f.fingerprint) for f in files]]
        scope = "browser-upload:" + hashlib.sha256(json.dumps(binding).encode()).hexdigest()
        return tab, files, scope

    def _upload_permission_check(args):
        tab, files, scope = _upload_context(args)
        request = upload_approvals.request(scope=scope, kind="browser_upload", operation="Select browser files",
            target=normalized_origin(tab.url), reason="Provide these exact local files to this website",
            detail="The site may upload on selection. This does not authorize clicking Submit or other local files.",
            arguments={"tab_id": tab.tab_id, "selector": args["selector"], "paths": json.dumps([str(f.path) for f in files])},
            approval_title="向网页提供指定文件", approval_effect="读取列出的文件并选择到目标网页；网站可能立即上传。",
            approval_boundary="仅此文件列表、文件身份和目标控件；不包含 Submit")
        if request is not None:
            request["files"] = [{**f.metadata(), "path": str(f.path)} for f in files]
            request["filesystem_requests"] = [r for f in files if
                (r := file_access.permission_request(str(f.path), write=False, operation="Upload file"))]
        return request

    async def _browser_upload(tab_id: str, selector: str, paths: list[str], frame_ref: str = ""):
        try:
            tab, files, scope = _upload_context({"tab_id": tab_id, "selector": selector, "paths": paths, "frame_ref": frame_ref})
            if scope not in upload_approvals.approved_scopes:
                return _failure("Exact upload authorization is missing or the file/target changed", "approval_required")
            method = getattr(manager.backend, "interactive_upload", None)
            if not callable(method):
                from ..browser_control_transport import BrowserUnsupportedOperation
                return await _finish_action(tab_id, json.dumps(BrowserUnsupportedOperation("browser_upload").result()))
            error = _interactive_guard(tab)
            if error:
                return _failure(error)
            # Combined approval includes the listed local reads. Grants are exact
            # paths and revoked in finally, independently of session origin grants.
            grants = [file_access.grant(str(file.path), "ro") for file in files]
            try:
                for file in files:
                    if file_access.resolve(str(file.original)) != file.path:
                        raise ValueError("Upload path changed after authorization")
                result = await _await_backend_result(method(selector, files, tab_id=tab_id, url=tab.url, frame_ref=frame_ref))
                return await _finish_action(tab_id, str(result))
            finally:
                for grant in reversed(grants):
                    file_access.revoke(grant)
        except (OSError, ValueError, RuntimeError) as exc:
            return _connection_failure("browser_upload", exc)

    def _get_or_create_session() -> str:
        session_id = active.get("session_id", "")
        if session_id and manager.get_session(session_id) is not None:
            return session_id
        session = manager.create_session()
        active["session_id"] = session.session_id
        return session.session_id

    def _resolve_tab(tab_id: str = "") -> tuple[Any, str]:
        """Resolve a tab, defaulting to the current tab of the active session.

        Returns (tab, error). On error, tab is None.
        """
        if tab_id:
            owner = manager.get_session(active.get("session_id", ""))
            if owner is None or tab_id not in owner.tabs:
                return None, "[Browser Error] Unknown tab or expired handle for this session. Call browser_open/browser_connect and observe again."
            tab = manager.get_tab(tab_id)
            if tab is None:
                return None, f"[Browser Error] Unknown tab: {tab_id}"
            return tab, ""
        session_id = active.get("session_id", "")
        session = manager.get_session(session_id) if session_id else None
        if session is None or not session.current_tab_id:
            return None, "[Browser Error] No active tab. Call browser_open first."
        tab = manager.get_tab(session.current_tab_id)
        if tab is None:
            return None, "[Browser Error] Active tab disappeared. Call browser_open."
        return tab, ""

    def _browser_write_permission_check(operation: str, value_key: str = ""):
        def check(args: dict) -> dict | None:
            tab, error = _resolve_tab(str(args.get("tab_id") or ""))
            if error or tab is None:
                return None
            origin = normalized_origin(str(tab.url)) or f"tab:{tab.tab_id}"
            selector = str(args.get("selector") or "")
            preview: dict[str, Any] = {
                "tab_id": str(tab.tab_id),
                "selector": selector,
            }
            if operation == "Set page choices":
                preview["checks"] = args.get("checks") or [{"selector": selector, "checked": args.get("checked", True)}]
            if value_key:
                value = str(args.get(value_key) or "")
                preview[value_key] = (
                    f"<{len(value)} chars>"
                    if value_key == "text"
                    else value[:240]
                )
            return browser_approvals.request(
                scope=f"browser-write:{origin}",
                kind="browser_write",
                operation=operation,
                target=f"{origin} · {selector or '(current page)'}",
                reason=f"{operation} may change page or external account state",
                detail="Session approval is limited to browser write actions on this origin.",
                arguments=preview,
            )

        return check

    def _profile_dir(tab_id: str) -> str:
        """Resolve an opaque profile reference to a project-owned directory."""
        owner = None
        for session in manager.list_sessions(limit=100):
            if tab_id in session.tabs:
                owner = session
                break
        label = (owner.profile_ref if owner else "") or (owner.session_id if owner else "default")
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip(".-") or "default"
        profile = Path(manager.path).parent / "browser-profiles" / safe_label[:80]
        profile.mkdir(parents=True, exist_ok=True)
        return str(profile)

    async def _sync_interactive_state(tab_id: str, *, expected_origin: str = "") -> Any:
        backend = manager.backend
        if backend is None:
            return manager.get_tab(tab_id)
        url, title, snapshot = await backend.interactive_state(tab_id=tab_id)
        if expected_origin and normalized_origin(url) != expected_origin:
            raise PermissionError("Cross-origin redirect blocked; open or attach the new page explicitly")
        return manager.update_page_state(
            tab_id, url=url, title=title, snapshot=snapshot[:65536]
        )

    async def _assert_write_origin(tab) -> None:
        check = getattr(manager.backend, "assert_origin", None)
        if callable(check):
            await _await_backend_result(check(tab_id=tab.tab_id, expected_url=tab.url))

    _failed_statuses = {"error", "stale_snapshot", "ambiguous_target", "target_not_found",
                        "verification_failed", "unknown_outcome", "timeout", "unsupported_operation", "invalid_arguments"}

    def _failure(message: str, code: str = "browser_error", *, partial: bool = False,
                 recovery_hint: str = "", details: dict | None = None) -> ToolFailure:
        return ToolFailure(code=code, message=sanitize_snapshot(message), retryable=False, partial=partial,
            details=details or {}, recovery_hint=recovery_hint or
            "Observe the target with browser_snapshot/browser_read before deciding the next action. Do not replay uncertain writes or fall back to global keyboard/paste.")

    def _interaction_failure(operation: str, message: str, *, dispatched: bool = False) -> ToolFailure:
        mutating = operation in {"click", "type", "fill", "check"}
        return _failure(message, partial=mutating and dispatched, details={
            "dispatch_state": "unknown" if dispatched else "not_dispatched",
            "operation": operation,
        } if mutating else None)

    def _connection_failure(operation: str, exc: Exception) -> ToolFailure:
        if isinstance(exc, BrowserEndpointOwnedError):
            return ToolFailure(code=exc.code, message=str(exc), retryable=False,
                recovery_hint=BROWSER_ENDPOINT_RECOVERY,
                details={"dispatch_state": "not_dispatched", "next_step": "use_available_channel",
                         "owner_pid": exc.owner_pid})
        return _failure(f"{operation} failed: {exc}")

    async def _finish_action(tab_id: str, result: str, *, expected_text: str | None = None) -> str | ToolFailure:
        # Re-observing would invalidate the element refs in the action's own
        # postcondition snapshot. Persist and return that exact observation.
        try:
            payload = json.loads(result)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and expected_text is not None and isinstance(payload.get("value"), str):
            actual = payload["value"]
            expected = expected_text.replace("\r\n", "\n").replace("\r", "\n")
            complete = payload.get("valueTruncated") is False
            payload["value_comparison"] = {
                "exact_match": actual == expected if complete else None,
                "whitespace_equivalent": re.sub(r"\s+", " ", actual).strip() == re.sub(r"\s+", " ", expected).strip() if complete else None,
            }
            if payload["value_comparison"]["exact_match"] is False and payload.get("verified") is True:
                payload["verified"] = False
                payload["status"] = "verification_failed"
        after = payload.get("after") if isinstance(payload, dict) else None
        if isinstance(after, dict) and isinstance(after.get("url"), str):
            updated = manager.update_page_state(tab_id, url=after["url"],
                title=str(after.get("title", "")),
                snapshot=json.dumps(after, ensure_ascii=False)[:65536])
        elif isinstance(payload, dict) and (payload.get("status") in _failed_statuses | {"no_observed_change"} or "target" in payload):
            # Unknown dispatch must not trigger a reconnect or implicit replay.
            updated = manager.get_tab(tab_id)
        else:
            updated = await _sync_interactive_state(tab_id)
        if updated is None:
            return _failure("Active tab disappeared. Call browser_open.")
        if isinstance(payload, dict):
            # Existing structured fields stay intact for older callers. Give
            # models an explicit continuation rather than a vague status marker.
            value_verified = payload.get("verified") is True and payload.get("status") not in _failed_statuses | {"no_observed_change"}
            payload["continuation"] = {
                "next_step": "continue_from_after" if value_verified else "inspect_after_then_observe",
                "repeat_input": False,
                "instruction": (
                    "Target value is verified. Continue with fresh after refs or report briefly; application saving is separate."
                    if value_verified else
                    "Inspect after for the requested result. If not yet confirmed, use browser_read on the relevant container, "
                    "browser_wait with an observed condition, or a scoped browser_snapshot; an unchanged page does not authorize another click. "
                    "Changing the ref, selector, parent/child target, or channel is still a retry of the same intended action while its outcome is unconfirmed."
                ),
            }
            comparison = payload.get("value_comparison", {})
            if comparison.get("exact_match") is False:
                payload["continuation"]["instruction"] = (
                    "Exact text verification failed. Report this separately from verified fields; do not claim all fields matched. "
                    + ("Only whitespace differs; inspect whether that difference matters for this field. " if comparison.get("whitespace_equivalent") else "Inspect the actual value for missing or changed content. ")
                    + "Do not repeat the fill automatically; application saving is a separate check."
                )
            if payload.get("status") == "unsupported_operation":
                payload["continuation"] = {"next_step": "use_available_channel", "repeat_input": False,
                    "instruction": payload.get("recovery_hint", "This operation is unavailable; do not change CSS/ref syntax or retry it.")}
            elif payload.get("status") == "stale_snapshot":
                payload["continuation"] = {"next_step": "fresh_snapshot", "repeat_input": False,
                    "instruction": "This reference is expired. Observe once and use new refs; refreshing refs does not restore unsupported operations."}
            elif payload.get("status") == "timeout":
                payload["continuation"] = {"next_step": "inspect_after", "repeat_input": False,
                    "instruction": "The requested wait condition was not observed. Inspect the current after URL/text for the task result before waiting again; do not repeat the preceding input."}
            result = json.dumps(payload, ensure_ascii=False,
                separators=(",", ":") if isinstance(after, dict) and after.get("scope") == "form" else None)
        message = f"[Browser] tab {tab_id} ({updated.url}): {sanitize_snapshot(result)}"
        if isinstance(payload, dict) and payload.get("status") in _failed_statuses:
            return _failure(message, "browser_" + payload["status"],
                partial=payload.get("dispatch_state") in {"dispatched", "partial", "unknown"}
                or (payload.get("dispatch_state") != "not_dispatched" and payload["status"] in {"unknown_outcome", "verification_failed"}),
                recovery_hint=str(payload["continuation"]["instruction"]),
                details={key:payload[key] for key in ("dispatch_state", "operation", "wait") if key in payload})
        return message

    # ── fallback extraction ladder ─────────────────────────────────

    # Minimum chars to consider a static extraction "successful".
    # Below this, the page is likely a JS-rendered SPA and we escalate.
    _MIN_STATIC_CHARS = 200

    async def _fallback_extract(
        url: str,
        *,
        tab_id: str = "",
        max_length: int = 12000,
    ) -> tuple[str, str]:
        """Extract page content with automatic escalation.

        Returns (content, rung) where rung is one of:
        "static", "cdp", "screenshot", "error".

        Ladder:
        1. Static extract (--dump-dom / browser extractor) — fast, no JS
        2. CDP interactive (headless navigate + get_text) — handles SPA/JS
        3. Screenshot — last resort, returns image path for vision
        """
        backend = manager.backend
        enforce_requested_origin = registry.approval_handler is not None

        # ── Rung 1: Static extraction ──
        # Static extractors do not expose the final URL after redirects. Keep
        # the stricter same-origin path when the interactive runtime is active,
        # even though network reads themselves no longer require approval.
        if (
            backend is not None
            and backend.capabilities.read
            and not enforce_requested_origin
        ):
            try:
                content = await manager.extract(url, max_length=max_length)
                if content and not content.startswith("[Browser Error]") and not content.startswith("[CDP Error]"):
                    if len(content.strip()) >= _MIN_STATIC_CHARS:
                        return content, "static"
                    # Too short — likely JS-only page, escalate
                    logger.info(
                        "Static extract got %d chars for %s, escalating to CDP",
                        len(content.strip()), url,
                    )
            except Exception as exc:
                logger.info("Static extract failed for %s: %s", url, exc)

        # ── Rung 2: CDP interactive (headless navigate + get_text) ──
        if backend is not None and backend.capabilities.interactive:
            try:
                effective_tab = tab_id or "default"
                await backend.interactive_navigate(  # type: ignore[union-attr]
                    url, tab_id=effective_tab
                )
                # Guard: verify final origin after CDP navigation.
                # If the page redirected to a different origin, block the
                # escalation so redirected content is never attributed to the
                # URL the model originally requested.
                final_url, _, text = await backend.interactive_state(  # type: ignore[union-attr]
                    tab_id=effective_tab, url=url,
                )
                final_origin = normalized_origin(final_url) if final_url else ""
                expected_origin = normalized_origin(url)
                if not final_origin or not expected_origin:
                    return (
                        f"[Browser Error] Unable to verify final origin after navigation: {url}",
                        "error",
                    )
                if final_origin != expected_origin:
                    return (
                        f"[Browser Error] Cross-origin redirect blocked: "
                        f"{url} → {final_url}",
                        "error",
                    )
                if text and len(text.strip()) >= _MIN_STATIC_CHARS:
                    if max_length > 0 and len(text) > max_length:
                        text = text[:max_length] + "\n…[truncated]"
                    return text, "cdp"
                logger.info(
                    "CDP get_text got %d chars for %s, escalating to screenshot",
                    len(text.strip()) if text else 0, url,
                )
            except Exception as exc:
                logger.info("CDP interactive failed for %s: %s", url, exc)
                if enforce_requested_origin:
                    return (
                        f"[Browser Error] Unable to verify final origin after navigation: "
                        f"{type(exc).__name__}: {exc}",
                        "error",
                    )

        if enforce_requested_origin:
            return (
                "[Browser Error] Same-origin extraction requires an interactive backend "
                "that reports the final URL; unsafe static fallback was not executed.",
                "error",
            )

        # ── Rung 3: Screenshot ──
        if backend is not None and backend.capabilities.interactive:
            try:
                effective_tab = tab_id or "default"
                path = await backend.interactive_screenshot(tab_id=effective_tab, url=url)  # type: ignore[union-attr]
                if path and not path.startswith("["):
                    return json.dumps({
                        "success": True,
                        "type": "image_attachment",
                        "image_paths": [path],
                        "detail": "high",
                        "rung": "screenshot",
                        "question": (
                            f"Inspect the screenshot of {url}; static and CDP text "
                            "extraction were insufficient."
                        ),
                        "message": f"Extracted via screenshot rung: {path}",
                    }, ensure_ascii=False), "screenshot"
            except Exception as exc:
                logger.info("Screenshot failed for %s: %s", url, exc)

        return f"[Browser Error] All extraction rungs failed for {url}", "error"

    # ── browser_open ────────────────────────────────────────────────

    async def _browser_open(url: str, *, profile_ref: str = "", extract: bool = True) -> str | ToolFailure:
        if not url or not str(url).strip():
            return "[Browser Error] url is required"
        prepare = getattr(manager.backend, 'prepare_open', None)
        if callable(prepare):
            try:
                await _await_backend_result(prepare())
            except BrowserEndpointOwnedError as exc:
                return _connection_failure("Open", exc)
            except Exception as exc:
                return f"[Browser Error] open failed: {exc}"
        session_id = active.get("session_id", "")
        session = manager.get_session(session_id) if session_id else None
        if session is None:
            session = manager.create_session(profile_ref=profile_ref)
            active["session_id"] = session.session_id
        tab = manager.open_tab(session.session_id, url)
        backend = manager.backend
        if backend is not None and getattr(backend, "structured_snapshots", False) and backend.capabilities.interactive:
            try:
                navigate = getattr(backend, "interactive_navigate", None)
                if not callable(navigate):
                    raise RuntimeError("Live browser navigation is unavailable")
                await _await_backend_result(navigate(url, tab_id=tab.tab_id))
                tab = await _sync_interactive_state(tab.tab_id,
                    expected_origin=normalized_origin(url) if registry.approval_handler is not None else "")
                manager.escalate_to(tab.tab_id, BrowserMode.HEADLESS)
            except Exception as exc:
                with suppress(Exception):
                    await backend.close_connection(tab.tab_id)
                manager.close_tab(tab.tab_id)
                return f"[Browser Error] open failed: {exc}"
            return (f"Opened tab {tab.tab_id} at {tab.url} (live browser)" +
                    (f"\n{tab.last_snapshot}" if extract else ""))
        lines = [f"Opened tab {tab.tab_id} at {url} (mode: {tab.mode.value})"]
        if extract:
            content, rung = await _fallback_extract(url, tab_id=tab.tab_id)
            if rung != "error":
                manager.record_snapshot(tab.tab_id, content)
                # Escalate tab mode to match the rung used
                if rung == "cdp" and tab.mode == BrowserMode.READ_ONLY:
                    with suppress(ValueError, KeyError):
                        manager.escalate_to(tab.tab_id, BrowserMode.HEADLESS)
                elif rung == "screenshot" and tab.mode in (BrowserMode.READ_ONLY, BrowserMode.HEADLESS):
                    with suppress(ValueError, KeyError):
                        manager.escalate_to(tab.tab_id, BrowserMode.SCREENSHOT)
                if rung == "screenshot":
                    payload = json.loads(content)
                    payload["message"] = (
                        f"Opened tab {tab.tab_id} at {url}; "
                        f"{payload.get('message', 'captured screenshot')}"
                    )
                    return json.dumps(payload, ensure_ascii=False)
                preview = content[:1500]
                lines.append(f"Extracted {len(content)} chars via {rung} rung:")
                lines.append(preview)
                if len(content) > 1500:
                    lines.append("…[truncated — use browser_snapshot for the stored copy]")
            else:
                lines.append(content)
        return "\n".join(lines)

    # ── browser_snapshot ────────────────────────────────────────────

    async def _browser_snapshot(tab_id: str = "", *, refresh: bool = False,
        scope: str = "all", role_filter: str = "", frame_ref: str = "", offset: int = 0, limit: int = 150,
        include_text: bool = True,
    ) -> str | ToolFailure:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        if type(include_text) is not bool:
            return _failure("include_text must be a boolean")
        needs_text = False
        with suppress(ValueError):
            cached = json.loads(tab.last_snapshot)
            needs_text = include_text and isinstance(cached, dict) and cached.get("textIncluded") is False
        if scope != "all" or role_filter or frame_ref or offset or limit != 150 or not include_text or needs_text:
            hook = getattr(manager.backend, "interactive_snapshot", None)
            if not callable(hook):
                return _failure("This backend does not support scoped snapshots")
            try:
                result = await _await_backend_result(hook(tab_id=tab.tab_id, url=tab.url,
                    scope=scope, role_filter=role_filter, frame_ref=frame_ref, offset=offset, limit=limit,
                    include_text=include_text))
                payload = json.loads(str(result))
                if payload.get("status") in _failed_statuses:
                    return _failure(str(result), "browser_" + payload["status"])
                if normalized_origin(payload["url"]) != normalized_origin(tab.url):
                    return _failure("Page origin changed; attach the new page explicitly")
                if scope == "form":
                    result = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                tab = manager.update_page_state(tab.tab_id, url=payload["url"],
                    title=str(payload.get("title", "")), snapshot=str(result))
            except Exception as exc:
                return _failure(f"Live scoped snapshot unavailable: {exc}")
            refresh = False
        if refresh and manager.backend is not None and manager.backend.capabilities.read:
            if manager.backend.capabilities.interactive:
                try:
                    tab = await _sync_interactive_state(tab.tab_id,
                        expected_origin=normalized_origin(tab.url) if registry.approval_handler is not None else "")
                except Exception as exc:
                    return _failure(f"Live snapshot unavailable: {exc}")
            else:
                content, rung = await _fallback_extract(tab.url, tab_id=tab.tab_id)
                if rung != "error":
                    tab = manager.record_snapshot(tab.tab_id, content)
                else:
                    return content
        if not tab.last_snapshot:
            return (
                f"Tab {tab.tab_id} ({tab.url}) has no stored snapshot. "
                "Call browser_snapshot with refresh=true or browser_open with extract."
            )
        header = f"Snapshot of {tab.url} (tab {tab.tab_id}, mode {tab.mode.value}):"
        return f"{header}\n{tab.last_snapshot}"

    # ── browser_extract (explicit read-only fetch) ──────────────────

    async def _browser_extract(url: str, *, max_length: int = 12000) -> str:
        if not url or not str(url).strip():
            return "[Browser Error] url is required"
        content, rung = await _fallback_extract(url, max_length=max_length)
        if rung == "error":
            return content
        return content

    # ── interactive: browser_click / browser_type ───────────────────

    def _interactive_guard(tab) -> str:
        """Return an error string if the tab cannot accept interactive actions.

        If the backend supports interactive mode and the tab is still in
        READ_ONLY, auto-escalate to HEADLESS instead of blocking.
        """
        if manager.backend is None or not manager.backend.capabilities.interactive:
            return (
                "[Browser Error] Interactive backend not available. "
                "Only read-only extraction is configured (legacy browser extractor). "
                "A CDP/headless interactive backend is required for click/type."
            )
        if tab.mode == BrowserMode.READ_ONLY:
            # Auto-escalate: the backend can do interactive work, so bump
            # the tab up the ladder instead of making the model do it.
            try:
                manager.escalate_to(tab.tab_id, BrowserMode.HEADLESS)
            except (ValueError, KeyError):
                return (
                    f"[Browser Error] Tab {tab.tab_id} is in read_only mode "
                    "and auto-escalation failed."
                )
        if tab.mode == BrowserMode.HEADED_TAKEOVER:
            return (
                f"[Browser Error] Tab {tab.tab_id} is in human takeover. "
                "Call browser_resume after the user finishes before automating."
            )
        return ""

    async def _browser_click(selector: str, tab_id: str = "") -> str | ToolFailure:
        tab, err = _resolve_tab(tab_id)
        if err:
            return _interaction_failure("click", err)
        guard = _interactive_guard(tab)
        if guard:
            return _interaction_failure("click", guard)
        # Dispatch to the backend's interactive click if available
        hook = getattr(manager.backend, "interactive_click", None)
        if callable(hook):
            dispatched = False
            try:
                await _assert_write_origin(tab)
                dispatched = True
                result = await hook(selector, tab_id=tab.tab_id, url=tab.url)
                return await _finish_action(tab.tab_id, result)
            except Exception as e:
                return _interaction_failure("click", f"Click failed: {e}", dispatched=dispatched)
        return _interaction_failure("click", "[Browser Error] Interactive backend not available.")

    async def _browser_type(selector: str, text: str, tab_id: str = "") -> str | ToolFailure:
        tab, err = _resolve_tab(tab_id)
        if err:
            return _interaction_failure("type", err)
        guard = _interactive_guard(tab)
        if guard:
            return _interaction_failure("type", guard)
        hook = getattr(manager.backend, "interactive_type", None)
        if callable(hook):
            dispatched = False
            try:
                await _assert_write_origin(tab)
                dispatched = True
                result = await hook(
                    selector, text, tab_id=tab.tab_id, url=tab.url
                )
                return await _finish_action(tab.tab_id, result)
            except Exception as e:
                return _interaction_failure("type", f"Type failed: {e}", dispatched=dispatched)
        return _interaction_failure("type", "[Browser Error] Interactive backend not available.")

    async def _target_tool(operation: str, selector: str, tab_id: str, **args) -> str | ToolFailure:
        tab, err = _resolve_tab(tab_id)
        if err:
            return _interaction_failure(operation, err)
        guard = _interactive_guard(tab)
        if guard:
            return _interaction_failure(operation, guard)
        hook = getattr(manager.backend, "interactive_" + operation, None)
        if not callable(hook):
            return _interaction_failure(operation, f"Backend does not support browser_{operation}")
        dispatched = False
        try:
            await _assert_write_origin(tab)
            dispatched = True
            result = await _await_backend_result(hook(selector, tab_id=tab.tab_id, url=tab.url, **args))
            return await _finish_action(tab.tab_id, str(result), expected_text=args.get("text") if operation == "fill" else None)
        except Exception as exc:
            return _interaction_failure(operation, f"{operation} failed: {exc}", dispatched=dispatched)

    async def _browser_fill(selector: str, text: str, tab_id: str = "") -> str | ToolFailure:
        return await _target_tool("fill", selector, tab_id, text=text)

    async def _browser_read(selector: str, tab_id: str = "") -> str | ToolFailure:
        return await _target_tool("read", selector, tab_id)

    async def _browser_check(selector: str = "", checked: bool = True,
        checks: list[dict[str, object]] | None = None, tab_id: str = "") -> str | ToolFailure:
        if not isinstance(selector, str) or type(checked) is not bool:
            return _failure("selector must be a string and checked must be a boolean", "invalid_arguments")
        if checks is not None:
            if selector or not isinstance(checks, list) or not 1 <= len(checks) <= 20:
                return _failure("Use selector or 1..20 checks, not both", "invalid_arguments")
            if any(not isinstance(item, dict) or set(item) != {"selector", "checked"}
                or not isinstance(item["selector"], str) or not item["selector"]
                or type(item["checked"]) is not bool for item in checks):
                return _failure("Each check requires selector and boolean checked", "invalid_arguments")
        elif not selector:
            return _failure("selector or checks is required", "invalid_arguments")
        return await _target_tool("check", selector, tab_id, checked=checked, checks=checks)

    async def _browser_select(selector: str, value: str, tab_id: str = "") -> str | ToolFailure:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        guard = _interactive_guard(tab)
        if guard:
            return guard
        try:
            await _assert_write_origin(tab)
            result = await manager.backend.interactive_select(  # type: ignore[union-attr]
                selector, value, tab_id=tab.tab_id, url=tab.url
            )
            return await _finish_action(tab.tab_id, result)
        except Exception as exc:
            return _failure(f"Select failed: {exc}")

    async def _browser_wait(
        selector: str = "",
        text: str = "",
        url_contains: str = "",
        timeout_ms: int = 10000,
        tab_id: str = "",
    ) -> str | ToolFailure:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        guard = _interactive_guard(tab)
        if guard:
            return guard
        try:
            result = await manager.backend.interactive_wait(  # type: ignore[union-attr]
                tab_id=tab.tab_id,
                url=tab.url,
                selector=selector,
                text=text,
                url_contains=url_contains,
                timeout_ms=timeout_ms,
            )
            return await _finish_action(tab.tab_id, result)
        except Exception as exc:
            return _failure(f"Wait failed: {exc}")

    async def _browser_screenshot(tab_id: str = "", output_path: str = "") -> str:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        guard = _interactive_guard(tab)
        if guard:
            return guard
        try:
            path = await manager.backend.interactive_screenshot(  # type: ignore[union-attr]
                tab_id=tab.tab_id, url=tab.url, output_path=output_path
            )
            if path.startswith("[CDP Error]"):
                return f"[Browser Error] screenshot failed: {path}"
            current = manager.get_tab(tab.tab_id)
            if current and current.mode == BrowserMode.HEADLESS:
                manager.escalate_to(tab.tab_id, BrowserMode.SCREENSHOT)
            return json.dumps({
                "success": True,
                "type": "image_attachment",
                "image_paths": [path],
                "detail": "high",
                "question": f"Inspect the screenshot of {tab.url}.",
                "message": f"Browser screenshot saved: {path}",
            }, ensure_ascii=False)
        except Exception as exc:
            return f"[Browser Error] screenshot failed: {exc}"

    def _verify_browser_screenshot(_args: dict, result: dict) -> tuple[bool, str]:
        try:
            payload = json.loads(str(result.get("output") or ""))
        except json.JSONDecodeError:
            return False, "tool did not return an image attachment payload"
        paths = payload.get("image_paths") if isinstance(payload, dict) else None
        if payload.get("type") != "image_attachment" or not isinstance(paths, list) or not paths:
            return False, "image attachment payload has no image path"
        missing = [str(path) for path in paths if not Path(str(path)).is_file()]
        if missing:
            return False, f"screenshot file was not created: {', '.join(missing)}"
        return True, f"verified {len(paths)} screenshot file(s)"

    # ── connect to existing Chrome ──────────────────────────────────

    async def _browser_tabs(transport: str = "extension") -> str | ToolFailure:
        method = getattr(manager.backend, "list_tabs", None)
        if not callable(method):
            return "[Browser Error] Extension tab discovery is unavailable."
        try:
            return sanitize_snapshot(json.dumps(
                await _await_backend_result(method(transport=transport)), ensure_ascii=False,
            ))
        except Exception as exc:
            return _connection_failure("Tab discovery", exc)

    async def _browser_connect(port: int = 0, host: str = "", transport: str = "cdp", target_tab_id: str = "") -> str | ToolFailure:
        if transport not in {"cdp", "extension"}:
            return "[Browser Error] transport must be cdp or extension"
        backend = manager.backend
        connect_existing = cast(
            Callable[..., Awaitable[str]] | None,
            getattr(backend, "connect_existing", None),
        )
        if backend is None or not callable(connect_existing):
            return "[Browser Error] An interactive browser backend is required to connect."
        session_id = active.get("session_id", "")
        session = manager.get_session(session_id) if session_id else None
        if session is None:
            session = manager.create_session(profile_ref="existing-chrome")
            active["session_id"] = session.session_id
        tracking_tab = manager.open_tab(
            session.session_id,
            "about:blank",
            title="Connecting to existing Chrome",
        )
        try:
            connect_args = dict(port=port, host=host, tab_id=tracking_tab.tab_id)
            if transport != "cdp" or getattr(backend, "supports_transport_selection", False):
                connect_args.update(transport=transport, target_tab_id=target_tab_id)
            result = await connect_existing(**connect_args)
            if result.startswith(("[Browser]", "[Browser Error]", "[CDP Error]")):
                manager.close_tab(tracking_tab.tab_id)
                return result
            synced = await _sync_interactive_state(tracking_tab.tab_id)
            return (
                f"{result}\nTracked as browser tab {synced.tab_id} "
                f"({synced.title or synced.url})."
            )
        except Exception as exc:
            with suppress(Exception):
                await backend.close_connection(tracking_tab.tab_id)
            with suppress(Exception):
                manager.close_tab(tracking_tab.tab_id)
            return _connection_failure("Connect", exc)

    async def _browser_close(tab_id: str = "") -> str:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        try:
            if manager.backend is not None:
                await manager.backend.close_connection(tab.tab_id)
            manager.close_tab(tab.tab_id)
            return f"Closed browser tab {tab.tab_id}."
        except Exception as exc:
            return f"[Browser Error] close failed: {exc}"

    # ── takeover: browser_handoff / browser_resume ──────────────────

    async def _browser_handoff(reason: str = "unknown", tab_id: str = "") -> str:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        backend = manager.backend
        if backend is None or not backend.capabilities.takeover:
            return "[Browser Error] Human takeover backend is unavailable."
        try:
            detail = await backend.interactive_handoff(
                tab_id=tab.tab_id, url=tab.url, profile_dir=_profile_dir(tab.tab_id)
            )
        except Exception as exc:
            return f"[Browser Error] headed takeover failed: {exc}"
        tab = manager.request_takeover(tab.tab_id, reason=reason)
        return (
            f"Tab {tab.tab_id} ({tab.url}) handed off to human takeover "
            f"(reason: {tab.takeover_reason}). {detail}. The user now drives the real "
            "browser. Call browser_resume once they finish to continue automation."
        )

    async def _browser_resume(tab_id: str = "") -> str:
        tab, err = _resolve_tab(tab_id)
        if err:
            return err
        if tab.mode != BrowserMode.HEADED_TAKEOVER:
            return f"[Browser Error] Tab {tab.tab_id} is not in takeover (mode={tab.mode.value})."
        backend = manager.backend
        if backend is None or not backend.capabilities.interactive:
            return "[Browser Error] Interactive backend is unavailable."
        try:
            detail = await backend.interactive_resume(
                tab_id=tab.tab_id, url=tab.url, profile_dir=_profile_dir(tab.tab_id)
            )
            await _sync_interactive_state(tab.tab_id)
        except Exception as exc:
            return f"[Browser Error] resume failed: {exc}"
        restored = manager.resume_from_takeover(tab.tab_id)
        return (
            f"Tab {restored.tab_id} resumed from human takeover, restored to "
            f"{restored.mode.value} mode. {detail}. Automation may continue."
        )

    # ── browser_status (diagnostic) ─────────────────────────────────

    async def _browser_status() -> str:
        if lifecycle.release_state in {"releasing", "release_failed"}:
            return f"Backend: unavailable ({lifecycle.release_state}); use /browser stop to finish release."
        ok, detail = await manager.backend_status()
        availability = "available" if ok else "idle" if "auto-connect: idle" in detail.lower() else "unavailable"
        backend_line = f"Backend: {availability} ({detail})"
        sessions = manager.list_sessions(limit=5)
        if not sessions:
            return f"{backend_line}\nNo active browser sessions."
        lines = [backend_line, "Recent sessions (history only; not evidence of live attachment):"]
        for session in sessions:
            lines.append(format_session(session))
        return "\n".join(lines)

    def _browser_trace_context(_args: dict, _result: dict) -> dict[str, str]:
        return {"browser_session_id": active.get("session_id", "")}

    # ── Register ────────────────────────────────────────────────────

    register(ToolDef(
        name="browser_open",
        description=(
            "在持久浏览器会话中打开一个标签页（起始为 read_only 档）。"
            "也适用于搜索或正文提取不足时主动访问原站、展开内容或交互搜索补查。"
            "查资料且 URL 已知、不依赖原标签状态时，优先复用本任务可用标签，没有则直接调用本工具。"
            "已配置自动连接时会主动连接扩展并打开可见标签，无需先调用 browser_status/browser_tabs/browser_connect；等待最多45秒。"
            "新标签当前可能被选中，不保证保留用户的活动标签；按已有通道能力优先减少前台打扰。"
            "若配置了只读后端，会立即抓取页面正文并存储脱敏快照。"
            "需要交互时再用 browser_click/browser_fill，遇到验证码/登录/支付用 browser_handoff。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要打开的完整 URL"},
                "profile_ref": {
                    "type": "string",
                    "description": "浏览器配置引用名（如 default/work），仅标签，不含凭据",
                    "default": "",
                },
                "extract": {
                    "type": "boolean",
                    "description": "是否立即抓取正文（默认 true）",
                    "default": True,
                },
            },
            "required": ["url"],
        },
        fn=_browser_open, risk="network", approval="never", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_snapshot",
        description=(
            "读取当前标签页已存储快照；refresh=true 观察当前页面，不重新导航。"
            "选择题/混合表单优先 scope=form,include_text=false：groups 保留题干，elements 保留选项、checked 和 frame/ref，自动刷新。文本编辑器用 scope=editable。"
            "可用 role_filter/frame_ref 筛选、offset/limit 分页，避免工具栏挤掉输入框。"
            "offset/limit 只分页匹配元素，不分页正文；长页内容优先定位容器后用 browser_read，避免重复全页刷新。"
            "已读页面正文后用 include_text=false 自动刷新为精简观察，后续 after 沿用；保留元素值、checked 状态和新 refs。"
            "使用最新 elements 中的 ref:<id>；点击后结果未明时先观察或 browser_wait，不重复提交。"
            "快照中的 cookie/token/密码等敏感信息已在落库前剥离。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "标签页 ID；缺省用当前标签页", "default": ""},
                "refresh": {"type": "boolean", "description": "是否重新抓取", "default": False},
                "include_text": {"type": "boolean", "description": "是否包含全页正文；false 自动刷新且让后续 after 省略正文，true 可恢复正文", "default": True},
                "scope": {"type": "string", "enum": ["all", "editable", "form"], "default": "all"},
                "role_filter": {"type": "string", "description": "仅匹配此角色，如 textbox", "default": ""},
                "frame_ref": {"type": "string", "description": "最新快照的 frameRef；过期需重新读取", "default": ""},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 150, "default": 150},
            },
        },
        fn=_browser_snapshot, risk="read", approval="never", idempotent=True,
        cache_results=False, repeat_guard=False, max_calls_per_turn=20,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_extract",
        description="对指定 URL 做一次只读正文提取（静态/无头），返回脱敏文本。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要提取的完整 URL"},
                "max_length": {"type": "integer", "description": "最大字符数，默认 12000", "default": 12000},
            },
            "required": ["url"],
        },
        fn=_browser_extract, risk="network", approval="never", idempotent=True,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_click",
        description=(
            "点击最新快照的 ref:<id> 或唯一 CSS 元素；返回观测结果和新快照，不能把派发成功当作任务成功。"
            "no_observed_change 时先只读核对；结果仍不明时，换 ref/CSS 或内外层元素点击仍是同一目标的重试。"
            "read_only 档或 human takeover 中会报错。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "最新快照元素引用 ref:<id>，或唯一 CSS 选择器"},
                "tab_id": {"type": "string", "description": "标签页 ID；缺省用当前标签页", "default": ""},
            },
            "required": ["selector"],
        },
        fn=_browser_click, risk="write", approval="on_risk", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser",
        permission_check=_browser_write_permission_check("Click page element"),
        permission_grant=browser_approvals.grant,
    ))

    register(ToolDef(
        name="browser_type",
        description=(
            "兼容填写接口：替换唯一目标输入框的内容并读回核对；新表单操作优先 browser_fill。"
            "不要在文本中传入密码/令牌等凭据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "最新快照元素引用 ref:<id>，或唯一 CSS 选择器"},
                "text": {"type": "string", "description": "要输入的文本（勿传凭据）"},
                "tab_id": {"type": "string", "description": "标签页 ID；缺省用当前标签页", "default": ""},
            },
            "required": ["selector", "text"],
        },
        fn=_browser_type, risk="write", approval="on_risk", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser", expose_by_default=False,
        permission_check=_browser_write_permission_check("Type into page", "text"),
        permission_grant=browser_approvals.grant,
    ))

    register(ToolDef(
        name="browser_fill",
        description=("替换指定输入框/同源 iframe 富文本框的全部文本，并读回该目标验证。"
            "先用 scope=editable 快照，根据题目/框架上下文选择最新 ref:<id>；同名框不能用模糊选择器。"
            "后台执行，不激活浏览器或使用系统剪贴板。verified 仅证明字段内容，页面保存状态须另查。"
            "失败或 unknown_outcome 时先读取，禁止盲目重试或系统粘贴。"),
        parameters={"type": "object", "properties": {
            "selector": {"type": "string", "description": "最新快照的 ref:<id>（绑定具体 iframe）或全页唯一 CSS"},
            "text": {"type": "string", "maxLength": 12000, "description": "完整替换文本，勿传凭据"},
            "tab_id": {"type": "string", "default": ""}}, "required": ["selector", "text"]},
        fn=_browser_fill, risk="write", approval="on_risk", idempotent=False,
        trace_context=_browser_trace_context, group="browser",
        permission_check=_browser_write_permission_check("Fill page field", "text"),
        permission_grant=browser_approvals.grant,
    ))
    register(ToolDef(
        name="browser_upload",
        description=("通过 Browser Control 扩展向指定网页选择/替换本地文件；paths=[] 清空。"
            "先 browser_snapshot(scope=form,role_filter=file) 取新 ref；支持隐藏 file input 和同源 iframe。"
            "不打开系统文件面板、不抢桌面焦点。网站可能在选择后自动上传；Submit 是另一个操作。"
            "最多10个文件，单文件32 MiB、合计64 MiB；verified仅证明input.files匹配。"
            "unknown_outcome先观察，禁止自动重试；其他后端明确不支持。"),
        parameters={"type": "object", "properties": {
            "tab_id": {"type": "string", "minLength": 1},
            "selector": {"type": "string", "pattern": "^ref:.+"},
            "paths": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1}},
            "frame_ref": {"type": "string", "default": ""}}, "required": ["tab_id", "selector", "paths"]},
        fn=_browser_upload, risk="write", approval="on_risk", idempotent=False,
        permission_check=_upload_permission_check, permission_grant=upload_approvals.grant,
        permission_authoritative=True, permission_yolo_auto_grant=True,
        trace_context=_browser_trace_context, group="browser",
    ))
    register(ToolDef(
        name="browser_read",
        description=(
            "读取指定 ref:<id> 或唯一 CSS 元素的实际值、frameRef 和选择控件的 checked 状态；value 不代表勾选状态。"
            "适合读取长页面的已定位内容容器；只读题头不能确认答案或展开状态。不点击、不改变焦点、不使现有引用过期。"
        ),
        parameters={"type": "object", "properties": {
            "selector": {"type": "string", "description": "最新快照的 ref:<id> 或全页唯一 CSS"},
            "tab_id": {"type": "string", "default": ""}}, "required": ["selector"]},
        fn=_browser_read, risk="read", approval="never", idempotent=True,
        cache_results=False, repeat_guard=False, max_calls_per_turn=30,
        trace_context=_browser_trace_context, group="browser",
    ))

    register(ToolDef(
        name="browser_check",
        description=("设置 checkbox/radio 的 checked 状态并逐项回读。表单优先使用此工具："
            "可传 selector 单项或 checks 批量 1..20 项，使用同一最新快照的 ref:<id> 或唯一 CSS。"
            "已满足的目标不点击；每项目标最多点击一次，批次失败即停并返回 completed/failedIndex。"
            "verified 只证明控件状态；不提交表单。失败后检查回执和新状态，只继续未完成项，禁止重放整批。"
            "旧扩展若不支持 check，重载扩展后重新观察；不要重复相同不支持调用。"),
        parameters={"type": "object", "properties": {
            "selector": {"type": "string", "default": "", "description": "单个目标的 ref:<id> 或唯一 CSS"},
            "checked": {"type": "boolean", "default": True},
            "checks": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                "type": "object", "properties": {
                    "selector": {"type": "string", "minLength": 1}, "checked": {"type": "boolean"}},
                "required": ["selector", "checked"], "additionalProperties": False}},
            "tab_id": {"type": "string", "default": ""}}},
        fn=_browser_check, risk="write", approval="on_risk", idempotent=False,
        trace_context=_browser_trace_context, group="browser",
        permission_check=_browser_write_permission_check("Set page choices"),
        permission_grant=browser_approvals.grant,
    ))

    register(ToolDef(
        name="browser_select",
        description="在当前标签页的原生 select 元素中按 value 选择选项。",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector"},
                "value": {"type": "string", "description": "option value"},
                "tab_id": {"type": "string", "default": ""},
            },
            "required": ["selector", "value"],
        },
        fn=_browser_select, risk="write", approval="on_risk", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser",
        permission_check=_browser_write_permission_check("Select page option", "value"),
        permission_grant=browser_approvals.grant,
    ))

    register(ToolDef(
        name="browser_wait",
        description="等待已观察到或明确已知的 selector、页面文本或 URL 条件；多个条件需同时满足，不猜通用成功文案。timeout 只说明条件未满足，先检查 after 的当前结果；extension 最多等待 10000ms，回执包含实际时间上限。",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": ""},
                "text": {"type": "string", "default": ""},
                "url_contains": {"type": "string", "default": ""},
                "timeout_ms": {"type": "integer", "default": 10000},
                "tab_id": {"type": "string", "default": ""},
            },
        },
        fn=_browser_wait, risk="read", approval="never", idempotent=True,
        cache_results=False, repeat_guard=False, max_calls_per_turn=20,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_screenshot",
        description="CDP 后端截取已绑定标签页并返回 PNG 路径；extension 后端不支持截图。",
        parameters={
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "default": ""},
                "output_path": {"type": "string", "default": ""},
            },
        },
        fn=_browser_screenshot, risk="write", approval="on_risk", idempotent=False,
        postcondition=_verify_browser_screenshot,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_handoff",
        description=(
            "把当前标签页升级为 human takeover：遇到验证码、2FA、支付、登录或难以判断的弹窗时调用，"
            "由用户在真实浏览器中完成，之后用 browser_resume 继续。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["captcha", "2fa", "payment", "dialog", "login", "unknown"],
                    "description": "接管原因",
                    "default": "unknown",
                },
                "tab_id": {"type": "string", "description": "标签页 ID；缺省用当前标签页", "default": ""},
            },
        },
        fn=_browser_handoff, risk="read", approval="never", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_resume",
        description="用户在 human takeover 完成后调用，把标签页恢复到接管前的执行档位继续自动化。",
        parameters={
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "标签页 ID；缺省用当前标签页", "default": ""},
            },
        },
        fn=_browser_resume, risk="read", approval="never", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    registry.register(ToolDef(
        name="browser_status",
        description=(
            "查看浏览器后端可用性与最近会话/标签页状态，用于实际连接失败、明确配置问题或用户要求诊断。"
            "普通资料调研已有目标 URL 时可直接 browser_open，不必例行预检；历史标签不是当前已绑定标签。"
        ),
        parameters={"type": "object", "properties": {}},
        fn=_browser_status, risk="read", approval="never", idempotent=True,
        cache_results=False, repeat_guard=False, max_calls_per_turn=20,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_close",
        description="关闭当前或指定的真实浏览器标签页，并清理其 CDP 进程。",
        parameters={
            "type": "object",
            "properties": {"tab_id": {"type": "string", "default": ""}},
        },
        fn=_browser_close, risk="write", approval="on_risk", idempotent=False,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_connect",
        description=(
            "连接现有浏览器：transport=extension 使用独立控制扩展授权的 Edge/Chrome 标签，"
            "先 browser_tabs 查看 target_tab_id；cdp 保留调试端口连接。"
            "用于用户指定或任务依赖原页面状态的已有标签；普通资料调研可直接 browser_open 新建任务标签。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "transport": {"type": "string", "enum": ["cdp", "extension"], "default": "cdp"},
                "target_tab_id": {"type": "string", "description": "扩展已授权的真实标签 ID；为空时必须仅有一个可选标签", "default": ""},
                "port": {"type": "integer", "description": "DevTools 调试端口，0=自动扫描", "default": 0},
                "host": {"type": "string", "description": "Chrome 所在主机，默认 127.0.0.1", "default": ""},
            },
        },
        fn=_browser_connect, risk="network", approval="never", idempotent=True,
        cache_results=False,
        trace_context=_browser_trace_context,
        group="browser",
    ))

    register(ToolDef(
        name="browser_tabs",
        description=(
            "发现用户指定或任务所需的已有标签，只列出独立控制扩展明确授权的标签，不枚举未授权的日常标签。"
            "普通资料调研无需将此作为 browser_open 的前置；空列表只表示无已授权标签，仍可新建任务标签。"
            "连接不可用时返回安装/连接提示。"
        ),
        parameters={"type": "object", "properties": {
            "transport": {"type": "string", "enum": ["extension"], "default": "extension"}}},
        fn=_browser_tabs, risk="read", approval="never", idempotent=True,
        cache_results=False, repeat_guard=False, max_calls_per_turn=20,
        trace_context=_browser_trace_context, group="browser",
    ))
    registry.register(ToolDef(
        name="browser_stop",
        description="Release this session's browser control after in-flight work finishes. Keep the user's browser open; old logical tabs/refs expire. Connect/open and observe again before further actions.",
        parameters={"type": "object", "properties": {}},
        fn=lifecycle.stop, risk="read", idempotent=True,
        cache_results=False, repeat_guard=False, group="browser",
    ))
    return manager
