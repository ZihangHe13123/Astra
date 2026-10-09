"""Image tools for attaching local image files to multimodal LLM turns."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..process_env import hidden_process_creationflags
from ..tool_failure import ToolFailure
from ..vision_preprocessor import VisionTileSelectionError
from .registry import ToolDef, ToolPrivateResult, ToolRegistry

SUPPORTED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _resolve_image_path(path: str, root: Path) -> Path:
    normalized = path.strip()
    if sys.platform == "win32":
        mnt_match = re.fullmatch(r"/mnt/([A-Za-z])(?:/(.*))?", normalized)
        if mnt_match:
            tail = (mnt_match.group(2) or "").replace("/", "\\")
            normalized = f"{mnt_match.group(1).upper()}:\\{tail}"
        elif normalized.startswith("/"):
            distro = (
                os.getenv("AGENT_WSL_DISTRO", "").strip()
                or "Ubuntu"
            )
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
                raise ValueError("WSL distro contains unsupported characters")
            normalized = rf"\\wsl.localhost\{distro}" + normalized.replace("/", "\\")

    image_path = Path(normalized).expanduser()
    if not image_path.is_absolute():
        image_path = root / image_path
    try:
        return image_path.resolve()
    except OSError:
        # WSL UNC paths can fail to resolve without live WSL credentials;
        # the translated path is still usable by materialization fallbacks.
        return image_path


def _wsl_distro() -> str:
    distro = (
        os.getenv("AGENT_WSL_DISTRO", "").strip()
        or "Ubuntu"
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", distro):
        raise ValueError("WSL distro contains unsupported characters")
    return distro


def _materialize_wsl_image(path: str, root: Path, max_bytes: int) -> Path:
    """Copy an inaccessible WSL file into the project cache without shell parsing."""
    normalized = path.strip()
    if not normalized.startswith("/") or normalized.startswith("/mnt/"):
        raise ValueError("Only absolute WSL Linux paths can be materialized")
    distro = _wsl_distro()
    stat = subprocess.run(
        ["wsl.exe", "-d", distro, "--", "stat", "-c", "%s", "--", normalized],
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=hidden_process_creationflags(),
    )
    if stat.returncode != 0:
        detail = stat.stderr.decode("utf-8", errors="replace").strip()
        raise FileNotFoundError(f"WSL image not found: {normalized}" + (f" ({detail})" if detail else ""))
    try:
        size = int(stat.stdout.decode("ascii", errors="strict").strip())
    except (UnicodeError, ValueError) as exc:
        raise OSError(f"Could not determine WSL image size: {normalized}") from exc
    if size > max_bytes:
        raise ValueError(f"Image is too large: {size} bytes > {max_bytes} bytes")

    cache = root / ".astra" / "image-cache"
    cache.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{distro}\0{normalized}".encode()).hexdigest()[:12]
    target = cache / f"{digest}-{Path(normalized).name}"
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("wb") as output:
        copied = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "cat", "--", normalized],
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            creationflags=hidden_process_creationflags(),
        )
    if copied.returncode != 0:
        temporary.unlink(missing_ok=True)
        detail = copied.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(f"Could not copy WSL image: {normalized}" + (f" ({detail})" if detail else ""))
    temporary.replace(target)
    return target.resolve()


def _prepare_image_path(path: str, root: Path, max_bytes: int) -> Path:
    image_path = _resolve_image_path(path, root)
    try:
        _validate_image_path(image_path, max_bytes)
        return image_path
    except (FileNotFoundError, PermissionError):
        if sys.platform != "win32" or not path.strip().startswith("/") or path.strip().startswith("/mnt/"):
            raise
    image_path = _materialize_wsl_image(path, root, max_bytes)
    _validate_image_path(image_path, max_bytes)
    return image_path


def _validate_image_path(path: Path, max_bytes: int) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image extension: {path.suffix or '(none)'}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"Image is too large: {size} bytes > {max_bytes} bytes")
    mime, _ = mimetypes.guess_type(str(path))
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError(f"Unsupported image MIME type: {mime or 'unknown'}")


def _png_text_chunks(path: Path, max_metadata_bytes: int = 4 * 1024 * 1024) -> tuple[dict[str, str], dict[str, Any]]:
    """Read PNG text metadata without requiring Pillow."""
    metadata: dict[str, str] = {}
    image_info: dict[str, Any] = {"format": "PNG"}
    consumed = 0
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Image is not a PNG file")
        while True:
            header = stream.read(8)
            if not header:
                break
            if len(header) != 8:
                raise ValueError("Truncated PNG chunk header")
            length, chunk_type = struct.unpack(">I4s", header)
            if length > 64 * 1024 * 1024:
                raise ValueError("PNG chunk is unreasonably large")
            data = stream.read(length)
            crc = stream.read(4)
            if len(data) != length or len(crc) != 4:
                raise ValueError("Truncated PNG chunk")
            if chunk_type == b"IHDR" and len(data) >= 13:
                width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
                image_info.update({
                    "width": width,
                    "height": height,
                    "bit_depth": bit_depth,
                    "color_type": color_type,
                })
            elif chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
                consumed += length
                if consumed > max_metadata_bytes:
                    raise ValueError("PNG text metadata exceeds the configured limit")
                try:
                    if chunk_type == b"tEXt":
                        keyword, value = data.split(b"\x00", 1)
                        metadata[keyword.decode("latin-1")] = value.decode("latin-1")
                    elif chunk_type == b"zTXt":
                        keyword, remainder = data.split(b"\x00", 1)
                        if not remainder or remainder[0] != 0:
                            continue
                        metadata[keyword.decode("latin-1")] = zlib.decompress(remainder[1:]).decode("latin-1")
                    else:
                        keyword, remainder = data.split(b"\x00", 1)
                        if len(remainder) < 2:
                            continue
                        compressed, method = remainder[0], remainder[1]
                        remainder = remainder[2:]
                        _language, remainder = remainder.split(b"\x00", 1)
                        _translated, value = remainder.split(b"\x00", 1)
                        if compressed:
                            if method != 0:
                                continue
                            value = zlib.decompress(value)
                        metadata[keyword.decode("latin-1")] = value.decode("utf-8")
                except (ValueError, UnicodeError, zlib.error):
                    continue
            if chunk_type == b"IEND":
                break
    return metadata, image_info


def _linked_node(workflow: dict[str, Any], link: Any) -> dict[str, Any]:
    if isinstance(link, list) and link:
        node = workflow.get(str(link[0]))
        return node if isinstance(node, dict) else {}
    return {}


def _prompt_text(node: dict[str, Any], previous: tuple[str, bool]) -> tuple[str, bool]:
    """Return ``(text, linked)`` for a prompt node, or ``previous`` without a text input.

    A linked ``text`` input holds a node reference such as ``["12", 0]``. The
    linked node may be a template, wildcard or translation step whose output is
    not its own text, so the prompt is reported as not extracted.
    """
    inputs = node.get("inputs", {})
    if not isinstance(inputs, dict) or "text" not in inputs:
        return previous
    text = inputs["text"]
    if isinstance(text, list):
        return "", True
    return str(text), False


def _summarize_comfyui_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    loras: list[dict[str, Any]] = []
    samplers: list[dict[str, Any]] = []
    latent: list[dict[str, Any]] = []
    outputs: list[str] = []
    positive: tuple[str, bool] = ("", False)
    negative: tuple[str, bool] = ("", False)
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        if class_type in {"UNETLoader", "CheckpointLoaderSimple", "CLIPLoader", "VAELoader"}:
            keys = ("unet_name", "ckpt_name", "clip_name", "type", "vae_name", "weight_dtype")
            models.append({"node": str(node_id), "type": class_type, **{
                key: inputs[key] for key in keys if key in inputs
            }})
        elif "LoraLoader" in class_type:
            loras.append({
                "node": str(node_id),
                "name": inputs.get("lora_name", ""),
                "strength_model": inputs.get("strength_model"),
                "strength_clip": inputs.get("strength_clip"),
            })
        elif class_type in {"KSampler", "KSamplerAdvanced"}:
            fields = (
                "seed", "noise_seed", "steps", "cfg", "sampler_name", "scheduler",
                "denoise", "control_after_generate", "start_at_step", "end_at_step",
            )
            sampler = {"node": str(node_id), **{key: inputs[key] for key in fields if key in inputs}}
            samplers.append(sampler)
            positive = _prompt_text(_linked_node(workflow, inputs.get("positive")), positive)
            negative = _prompt_text(_linked_node(workflow, inputs.get("negative")), negative)
        elif class_type in {"EmptyLatentImage", "EmptySD3LatentImage"}:
            latent.append({"node": str(node_id), **{
                key: inputs[key] for key in ("width", "height", "batch_size") if key in inputs
            }})
        elif class_type in {"SaveImage", "PreviewImage"} and inputs.get("filename_prefix"):
            outputs.append(str(inputs["filename_prefix"]))
    summary: dict[str, Any] = {
        "format": "comfyui",
        "node_count": len(workflow),
        # Prompts are intentionally first: small local models often stop
        # attending before the end of a long model/LoRA/sampler inventory.
        "positive_prompt": positive[0],
        "negative_prompt": negative[0],
        "models": models,
        "loras": loras,
        "samplers": samplers,
        "latent_images": latent,
        "filename_prefixes": outputs,
    }
    not_extracted = [
        name
        for name, (_text, linked) in (("positive_prompt", positive), ("negative_prompt", negative))
        if linked
    ]
    if not_extracted:
        summary["prompts_not_extracted"] = not_extracted
    return summary


def _inspect_image_metadata(path: Path, include_raw: bool = False) -> dict[str, Any]:
    if path.suffix.lower() != ".png":
        return {
            "success": True,
            "prompt_extracted": False,
            "raw_metadata_included": False,
            "raw_metadata_needed_for_prompt": False,
            "path": str(path),
            "metadata_found": False,
            "message": "Generation metadata extraction currently supports PNG files; use read_image for pixels.",
        }
    metadata, image_info = _png_text_chunks(path)
    generation: dict[str, Any] | None = None
    plain_prompt = ""
    prompt = metadata.get("prompt", "")
    if prompt:
        try:
            workflow = json.loads(prompt)
            if isinstance(workflow, dict):
                generation = _summarize_comfyui_workflow(workflow)
        except json.JSONDecodeError:
            plain_prompt = prompt
    parameters = metadata.get("parameters", "")
    # Name only fields this result carries: ComfyUI prompts sit under
    # generation, an A1111 chunk or a plain prompt string is top-level.
    prompt_fields = [
        f"generation.{key}"
        for key in ("positive_prompt", "negative_prompt")
        if generation and generation.get(key)
    ]
    if plain_prompt:
        prompt_fields.append("prompt")
    if parameters:
        prompt_fields.append("parameters")
    not_extracted = [
        f"generation.{key}" for key in (generation or {}).get("prompts_not_extracted", [])
    ]
    prompt_extracted = bool(prompt_fields) and not not_extracted
    raw_needed = bool(metadata) and not prompt_extracted
    if prompt_extracted:
        guidance = (
            f"The exact prompt is available in {' and '.join(prompt_fields)}. "
            f"Continue the user's task now using {'that field' if len(prompt_fields) == 1 else 'those fields'}. "
            "No additional metadata extraction is required."
        )
    elif not_extracted:
        guidance = (
            f"Not extracted: {' and '.join(not_extracted)} (the text comes from another workflow node). "
            + (f"Available: {' and '.join(prompt_fields)}. " if prompt_fields else "")
            + "Retry with include_raw=true if the missing prompt text is required."
        )
    elif raw_needed:
        guidance = "Known prompt fields were not found; retry with include_raw=true only if the raw workflow is required."
    else:
        guidance = "No embedded generation prompt was found. Do not retry with include_raw=true."

    # Status and actionable prompt data deliberately precede file diagnostics.
    result: dict[str, Any] = {
        "success": True,
        "prompt_extracted": prompt_extracted,
        "raw_metadata_included": include_raw,
        "raw_metadata_needed_for_prompt": raw_needed,
        "guidance": guidance,
    }
    if generation is not None:
        result["generation"] = generation
    if plain_prompt:
        result["prompt"] = plain_prompt
    if parameters:
        result["parameters"] = parameters
    result.update({
        "path": str(path),
        "image": image_info,
        "metadata_found": bool(metadata),
        "metadata_keys": sorted(metadata),
    })
    if generation is None and not parameters and not plain_prompt:
        result["message"] = "PNG text metadata exists, but no recognized ComfyUI or A1111 generation payload was found."
    if include_raw:
        result["raw_metadata"] = metadata
    return result


def select_vision_tiles_from_holder(
    agent_holder: dict[str, Any],
    tile_set_id: str,
    tile_ids: list[str],
) -> dict:
    """Resolve the live agent lazily for entrypoint registration callbacks."""
    agent = agent_holder.get("agent")
    selector = getattr(agent, "select_vision_tiles", None) if agent is not None else None
    if not callable(selector):
        raise VisionTileSelectionError(
            "Vision tile selection is unavailable until the agent is ready."
        )
    result = selector(tile_set_id, tile_ids)
    if not isinstance(result, dict):
        raise VisionTileSelectionError(
            "Vision tile selection is unavailable because the active agent returned an invalid result."
        )
    return result


def register_image_tools(
    registry: ToolRegistry,
    workdir: str = ".",
    *,
    select_tiles: Callable[[str, list[str]], dict] | None = None,
):
    root = Path(workdir).resolve()

    def _read_image(path: str, question: str = "", detail: str = "auto") -> str:
        """Attach a local image to the next multimodal model call."""
        max_bytes = _positive_int_env("MAX_IMAGE_UPLOAD_BYTES", 10 * 1024 * 1024)
        image_path = _prepare_image_path(path, root, max_bytes)
        payload = {
            "success": True,
            "type": "image_attachment",
            "image_paths": [str(image_path)],
            "question": question,
            "detail": detail,
            "message": "Image attached for the next model turn. Inspect the actual pixels before answering.",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _inspect_metadata(path: str, include_raw: bool = False) -> str:
        max_bytes = _positive_int_env("MAX_IMAGE_METADATA_BYTES", 100 * 1024 * 1024)
        image_path = _prepare_image_path(path, root, max_bytes)
        return json.dumps(
            _inspect_image_metadata(image_path, include_raw=include_raw),
            ensure_ascii=False,
            indent=2,
        )

    def _read_image_tiles(tile_set_id: str, tile_ids: list[str]) -> ToolPrivateResult | ToolFailure:
        if select_tiles is None:
            return ToolFailure(
                code="vision_tile_selection_unavailable",
                message="Vision tile selection is unavailable for this model request.",
                retryable=False,
            )
        opaque_id = tile_set_id.strip() if isinstance(tile_set_id, str) else ""
        if not opaque_id:
            return ToolFailure(
                code="invalid_arguments",
                message="tile_set_id must be a non-empty string.",
                retryable=True,
            )
        if not isinstance(tile_ids, list) or not 1 <= len(tile_ids) <= 11:
            return ToolFailure(
                code="invalid_arguments",
                message="tile_ids must contain between 1 and 11 IDs.",
                retryable=True,
            )
        if any(not isinstance(tile_id, str) or not tile_id.strip() for tile_id in tile_ids):
            return ToolFailure(
                code="invalid_arguments",
                message="Every tile ID must be a non-empty string.",
                retryable=True,
            )
        normalized_ids = [tile_id.strip() for tile_id in tile_ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            return ToolFailure(
                code="invalid_arguments",
                message="tile_ids must be unique; duplicate IDs are not allowed.",
                retryable=True,
            )
        try:
            selection = select_tiles(opaque_id, normalized_ids)
        except VisionTileSelectionError as exc:
            return ToolFailure(
                code="vision_tile_selection_rejected",
                message=str(exc),
                retryable=True,
            )
        except Exception:  # noqa: BLE001 - injected integration failures must cross a sanitized boundary
            return ToolFailure(
                code="vision_tile_selection_failed",
                message="Unable to select image tiles for this request.",
                retryable=False,
            )
        if not isinstance(selection, dict):
            return ToolFailure(
                code="vision_tile_selection_failed",
                message="The bounded tile selector returned an invalid result.",
                retryable=False,
            )
        data_urls = selection.get("data_urls")
        labels = selection.get("labels")
        unserved_ids = selection.get("unserved_ids")
        remaining_images = selection.get("remaining_images")
        remaining_inline_bytes = selection.get("remaining_inline_bytes")
        if (
            not isinstance(data_urls, (list, tuple))
            or not all(
                isinstance(url, str) and url.startswith("data:image/png;base64,")
                for url in data_urls
            )
            or not isinstance(labels, (list, tuple))
            or not all(isinstance(label, str) for label in labels)
            or len(data_urls) != len(labels)
            or not isinstance(unserved_ids, (list, tuple))
            or not all(isinstance(tile_id, str) for tile_id in unserved_ids)
            or isinstance(remaining_images, bool)
            or not isinstance(remaining_images, int)
            or remaining_images < 0
            or isinstance(remaining_inline_bytes, bool)
            or not isinstance(remaining_inline_bytes, int)
            or remaining_inline_bytes < 0
        ):
            return ToolFailure(
                code="vision_tile_selection_failed",
                message="The bounded tile selector returned an invalid result.",
                retryable=False,
            )
        payload = {
            "success": True,
            "type": "image_attachment",
            "image_labels": list(labels),
            "unserved_ids": list(unserved_ids),
            "remaining_images": remaining_images,
            "remaining_inline_bytes": remaining_inline_bytes,
            "message": (
                "Selected original-pixel tiles are attached for the next model turn. "
                "Inspect the actual pixels and respect the reported remaining budgets."
            ),
        }
        return ToolPrivateResult(
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            private={"image_data_urls": list(data_urls)},
        )

    registry.register(ToolDef(
        name="read_image",
        description=(
            "Attach a local image file to the next vision-capable model turn for inspection. "
            "Use this when the user asks you to look at, judge, compare, OCR, debug, or describe a local image path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Local image path (.png, .jpg, .jpeg, .webp, .gif). Windows paths, WSL UNC paths, "
                        "and WSL Linux paths such as /home/user/image.png are supported. Relative paths resolve "
                        "from the agent workdir."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": "What to inspect in the image, e.g. layout issues, aesthetic quality, OCR, or comparison criteria.",
                    "default": "",
                },
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high", "original"],
                    "description": "Requested image detail level for models that support it.",
                    "default": "auto",
                },
            },
            "required": ["path"],
        },
        fn=_read_image, risk="read", idempotent=True, group="image",
        sandboxed=False,
        timeout=30,
    ))
    registry.register(ToolDef(
        name="inspect_image_metadata",
        description=(
            "Extract exact generation settings from an existing PNG before guessing from pixels. "
            "For a ComfyUI PNG the default result contains the parsed positive/negative prompts plus models, "
            "LoRAs, seed, sampler, CFG, steps, resolution, and output prefix under generation; an A1111 PNG "
            "returns its settings text as parameters. When prompt_extracted=true, the current result is "
            "authoritative: continue the user task from the fields its guidance names. "
            "Use read_image separately for visual quality."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Local or WSL UNC image path."},
                "include_raw": {
                    "type": "boolean",
                    "description": (
                        "Diagnostic only: include complete raw PNG text chunks when "
                        "raw_metadata_needed_for_prompt=true or the user explicitly requests workflow JSON. "
                        "Do not enable when prompt_extracted=true."
                    ),
                    "default": False,
                },
            },
            "required": ["path"],
        },
        fn=_inspect_metadata, risk="read", idempotent=True, group="image",
        sandboxed=False, timeout=30,
    ))
    if select_tiles is not None:
        tile_calls_per_turn = 2
        registry.register(ToolDef(
            name="read_image_tiles",
            description=(
                "Attach bounded original-pixel detail tiles from the current request's opaque tile set. "
                "Use only tile_set_id and tile_ids listed in the annotated overview manifest. "
                "This tool accepts no file paths and reports unserved IDs and remaining request budgets. "
                f"Limit: {tile_calls_per_turn} calls per turn, one tile_set_id per call; ask for all the "
                "tiles you need within those calls. A further call is not run and ends the turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tile_set_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Opaque request-scoped tile set ID from the overview manifest.",
                    },
                    "tile_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 11,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                        "description": "One to eleven unique logical tile IDs from that manifest.",
                    },
                },
                "required": ["tile_set_id", "tile_ids"],
                "additionalProperties": False,
            },
            fn=_read_image_tiles,
            risk="read",
            idempotent=False,
            cache_results=False,
            group="image",
            max_calls_per_turn=tile_calls_per_turn,
            sandboxed=False,
            timeout=30,
        ))
