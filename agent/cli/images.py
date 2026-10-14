"""Image attachment helpers shared by CLI frontends."""

import base64
import mimetypes
import re
from pathlib import Path

from agent.core.msg import APPSHOT_UNTRUSTED_PREFIX, ContentBlock


SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def build_image_message_content(path: str, prompt: str = "", max_bytes: int = 10 * 1024 * 1024) -> list[ContentBlock]:
    image_path = _resolve_path(path)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if not image_path.is_file():
        raise ValueError(f"Not a file: {image_path}")

    size = image_path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"Image is too large: {size} bytes > {max_bytes} bytes")

    mime, _ = mimetypes.guess_type(str(image_path))
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError(f"Unsupported image type: {mime or image_path.suffix or 'unknown'}")

    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    blocks = []
    if prompt.strip():
        blocks.append(ContentBlock.text(prompt.strip()))
    blocks.append(ContentBlock.image_url(f"data:{mime};base64,{encoded}", source_path=str(image_path)))
    return blocks


def build_user_message_content(text: str, max_bytes: int = 10 * 1024 * 1024) -> list[ContentBlock]:
    attachments = _extract_existing_image_paths(text)
    if not attachments:
        return [ContentBlock.text(text)]

    prompt = _remove_spans(text, [span for _path, span in attachments])
    # Ordinary messages keep their exact text outside attachment spans, including
    # code indentation and line breaks. The explicit command retains its syntax.
    if re.match(r"^/image\b", prompt, flags=re.IGNORECASE):
        prompt = re.sub(r"^/image\b", "", prompt, flags=re.IGNORECASE).strip()
    blocks = [ContentBlock.text(prompt)] if prompt.strip() else [ContentBlock.text("Describe this image.")]
    for path, _span in attachments:
        image_blocks = build_image_message_content(str(path), max_bytes=max_bytes)
        blocks.extend(block for block in image_blocks if block.type == "image_url")
    return blocks


def message_display_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    lines = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text", "")
            if text:
                lines.append("[Appshot UI context]" if text.startswith(APPSHOT_UNTRUSTED_PREFIX) else text)
        elif part.get("type") == "appshot_image":
            lines.append("[Appshot image]")
        elif part.get("type") == "image_url":
            image = part.get("image_url", {})
            source = part.get("metadata", {}).get("source_path", "") or part.get("source_path", "") or image.get("url", "")
            name = Path(source).name if source and not source.startswith("data:") else "attached image"
            lines.append(f"[Image: {name}]")
    return "\n".join(lines)


def _resolve_path(path: str) -> Path:
    image_path = Path(path.strip()).expanduser()
    if not image_path.is_absolute():
        image_path = Path.cwd() / image_path
    return image_path.resolve()


def _extract_existing_image_paths(text: str) -> list[tuple[Path, tuple[int, int]]]:
    found: list[tuple[Path, tuple[int, int]]] = []
    occupied: list[tuple[int, int]] = []

    quoted_pattern = re.compile(r'(["\'])([^"\']+\.(?:png|jpe?g|webp|gif))\1', re.IGNORECASE)
    for match in quoted_pattern.finditer(text):
        path = _resolve_path(match.group(2))
        if path.exists() and path.is_file():
            found.append((path, match.span()))
            occupied.append(match.span())

    windows_pattern = re.compile(
        r'([A-Za-z]:.*?\.(?:png|jpe?g|webp|gif))(?=[A-Za-z]:|\s|$|["\'])',
        re.IGNORECASE,
    )
    for match in windows_pattern.finditer(text):
        span = match.span()
        if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
            continue
        path = _resolve_path(match.group(1))
        if path.exists() and path.is_file():
            found.append((path, span))
            occupied.append(span)

    other_pattern = re.compile(
        r'(?<!\S)([./~]?[^\s"\']+[\\/][^\s"\']+\.(?:png|jpe?g|webp|gif))',
        re.IGNORECASE,
    )
    for match in other_pattern.finditer(text):
        span = match.span()
        if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
            continue
        path = _resolve_path(match.group(1))
        if path.exists() and path.is_file():
            found.append((path, span))
            occupied.append(span)

    return sorted(found, key=lambda item: item[1][0])


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    result = []
    last = 0
    for start, end in sorted(spans):
        result.append(text[last:start])
        last = end
    result.append(text[last:])
    return "".join(result)
