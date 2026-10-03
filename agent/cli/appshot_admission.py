"""Appshot admission transaction, bounded replay ledger and fresh request budgeting."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import logging
import os
import re
import sys
import tempfile
from urllib.parse import urlparse

from agent.cli.appshots import AppshotValidationError, AppshotVerifier
from agent.cli.images import build_user_message_content
from agent.core.msg import ContentBlock, Msg
from agent.runtime.deepseek import DEEPSEEK_FLASH, DEEPSEEK_IMAGE_TOKENS, canonical_deepseek_model
from agent.runtime.appshot_media import AppshotMediaError, AppshotMediaStore
from agent.runtime.appshot_process import AppshotProcessIdentityError


def request_token_bound(messages, tools, config):
    """Fresh request budget, with published image bounds where available.

    All models share the runtime text/schema estimator with 25% slack. UTF-8
    bytes are transport size, never text tokens. Image payloads are excluded
    from text and charged separately, with additional framing/schema slack.
    Qwen3-VL/3.5--3.8 official Model Studio docs cap each image at 16384
    32x32 patches + 2 boundary tokens (including smart_resize):
    https://help.aliyun.com/zh/model-studio/vision
    GPT-4o/4.1 use 85+170 per 512px tile, conservatively without downscaling
    and with at least nine tiles to cover small-image upscaling.
    DeepSeek's official vision model caps every image at 1024 tokens:
    https://api-docs.deepseek.com/guides/vision/#token-usage
    Other vision-capable profiles use a dimension-aware image allowance.
    Text accounting is an estimate, not a proved provider bound; model/host
    names select image rules, not text accounting or admission rights.
    Remote URL images and malformed/unbounded local images remain rejected.
    """
    from PIL import Image
    from agent.runtime.token_estimator import estimate_messages_tokens, estimate_value_tokens

    model = str(config.model).lower()
    host = urlparse(str(config.base_url)).hostname or ""
    qwen = host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
        "token-plan.cn-beijing.maas.aliyuncs.com",
    } and re.fullmatch(r"qwen(?:3\.8|3\.7|3\.6|3\.5|3-vl)[a-z0-9.-]*", model)
    openai = host == "api.openai.com" and re.fullmatch(r"gpt-(?:4o|4\.1)(?:-\d{4}-\d{2}-\d{2})?", model)
    deepseek = (
        host == "api.deepseek.com"
        and canonical_deepseek_model(model, str(config.base_url)) == DEEPSEEK_FLASH
    )
    image_count = sum(
        1 for message in messages if isinstance(message.get("content"), list)
        for part in message["content"] if isinstance(part, dict) and part.get("type") == "image_url"
    )
    if deepseek and image_count:
        # The dimension cap tightens for the whole request at 15 images.
        # Include tools/text in the inline body bound, with framing slack.
        body = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
        if image_count > 600 or len(body.encode("utf-8")) + 4096 > 48 * 1024 * 1024:
            raise AppshotValidationError("context_budget_unavailable")
    prepared = copy.deepcopy(messages)
    visual = 0
    for message in prepared:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") != "image_url":
                continue
            # Match the adapter capability projection: a model name alone does
            # not authorize images when the selected profile is text-only.
            if "vision" not in getattr(config, "capabilities", ()):
                raise AppshotValidationError("appshot_vision_unavailable")
            url = part.get("image_url", {}).get("url", "")
            if (
                not isinstance(url, str)
                or not re.match(r"^data:image/(png|jpeg|webp);base64,", url)
                or len(url) > 15 * 1024 * 1024
            ):
                raise AppshotValidationError("context_budget_unavailable")
            try:
                raw = base64.b64decode(url.split(",", 1)[1], validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    width, height = image.size
                    if (
                        width < 1
                        or height < 1
                        or width > 16384
                        or height > 16384
                        or width * height > 32000000
                        or getattr(image, "n_frames", 1) != 1
                    ):
                        raise ValueError()
                    if qwen and (min(width, height) <= 10 or max(width, height) / min(width, height) > 200):
                        raise ValueError()
                    if deepseek and max(width, height) > (4096 if image_count >= 15 else 8192):
                        raise ValueError()
                    image.load()
            except Exception as exc:
                raise AppshotValidationError("context_budget_unavailable") from exc
            # Full published Qwen maximum is intentionally more conservative
            # than the default resize. Dimensions above are verified for caps.
            if deepseek:
                visual += DEEPSEEK_IMAGE_TOKENS
            elif qwen:
                visual += 16386
            elif openai:
                visual += 85 + 170 * max(9, math.ceil(width / 512) * math.ceil(height / 512))
            else:
                # Cover at least the runtime's high-detail allowance and grow
                # with a 32px patch grid for large original screenshots.
                visual += max(4096, math.ceil(width / 32) * math.ceil(height / 32) + 2)
            part.clear()
            part.update(type="text", text="[image]")
    text = math.ceil(1.25 * (estimate_messages_tokens(prepared) + estimate_value_tokens(tools)))
    return text + visual + 4096 + 128 * (len(messages) + len(tools))


def appshot_prompt_limit(context, config, generation_overrides=None):
    """Provider input capacity; the normal compression threshold stays separate.

    Reserve the configured output (at least 8K) and any larger turn override.
    Runtimes without resolved window metadata retain their previous conservative
    prompt budget. Never infer capacity by doubling a compression threshold.
    """
    configured_output = max(0, int(getattr(config, "max_tokens", 0)))
    overrides = generation_overrides or {}
    output = max(
        configured_output,
        int(overrides.get("max_tokens", 0)),
        int(overrides.get("max_completion_tokens", 0)),
    )
    window = getattr(config, "context_limit", None)
    if window is not None:
        if type(window) is not int or window < 2:
            raise AppshotValidationError("context_budget_unavailable")
        return max(0, window - max(8192, output))
    return max(0, context.max_prompt_tokens - (output - configured_output))


async def prepare_appshot_message(agent, msg):
    # Any ordinary-image tiling during preview uses isolated ephemeral storage
    # and request registries, never the live session's tile state.
    with tempfile.TemporaryDirectory(prefix="astra-appshot-preview-") as cache:
        try:
            return await _prepare_appshot_message(agent, msg, cache)
        except AppshotValidationError as exc:
            if (
                str(exc) != "context_budget_exceeded"
                or not agent.context.compaction_enabled
                or agent.context.compressor is None
            ):
                raise

        # Fresh Appshots reach admission before ReAct's automatic compaction.
        # Try once on detached history, retaining the incoming message verbatim
        # for a fresh preview. Rejection/cancellation must not compact live state.
        candidate = copy.copy(agent.context)
        candidate.messages = copy.deepcopy(agent.context.messages)
        candidate.system_projection = copy.deepcopy(agent.context.system_projection)
        candidate.runtime_projection = copy.deepcopy(agent.context.runtime_projection)
        candidate._message_token_costs = list(agent.context._message_token_costs)
        candidate._session_store = None
        candidate._save_lock = asyncio.Lock()
        candidate.compaction_observer = None
        candidate.compressor = copy.copy(agent.context.compressor)
        candidate.compressor.hooks = None
        live = agent.context
        before = len(live.messages)
        live.report_compaction("started", before)
        try:
            await candidate.compress_if_needed(force=True, preserve_on_failure=True)
            staged_agent = copy.copy(agent)
            staged_agent.context = candidate
            await _prepare_appshot_message(staged_agent, msg, cache)
            candidate.compressor.hooks = live.compressor.hooks
            return _PreparedAppshotCompaction(live, candidate)
        except BaseException as exc:
            live.report_compaction("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed", before)
            raise


class _PreparedAppshotCompaction:
    """Install validated history only across the synchronous launch boundary."""

    _fields = (
        "system_prompt", "messages", "compressor", "last_prompt_tokens",
        "_message_token_costs", "_system_token_cost", "_saved_message_count",
        "runtime_projection",
    )

    def __init__(self, context, candidate):
        self.context = context
        self.candidate = candidate
        self.previous = None
        self.messages_before = len(context.messages)

    def install(self):
        self.previous = {key: getattr(self.context, key) for key in self._fields}
        for key in self._fields:
            setattr(self.context, key, getattr(self.candidate, key))
        # Deterministic compaction can also change history. The next checkpoint
        # must replace the saved prefix, never append to its old message count.
        self.context._saved_message_count = 0

    def rollback(self):
        if self.previous is not None:
            for key, value in self.previous.items():
                setattr(self.context, key, value)
        self.context.report_compaction("failed", self.messages_before)

    def committed(self):
        logging.getLogger(__name__).info(
            "appshot_compaction_committed messages_before=%d messages_after=%d",
            self.messages_before, len(self.context.messages),
        )
        self.context.report_compaction(
            "completed", self.messages_before, details=self.candidate._last_compaction_report,
        )


async def _prepare_appshot_message(agent, msg, cache):
    """Preview the real runtime prompt on an isolated context, without compaction.

    No live message/history/cache is changed on rejection. The actual runtime
    request is checked again after dynamic/transient context and tool selection.
    """
    from agent.runtime.vision_preprocessor import VisionPreprocessor

    preview = copy.copy(agent)
    preview.vision_preprocessor = VisionPreprocessor(cache_root=cache)
    preview.context = copy.copy(agent.context)
    preview.context.messages = copy.deepcopy(agent.context.messages)
    preview.context.system_projection = copy.deepcopy(agent.context.system_projection)
    preview.context.runtime_projection = copy.deepcopy(agent.context.runtime_projection)
    preview.context._message_token_costs = list(agent.context._message_token_costs)
    preview.context._session_store = None
    preview.context._save_lock = asyncio.Lock()
    preview.context.compaction_observer = None
    preview.context.compaction_enabled = False
    # Mutable per-turn state must not alias the live React instance.
    for key, value in vars(agent).items():
        if isinstance(value, (dict, list, set)):
            setattr(preview, key, copy.copy(value))
    preview._turn_context_key = ""
    preview._memory_turn_key = "appshot-preview:" + msg.id
    preview._active_vision_prompt_overlays = []
    preview._fresh_tool_context = {}
    preview._project_instructions = copy.deepcopy(agent._project_instructions)
    msg.metadata["appshot_budget_required"] = True
    _, user_index, _, _ = await preview._prepare_turn(msg)
    transient = [
        {
            "role": "system",
            "content": "[USER STEERING — mid-run correction, authoritative]\n" + text + "\n[END USER STEERING]",
        }
        for text in preview._steering
    ]
    if preview._verification_required:
        transient.append(
            {
                "role": "system",
                "content": "Coding mode recorded a successful write that has not been followed "
                "by a successful check. Consider running the narrowest relevant test, "
                "build, lint, or validation command before the final answer; if no "
                "check is genuinely applicable, state the uncovered risk explicitly "
                "instead of claiming success.",
            }
        )
    prompt, _ = await preview._prepare_prompt_for_llm(
        msg.get_text(), None, current_user_index=user_index, transient_messages=transient
    )
    schemas = preview.tools.to_openai_tools() if preview.tools_enabled else []
    if preview.tools_enabled:
        activation = preview.tools.activation_tool_schema()
        if activation is not None:
            schemas.append(activation)
        schemas = preview._apply_code_mode(schemas)
    if preview._vision_tile_tool_enabled():
        preview.vision_preprocessor.validate_provider_prompt(prompt, preview.llm.config.vision_preprocess)
    limit = appshot_prompt_limit(agent.context, agent.llm.config, preview._generation_overrides())
    if request_token_bound(prompt, schemas, agent.llm.config) >= limit:
        raise AppshotValidationError("context_budget_exceeded")
    msg.metadata["appshot_budget_required"] = True
    return msg


def schedule_reserved_turn(coroutine, lock, *, name):
    """Transfer exactly one lock release even if cancelled before first resume."""
    task = asyncio.create_task(coroutine, name=name)
    task.add_done_callback(lambda completed: lock.release())
    return task


class AppshotAdmission:
    """One backend process ledger. A restart deliberately returns unknown.

    launch is synchronous and takes ownership of the already-held turn lock
    only on True. It must schedule the turn without running provider work.
    The caller emits acceptance before yielding to that scheduled coroutine.
    """

    def __init__(self, *, lock, busy, context, prepare, launch, send, runtime_root=None, parent_identity=None):
        self.lock = lock
        self.busy = busy
        self.context = context
        self.prepare = prepare
        self.launch = launch
        self.send = send
        self.runtime_root = runtime_root
        if parent_identity is None:
            if sys.platform == "win32":
                from agent.runtime.appshot_windows_process import read_windows_recipient_identity
                parent_identity = read_windows_recipient_identity
            else:
                from agent.runtime.appshot_process import read_parent_identity
                parent_identity = read_parent_identity
        self.parent_identity = parent_identity
        self.records = {}
        self.reserved = False

    def status(self, submission_id):
        record = self.records.get(submission_id)
        event = {"type": "submission_status", "submission_id": submission_id, "status": "unknown"}
        if record:
            result = record[1]
            event["status"] = (
                "pending" if result is None else ("accepted" if result["type"] == "message_accepted" else "rejected")
            )
            if result and "code" in result:
                event.update(code=result["code"], retryable=result["retryable"])
        return event

    async def submit(self, command):
        sid = command.get("submission_id")
        if not isinstance(sid, str) or not re.fullmatch("[A-Za-z0-9_-]{1,128}", sid):
            self.send(
                {
                    "type": "message_rejected",
                    "submission_id": sid if isinstance(sid, str) and len(sid) <= 128 else "",
                    "code": "invalid_submission",
                    "retryable": False,
                }
            )
            return
        try:
            digest = hashlib.sha256(
                json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
        except (ValueError, UnicodeError, TypeError):
            self.send(
                {"type": "message_rejected", "submission_id": sid, "code": "invalid_submission", "retryable": False}
            )
            return
        if sid in self.records:
            previous, result = self.records[sid]
            if previous != digest:
                self.send(
                    {
                        "type": "message_rejected",
                        "submission_id": sid,
                        "code": "submission_payload_conflict",
                        "retryable": False,
                    }
                )
            elif result is None:
                self.send(self.status(sid))
            else:
                self.send({**result, "replayed": True})
            return
        # Never evict accepted IDs and accidentally permit a duplicate launch.
        if len(self.records) >= 4096:
            self.send(
                {"type": "message_rejected", "submission_id": sid, "code": "submission_capacity", "retryable": False}
            )
            return
        self.records[sid] = (digest, None)
        refs = []
        store = None
        owned = False
        handed_off = False
        reservation_owned = False
        prepared = None
        stage = "validate_submission"
        try:
            if self.reserved or self.busy() or self.lock.locked():
                raise AppshotValidationError("backend_busy")
            self.reserved = True
            reservation_owned = True
            # An unlocked asyncio.Lock acquires without yielding; the reservation
            # blocks other backend launch paths over subsequent preparation awaits.
            await self.lock.acquire()
            owned = True
            if self.busy():
                raise AppshotValidationError("backend_busy")
            text = command.get("text")
            items = command.get("appshots")
            if (
                not isinstance(text, str)
                or len(text.encode("utf-8")) > 1024 * 1024
                or not isinstance(items, list)
                or not 1 <= len(items) <= 4
            ):
                raise AppshotValidationError("invalid_submission")
            for key in ("appshot_session_id", "appshot_broker_id"):
                if not isinstance(command.get(key), str) or not re.fullmatch("[A-Za-z0-9_-]{1,128}", command[key]):
                    raise AppshotValidationError("invalid_submission")
            parent = self.parent_identity()
            if sys.platform != "win32" and parent.uid != os.getuid():
                raise AppshotValidationError("recipient_identity_unavailable")
            decoded = []
            seen = set()
            labels = set()
            for item in items:
                if (
                    type(item) is not dict
                    or set(item) != {"label", "manifest_path"}
                    or not isinstance(item["label"], str)
                    or not re.fullmatch(r"\[Appshot #[1-9][0-9]{0,8}\]", item["label"])
                    or not isinstance(item["manifest_path"], str)
                ):
                    raise AppshotValidationError("invalid_submission")
                if item["manifest_path"] in seen or item["label"] in labels:
                    raise AppshotValidationError("duplicate_manifest")
                seen.add(item["manifest_path"])
                labels.add(item["label"])
                stage = "verify_artifact"
                if sys.platform == "win32":
                    from agent.cli.appshot_windows import read_windows_appshot
                    from agent.runtime.appshot_windows_config import resolve_windows_appshot_helper, windows_appshot_runtime

                    decoded.append(await read_windows_appshot(str(resolve_windows_appshot_helper()),
                        item["manifest_path"], broker_id=command["appshot_broker_id"],
                        session_id=command["appshot_session_id"], runtime_root=self.runtime_root or windows_appshot_runtime()))
                else:
                    with AppshotVerifier.open(
                        item["manifest_path"],
                        expected_broker_id=command["appshot_broker_id"],
                        expected_session_id=command["appshot_session_id"],
                        expected_process_start=parent.process_start,
                        runtime_root=self.runtime_root,
                    ) as verifier:
                        decoded.append(verifier.decode())
            if self.parent_identity() != parent:
                raise AppshotValidationError("recipient_identity_changed")
            if (
                sum(len(d.png_bytes) for d in decoded) > 40 * 1024 * 1024
                # Compact canonical UTF-8, matching the context projection; ASCII
                # escaping would incorrectly double the charge for Chinese AX.
                or sum(
                    len(
                        json.dumps(d.projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    )
                    for d in decoded
                )
                > 1024 * 1024
            ):
                raise AppshotValidationError("submission_size")
            blocks = build_user_message_content(text)
            for image in decoded:
                image_block = ContentBlock.image_url(image.image_data_url)
                image_block.data["appshot_verified"] = True
                blocks.extend(
                    [
                        image_block,
                        ContentBlock.appshot_context(image.source, image.projection),
                    ]
                )
            msg = Msg(sender="user", role="user", content=blocks)
            selected_context = self.context()
            stage = "prepare_request"
            prepared = await self.prepare(msg)
            if self.busy() or self.context() is not selected_context:
                raise AppshotValidationError("backend_busy")
            if self.parent_identity() != parent:
                raise AppshotValidationError("recipient_identity_changed")
            stage = "persist_media"
            store = AppshotMediaStore(selected_context.session_path)
            # Only verified Appshot images become durable; ordinary image behavior
            # remains unchanged. Durable IDs never enter provider projection.
            for index, image in enumerate(decoded):
                ref = store.put(image.png_bytes, image.width, image.height)
                refs.append(ref)
                blocks[len(blocks) - 2 * len(decoded) + 2 * index].data["appshot_media"] = ref
            stage = "launch"
            if isinstance(prepared, _PreparedAppshotCompaction):
                prepared.install()
            msg.metadata["submission_id"] = sid
            if not self.launch(msg, text):
                raise AppshotValidationError("backend_busy")
            handed_off = True
            if isinstance(prepared, _PreparedAppshotCompaction):
                prepared.committed()
            result = {"type": "message_accepted", "submission_id": sid}
            self.records[sid] = (digest, result)
            self.send(result)
        except BaseException as exc:
            if handed_off:
                # Custody/launch already committed. A broken acknowledgment
                # transport cannot turn this into a rejected, retryable turn.
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return
            if isinstance(prepared, _PreparedAppshotCompaction):
                prepared.rollback()
            if store is not None and not handed_off:
                try:
                    store.rollback(refs)
                except Exception:
                    # Cleanup uncertainty must not leave a pending replay record
                    # or mask cancellation. Retain unknown/replaced files and
                    # finish rejection; never fall back to pathname deletion.
                    logging.getLogger(__name__).warning("appshot_rollback_incomplete")
            code = (
                str(exc)
                if isinstance(exc, (AppshotValidationError, AppshotMediaError, AppshotProcessIdentityError))
                else "appshot_admission_failed"
            )
            if not re.fullmatch("[a-z_]{1,80}", code):
                code = "appshot_admission_failed"
            # Never log exception messages, paths, screenshot text or image bytes.
            logging.getLogger(__name__).warning(
                "appshot_rejected stage=%s code=%s exception=%s",
                stage, code, type(exc).__name__,
            )
            result = {
                "type": "message_rejected",
                "submission_id": sid,
                "code": code,
                "retryable": code
                in {
                    "backend_busy",
                    "context_budget_exceeded",
                    "context_budget_unavailable",
                    "appshot_vision_unavailable",
                    "session_media_unavailable",
                },
            }
            self.records[sid] = (digest, result)
            self.send(result)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
        finally:
            if reservation_owned:
                self.reserved = False
            if owned and not handed_off:
                self.lock.release()
