"""Exercise discovery and on-demand resource reads through the real skill tool."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.skills import register_skill_tools


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / ".astra" / "skills" / "development" / "web-product-debug"


def test_skill_resources_are_delivered_and_disclosed_on_demand(tmp_path):
    root = tmp_path / "skills"
    installed = root / "development" / "web-product-debug"
    shutil.copytree(SOURCE, installed, ignore=shutil.ignore_patterns("__pycache__"))
    store = SkillStore(root)
    registry = ToolRegistry()
    register_skill_tools(registry, store)

    async def check():
        listed = await registry.execute("skills_list", {})
        item = next(item for item in json.loads(listed["output"])["skills"]
                    if item["name"] == "web-product-debug")
        assert item["category"] == "development"
        catalog = store.catalog_prompt()
        entry = await registry.execute("skill_view", {"name": item["name"]})
        assert not entry["error"]
        assert entry["output"] == (installed / "SKILL.md").read_text(encoding="utf-8")
        assert entry["output"] not in catalog

        resources = [p for p in installed.rglob("*") if p.is_file() and p.name != "SKILL.md"]
        assert resources
        for path in resources:
            relative = path.relative_to(installed).as_posix()
            viewed = await registry.execute("skill_view", {"name": item["name"], "file_path": relative})
            assert not viewed["error"]
            assert viewed["output"] == path.read_text(encoding="utf-8")
            assert viewed["output"] not in catalog

        reference = installed / "references" / "debugging.md"
        reference.write_text("Updated diagnostic instructions\n", encoding="utf-8")
        assert store.catalog_prompt() == catalog
        refreshed = await registry.execute("skill_view", {
            "name": item["name"], "file_path": "references/debugging.md",
        })
        assert refreshed["output"] == reference.read_text(encoding="utf-8")

    asyncio.run(check())


def test_new_skill_files_are_not_excluded_from_source_delivery():
    for path in SOURCE.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        result = subprocess.run(
            ["git", "-c", f"safe.directory={REPO.as_posix()}", "check-ignore", "--no-index", "--quiet", str(path)],
            cwd=REPO, check=False,
        )
        assert result.returncode == 1, path


@pytest.mark.parametrize("source", ["override", "home", "relative", "missing"])
def test_documented_locator_works_from_an_unrelated_project(tmp_path, source):
    """Execute the documented setup example, rather than assuming an install cwd."""
    setup = (SOURCE / "references" / "setup.md").read_text(encoding="utf-8")
    code = setup.split("```python\n", 1)[1].split("```", 1)[0]
    project = tmp_path / "unrelated project"
    project.mkdir()
    home = tmp_path / "astra data"
    root = tmp_path / "custom skills" if source == "override" else home / "skills"
    installed = root / "development" / "web-product-debug"
    shutil.copytree(SOURCE, installed, ignore=shutil.ignore_patterns("__pycache__"))
    env = {key: value for key, value in os.environ.items()
           if key not in {"AGENT_SKILLS_PATH", "ASTRA_HOME"}}
    env["PYTHONUTF8"] = "1"
    if source in {"override", "home", "relative"}:
        env["ASTRA_HOME"] = str(home)
    if source in {"override", "relative"}:
        env["AGENT_SKILLS_PATH"] = str(root) if source == "override" else "custom-skills"
    result = subprocess.run([sys.executable, "-c", code], cwd=project, env=env,
                            capture_output=True, text=True, encoding="utf-8", check=False)
    if source in {"relative", "missing"}:
        assert result.returncode != 0
        assert result.stdout == ""
    else:
        assert result.returncode == 0, result.stderr
        assert Path(json.loads(result.stdout)["launcher"]) == installed / "scripts" / "playwright_cli.py"
