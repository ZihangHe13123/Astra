"""Private application state, independent of the user's working project."""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def state_dir(root: Path | None = None) -> Path:
    configured = os.environ.get("ASTRA_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    source = root if root is not None else _PACKAGE_ROOT
    if source != _PACKAGE_ROOT or (source / "pyproject.toml").is_file():
        return source / ".astra"
    from agent.launcher.installation import user_home

    return user_home()


def state_path(*parts: str, root: Path | None = None) -> Path:
    return state_dir(root).joinpath(*parts)


def sessions_dir(root: Path | None = None) -> Path:
    configured = os.environ.get("AGENT_SESSION_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    source = root if root is not None else _PACKAGE_ROOT
    if not os.environ.get("ASTRA_HOME", "").strip() and (source != _PACKAGE_ROOT or (source / "pyproject.toml").is_file()):
        return source / ".sessions"
    return state_path("sessions", root=root)


def env_file(root: Path) -> Path:
    configured = os.environ.get("ASTRA_ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if root != _PACKAGE_ROOT or (root / "pyproject.toml").is_file():
        return root / ".env"
    return state_path(".env")
