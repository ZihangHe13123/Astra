"""Check source archives and installed wheels offline.

Run after locked dependency installation has populated uv's build cache. Runtime
dependencies come from the current interpreter's site-packages, but Python starts
with -I -S: neither PYTHONPATH nor editable .pth files can rescue missing modules.
The wheel itself is installed into a temporary target without dependency resolution.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def check_source_distribution(path: Path) -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = config["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"]
    blocked_parts = {
        ".git", ".hg", ".venv", ".venv-wsl", ".logs", ".tmp", ".worktrees",
        ".build", ".swiftpm", "node_modules", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", ".cache", ".vite", "output", "test-results", "playwright-report",
    }
    names = set()
    with tarfile.open(path) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if member.isdir():
                continue
            relative = PurePosixPath(*parts[1:]).as_posix()
            name = PurePosixPath(relative).name
            allowed = relative == "PKG-INFO" or any(
                relative == entry or relative.startswith(f"{entry}/") for entry in included
            )
            private = (
                bool(blocked_parts.intersection(parts))
                or name in {"persona.local.json", ".DS_Store"}
                or (name.startswith(".env") and name != ".env.example")
                or PurePosixPath(relative).suffix in {".pem", ".p12", ".pfx", ".key", ".pyc", ".pyo", ".tsbuildinfo"}
                or relative.startswith((
                    "docs/superpowers/", "docs/validation/", "evals/coding/results/",
                    "ui-core/dist/", "ui-gui/dist/", "ui-tui/dist/",
                ))
            )
            if not member.isfile() or ".." in parts or member.name.startswith("/") or not allowed or private:
                raise SystemExit(f"Source distribution contains a non-public path: {relative}")
            names.add(relative)
    required = {
        "agent/cli/main.py", "agent/runtime/prompts.py", "config/models.yaml",
        "astra.py", "README.md", "pyproject.toml",
        "ui-core/package.json", "ui-core/package-lock.json", "ui-core/tsconfig.json",
        "ui-core/src/backend-protocol.ts", "ui-core/src/backend-handshake.ts", "ui-core/src/types.ts",
        "ui-core/src/session-state.ts", "ui-core/src/agent-team-state.ts", "ui-core/src/turn-changes.ts",
        "ui-gui/package.json", "ui-gui/package-lock.json", "ui-gui/tsconfig.json",
        "ui-gui/build.mjs", "ui-gui/index.html", "ui-gui/src/main/index.ts",
        "ui-gui/src/preload/index.ts", "ui-gui/src/renderer/index.tsx", "ui-gui/src/bridge.ts",
        "ui-tui/package.json", "ui-tui/package-lock.json", "ui-tui/tsconfig.json", "ui-tui/src/index.tsx",
    }
    if missing := required - names:
        raise SystemExit(f"Source distribution is missing public inputs: {sorted(missing)}")
    print("Source distribution smoke passed: public inputs present; local artifacts excluded")


SMOKE_CODE = r"""
import importlib
import importlib.metadata
import importlib.resources
import pathlib
import sys

installed = pathlib.Path(sys.argv[1]).resolve()
sys.path[:0] = sys.argv[1:]

def deny_network(event, arguments):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError("Network access is forbidden during wheel smoke")

sys.addaudithook(deny_network)
for name in ("agent.runtime.session_recall", "agent.cli.main", "agent.cli.backend",
             "agent.runtime.context_index.embedding_runtime",
             "agent.runtime.context_index.embedding_worker",
             "agent.evals.context_index_benchmark.resources"):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(installed), name
assert "mlx.core" not in sys.modules, "importing the client must not load MLX"
catalog = importlib.resources.files("agent").joinpath("_data/models.yaml")
assert catalog.is_file(), "wheel is missing its model catalog"
contract = importlib.resources.files("agent.runtime").joinpath("astra.md")
assert contract.is_file(), "wheel is missing its core rules"
prompts = importlib.import_module("agent.runtime.prompts")
assert prompts.AGENT_CORE_PROMPT == contract.read_text(encoding="utf-8").rstrip("\n")
skills = importlib.import_module("agent.runtime.skills").SkillStore()
assert skills.list()[0]["name"] == "astra-core"
assert prompts.AGENT_CORE_PROMPT in skills.view("astra-core")
models = importlib.import_module("agent.cli.models")
assert models.model_profiles(), "installed model catalog is empty"
quality = importlib.import_module("agent.evals.context_index_benchmark.quality")
assert pathlib.Path(quality.__file__).resolve().is_relative_to(installed)
assert len(quality.load_fixture()["scenarios"]) == 24, "wheel is missing the memory quality fixture"
distribution = importlib.metadata.distribution("agent-lab-local")
assert pathlib.Path(distribution.locate_file("")).resolve() == installed
entry_points = {entry.name: entry for entry in distribution.entry_points if entry.group == "console_scripts"}
for name in ("astra", "agent-lab", "agent-lab-backend", "agent-lab-eval"):
    assert callable(entry_points[name].load()), name
print("Wheel smoke passed: installed CLI, session_recall, catalog, memory runtime/fixture and entry points")
"""


def run(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode:
        raise SystemExit(f"Wheel smoke failed with exit code {completed.returncode}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="astra-wheel-") as temporary:
        root = Path(temporary)
        wheels = root / "wheels"
        installed = root / "installed"
        run([
            "uv", "build", "--sdist", "--wheel", "--offline", "--no-python-downloads",
            "--python", sys.executable, "--out-dir", str(wheels), str(PROJECT_ROOT),
        ], cwd=root)
        sources = list(wheels.glob("*.tar.gz"))
        if len(sources) != 1:
            raise SystemExit("Distribution smoke expected exactly one freshly built source archive")
        check_source_distribution(sources[0])
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise SystemExit("Wheel smoke expected exactly one freshly built wheel")
        run([
            "uv", "pip", "install", "--offline", "--no-deps", "--no-python-downloads",
            "--python", sys.executable, "--target", str(installed), str(candidates[0]),
        ], cwd=root)
        dependency_paths = sorted({sysconfig.get_paths()[key] for key in ("purelib", "platlib")})
        run([sys.executable, "-I", "-S", "-c", SMOKE_CODE, str(installed), *dependency_paths], cwd=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
