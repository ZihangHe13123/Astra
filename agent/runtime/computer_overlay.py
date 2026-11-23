"""Bounded observation-only evidence attached to a blocked native window read."""

import base64
from collections.abc import Mapping

from agent.cli.appshots import validate_png

from .computer_text import contains_credentials


def _label(value, limit):
    if not isinstance(value, str) or len(value) > limit or contains_credentials(value):
        return None
    return "".join(char for char in value if char.isprintable()) or None


def blocker_observation(raw, app_ref: str, window_ref: str) -> tuple[dict, str | None] | None:
    """Validate the helper's exact target and keep raw imagery out of public error details."""
    if not isinstance(raw, Mapping):
        return None
    source = raw.get("blocking_overlay")
    if not isinstance(source, Mapping) or not app_ref or not window_ref or (
        source.get("app_ref") != app_ref or source.get("window_ref") != window_ref
    ):
        return None
    ax, count, rows = source.get("ax_scan"), source.get("visible_count"), source.get("windows")
    if ax not in ("clear", "overlay", "uncertain") or type(count) is not int or count < 0 or not isinstance(rows, list):
        return None
    windows = []
    for row in rows[:9]:
        if not isinstance(row, Mapping) or row.get("source") not in ("accessibility", "window_server", "window_server_and_ax"):
            continue
        info = {"source": row["source"]}
        for key, lower, upper in (("width_pt", 1, 32768), ("height_pt", 1, 32768), ("layer", -10000, 10000)):
            value = row.get(key)
            if type(value) is int and lower <= value <= upper:
                info[key] = value
        if type(row.get("modal")) is bool:
            info["modal"] = row["modal"]
        if title := _label(row.get("title"), 160):
            info["title"] = title
        windows.append(info)
    image_status = source.get("image_status")
    if image_status not in ("no_visible_blocker", "window_binding_unverified", "image_size_limit",
                            "capture_or_privacy_check_failed", "captured_exact_window"):
        image_status = "image_unavailable"
    evidence = {"ax_scan": ax, "visible_count": count, "windows": windows, "image_status": image_status}
    if app := _label(source.get("app_name"), 128):
        evidence["application"] = app
    if image_status != "captured_exact_window":
        return evidence, None
    evidence["image_status"] = "invalid_image"
    encoded = raw.get("overlay_png")
    if count == 1 and len(rows) == len(windows) == 1 and windows[0]["source"] == "window_server_and_ax" and (
        isinstance(encoded, str) and 0 < len(encoded) <= 4 * ((512 * 1024 + 2) // 3)
    ):
        width, height = windows[0].get("width_pt", 0), windows[0].get("height_pt", 0)
        if 0 < width <= 2048 and 0 < height <= 2048:
            try:
                validate_png(base64.b64decode(encoded, validate=True), width, height)
            except (ValueError, ImportError):
                return evidence, None
            evidence["image_status"] = "captured_exact_window"
            return evidence, "data:image/png;base64," + encoded
    return evidence, None
