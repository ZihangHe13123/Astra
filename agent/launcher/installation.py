"""Installation identity is resolved from the launcher, never from cwd."""

from __future__ import annotations

import importlib.metadata
import hashlib
import os
import platform
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .common import LauncherError, git, read_json


def user_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Astra"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Astra"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "astra"


@dataclass(frozen=True)
class Installation:
    root: Path
    kind: str
    owner: str
    data: Path
    sessions: Path
    control: Path
    version: str

    @property
    def python(self) -> Path:
        if self.kind == "source":
            return self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if self.kind == "desktop":
            metadata = read_json(self.root / "desktop-installation.json")
            candidate = (self.root / metadata["python"]).resolve()
            if not candidate.is_relative_to(self.root.parent / "python") or not candidate.is_file():
                raise LauncherError("The desktop Python runtime is missing or outside its bundle.")
            return candidate
        return Path(sys.executable)

    @property
    def ui(self) -> Path:
        return self.root / "ui-tui"

    @property
    def metadata(self) -> dict:
        value = read_json(self.control / "installation.json")
        if value and value.get("schema") != 1:
            raise LauncherError(f"Unsupported installation metadata: {self.control / 'installation.json'}")
        if value and value.get("root") != str(self.root):
            value = {**value, "relocated": True}
        return value

    def describe(self) -> dict:
        commit = None
        dirty = None
        if self.kind == "source":
            try:
                commit = git(self.root, "rev-parse", "HEAD")
                dirty = bool(git(self.root, "status", "--porcelain", "--untracked-files=no"))
            except LauncherError:
                pass
        return {"version": self.version, "commit": commit, "dirty": dirty, "kind": self.kind, "owner": self.owner,
                "root": str(self.root), "data": str(self.data), "sessions": str(self.sessions),
                "python": str(self.python), "platform": f"{sys.platform}/{platform.machine()}",
                "channel": self.metadata.get("branch", "configured upstream" if self.kind == "source" else "installer"),
                "relocated": bool(self.metadata.get("relocated")),
                "pending_update": (self.control / "pending.json").exists(),
                "pending_services": (self.control / "services.json").exists()}


def discover(root: Path | None = None) -> Installation:
    location = (root if root is not None else Path(__file__).resolve().parents[2]).resolve()
    source = False
    desktop = read_json(location / "desktop-installation.json")
    if desktop and (desktop.get("schema") != 1 or desktop.get("distribution") != "astra-desktop"
                    or not isinstance(desktop.get("python"), str)):
        raise LauncherError("Invalid desktop installation metadata.")
    version = "unknown"
    pyproject = location / "pyproject.toml"
    if pyproject.is_file():
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
            source = project.get("name") == "agent-lab-local" and (location / "agent").is_dir()
            version = str(project.get("version", version))
        except (OSError, ValueError) as exc:
            raise LauncherError(f"Invalid Astra pyproject.toml: {exc}") from exc
    if root is not None and not source and not desktop:
        raise LauncherError(f"Not an Astra source installation: {location}")
    owner = "git" if source and (location / ".git").exists() else "source archive"
    if desktop:
        version, owner = str(desktop["version"]), "desktop installer"
    elif not source:
        try:
            distribution = importlib.metadata.distribution("agent-lab-local")
            version = distribution.version
            owner = (distribution.read_text("INSTALLER") or "package manager").strip()
        except importlib.metadata.PackageNotFoundError:
            owner = "unknown installer"
    configured = os.environ.get("ASTRA_HOME", "").strip()
    data = Path(configured).expanduser().resolve() if configured else (location / ".astra" if source else user_home())
    sessions = Path(os.environ.get("AGENT_SESSION_DIR", str(location / ".sessions" if source and not configured
                                                             else data / "sessions"))).expanduser().resolve()
    if desktop:
        application = location.parent
        if location.parent.parent.name.lower() == "resources":
            application = location.parents[3] if location.parents[2].name == "Contents" else location.parents[2]
        if data.is_relative_to(application):
            raise LauncherError("Desktop user data must be outside the application bundle.")
        sessions = data / "sessions"
    # Installation locks cannot be bypassed by selecting a different profile/data directory.
    identity = hashlib.sha256(os.path.normcase(str(location)).encode()).hexdigest()[:20]
    control = location / ".astra/launcher" if source else user_home() / "launcher" / identity
    return Installation(location, "desktop" if desktop else "source" if source else "installed",
                        owner, data, sessions, control, version)


def runtime_environment(install: Installation, workspace: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(AGENT_PROJECT_ROOT=str(install.root), AGENT_PYTHON=str(install.python),
               ASTRA_HOME=str(install.data), AGENT_SESSION_DIR=str(install.sessions),
               ASTRA_ENV_FILE=str(install.root / ".env" if install.kind == "source" else install.data / ".env"),
               ASTRA_INSTALL_ROOT=str(install.root), PYTHONUNBUFFERED="1")
    # Record a default separately so an explicit SANDBOX_WORKDIR in the
    # installation's .env can still take precedence after dotenv is loaded.
    env.setdefault("ASTRA_WORKSPACE", str(workspace.resolve()))
    env.setdefault("AGENT_LOG_DIR", str(install.root / ".logs" if install.kind == "source" else install.data / "logs"))
    env.setdefault("ASTRA_PROXY_MODE", "off")
    if install.kind == "desktop":
        for key in ("PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(key, None)
        env.update(PYTHONPATH=str(install.root), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                   AGENT_LOG_DIR=str(install.data / "logs"))
    if env["ASTRA_PROXY_MODE"].lower() == "off":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)
    return env
