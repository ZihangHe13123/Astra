"""Tests for search_text tool (content grep) and git_diff files scoping."""

import asyncio
import os

import pytest

from agent.runtime.tools.files import register_file_tools, DEFAULT_SEARCH_SKIP_DIRS
from agent.runtime.tools.git import register_git_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ── search_text ────────────────────────────────────────────────────


def test_search_text_basic(tmp_path):
    """Normal regex search returns file:line matches."""
    (tmp_path / "a.py").write_text("def hello():\n    print('world')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# nothing here\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "hello"}))
    assert r["error"] == ""
    assert "a.py:1: def hello()" in r["output"]


def test_search_text_glob_filter(tmp_path):
    """glob restricts matched files."""
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hello\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "hello", "glob": "*.py"}))
    assert r["error"] == ""
    assert "a.py" in r["output"]
    assert "b.txt" not in r["output"]


def test_search_text_invalid_regex(tmp_path):
    """Invalid regex is a structured failure, not a crash or a success."""
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "["}))
    assert r["code"] == "invalid_arguments"
    assert "Invalid regex" in r["error"]
    assert r["output"] == ""
    assert r["recovery_hint"]


def test_search_text_missing_path_and_file_path_are_failures(tmp_path):
    """A search root that cannot be searched fails instead of reporting success."""
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))

    missing = run(reg.execute("search_text", {"pattern": "needle", "path": "nope"}))
    assert missing["code"] == "file_not_found"
    assert missing["output"] == ""
    assert "Path not found" in missing["error"]
    assert missing["recovery_hint"]
    assert missing["details"]["path"].endswith("nope")

    not_a_directory = run(reg.execute("search_text", {"pattern": "needle", "path": "a.py"}))
    assert not_a_directory["code"] == "not_a_directory"
    assert not_a_directory["output"] == ""
    assert "glob" in not_a_directory["recovery_hint"]

    too_long = run(reg.execute("search_text", {"pattern": "x" * 501}))
    assert too_long["code"] == "invalid_arguments"
    assert "pattern too long" in too_long["error"]


def test_search_text_hits_can_be_passed_back_to_read_file(tmp_path):
    """Hits under a search subdirectory are printed relative to the workspace."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "m.py").write_text("needle = 1\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle", "path": "pkg"}))
    assert r["error"] == ""
    hit = r["output"].split("\n")[1]
    shown, lineno, _ = hit.split(":", 2)
    assert shown == os.path.join("pkg", "sub", "m.py")
    assert lineno == "1"

    read = run(reg.execute("read_file", {"path": shown}))
    assert read["error"] == ""
    assert read["output"] == "needle = 1\n"


def test_search_text_near_miss_paths_can_be_passed_back(tmp_path):
    """Suggested files use the same workspace-relative form as hits."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "configure.py").write_text("compression = 1\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    expected = os.path.join("pkg", "sub", "configure.py")
    for pattern in ("configre", "compresion"):
        r = run(reg.execute("search_text", {"pattern": pattern, "path": "pkg"}))
        assert r["error"] == ""
        suggested = [line[2:] for line in r["output"].split("\n") if line.startswith("- ")]
        assert suggested == [expected], pattern
        assert run(reg.execute("read_file", {"path": suggested[0]}))["error"] == ""


def test_search_text_outside_workspace_prints_absolute_paths(tmp_path):
    """A hit outside the workspace cannot be workspace-relative, so it is absolute."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "notes.txt").write_text("needle\n", encoding="utf-8")

    async def approve(request):
        return "once"

    reg = ToolRegistry()
    register_file_tools(reg, str(workspace))
    reg.set_approval_handler(approve)
    r = run(reg.execute("search_text", {"pattern": "needle", "path": str(outside)}))
    assert r["error"] == ""
    shown = r["output"].split("\n")[1].rsplit(":1:", 1)[0]
    assert shown == str((outside / "notes.txt").resolve())

    listed = run(reg.execute("search_files", {"pattern": "*.txt", "path": str(outside)}))
    assert listed["output"] == shown


@pytest.mark.parametrize(
    "glob,expected",
    [
        ("*.py", {"a.py", "src/b.py", "src/deep/c.py", "tests/d.py"}),
        ("**/*.py", {"a.py", "src/b.py", "src/deep/c.py", "tests/d.py"}),
        ("src/*.py", {"src/b.py"}),
        ("src/**/*.py", {"src/b.py", "src/deep/c.py"}),
        ("src/**", {"src/b.py", "src/deep/c.py", "src/e.txt"}),
        ("**/deep/*", {"src/deep/c.py"}),
        ("./src/*.txt", {"src/e.txt"}),
    ],
)
def test_search_text_glob_with_directories_matches_relative_paths(tmp_path, glob, expected):
    """A glob containing '/' is matched against the path below the search directory."""
    for relative in ("a.py", "src/b.py", "src/deep/c.py", "tests/d.py", "src/e.txt"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("needle\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle", "glob": glob}))
    assert r["error"] == ""
    found = {
        line.split(":", 1)[0].replace("\\", "/")
        for line in r["output"].split("\n")[1:]
    }
    assert found == expected


def test_search_text_path_glob_is_relative_to_the_search_directory(tmp_path):
    """With path set, the glob is applied below that directory."""
    for relative in ("pkg/src/b.py", "pkg/other/src/c.py"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("needle\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle", "path": "pkg", "glob": "src/*.py"}))
    assert r["error"] == ""
    assert r["output"].split("\n")[1:] == [os.path.join("pkg", "src", "b.py") + ":1: needle"]


def test_search_text_says_when_the_glob_excluded_every_file(tmp_path):
    """An empty result caused by the glob is not blamed on the pattern."""
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle", "glob": "*.rs"}))
    assert r["error"] == ""
    assert "glob '*.rs'" in r["output"]
    assert "No lines matching" not in r["output"]

    # The pattern is blamed only when files were actually searched.
    searched = run(reg.execute("search_text", {"pattern": "absent", "glob": "*.py"}))
    assert "No lines matching 'absent'" in searched["output"]
    assert "glob" not in searched["output"]


def test_search_text_max_results_capped(tmp_path):
    """Hits are capped at max_results."""
    lines = "\n".join(f"line{i}" for i in range(100))
    (tmp_path / "x.py").write_text(lines, encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "line", "max_results": 3}))
    assert r["error"] == ""
    assert "(capped at 3)" in r["output"]
    # Count match lines (lines with the "file:lineno: text" pattern)
    match_lines = [l for l in r["output"].split("\n") if l.startswith("x.py:")]
    assert len(match_lines) == 3
    # The tool cut the list short, so the result is marked partial.
    assert r["partial"] is True
    assert "max_results" in r["output"].split("\n")[0]


def test_search_text_complete_result_is_not_marked_partial(tmp_path):
    """Exactly max_results matches is a complete list, not a capped one."""
    (tmp_path / "x.py").write_text("line1\nline2\nline3\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "line", "max_results": 3}))
    assert r["error"] == ""
    assert r["output"].split("\n")[0] == "3 match(es):"
    assert not r.get("partial")


def test_search_text_skips_noise_dirs(tmp_path):
    """Default mode skips .astra, .venv, etc."""
    (tmp_path / "keep.py").write_text("needle\n", encoding="utf-8")
    for noise in DEFAULT_SEARCH_SKIP_DIRS:
        nd = tmp_path / noise
        nd.mkdir(parents=True, exist_ok=True)
        (nd / "noise.py").write_text("needle\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle"}))
    assert r["error"] == ""
    assert "keep.py" in r["output"]
    for noise in DEFAULT_SEARCH_SKIP_DIRS:
        assert noise not in r["output"]


def test_search_text_include_ignored(tmp_path):
    """include_ignored=True searches noise dirs too."""
    (tmp_path / ".astra").mkdir()
    (tmp_path / ".astra" / "cfg.py").write_text("needle\n", encoding="utf-8")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))

    default = run(reg.execute("search_text", {"pattern": "needle"}))
    assert ".astra" not in default["output"]

    included = run(reg.execute("search_text", {"pattern": "needle", "include_ignored": True}))
    assert ".astra" in included["output"]


def test_search_text_skips_binary(tmp_path):
    """Files with null bytes in first 8 KiB are skipped."""
    (tmp_path / "text.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00" * 100 + b"needle")

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle"}))
    assert r["error"] == ""
    assert "text.py" in r["output"]
    assert "binary.bin" not in r["output"]


def test_search_text_skips_large_file(tmp_path):
    """Files > 1 MiB are skipped."""
    (tmp_path / "small.py").write_text("needle\n", encoding="utf-8")
    big = tmp_path / "big.log"
    # Create a file just over 1 MiB
    big.write_bytes(b"x" * 1_100_000)

    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "needle"}))
    assert r["error"] == ""
    assert "small.py" in r["output"]
    assert "big.log" not in r["output"]


def test_search_text_no_match(tmp_path):
    """No matches returns a clear message."""
    (tmp_path / "a.py").write_text("nothing\n", encoding="utf-8")
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "xyzzy"}))
    assert r["error"] == ""
    assert "No lines matching" in r["output"]


def test_search_text_no_match_suggests_near_miss(tmp_path):
    """Zero matches probes for close filename suggestions."""
    (tmp_path / "configure.py").write_text("setting = 1\n", encoding="utf-8")
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "configre"}))
    assert r["error"] == ""
    assert "No lines matching" in r["output"]
    assert "configure.py" in r["output"]


def test_search_text_content_near_miss_suggests_file(tmp_path):
    """Zero matches probe file contents for close token matches."""
    (tmp_path / "main.py").write_text(
        "def compress_data():\n    return compression\n", encoding="utf-8"
    )
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "compresion"}))
    assert r["error"] == ""
    assert "No lines matching" in r["output"]
    assert "main.py" in r["output"]


def test_search_text_no_content_near_miss_when_unrelated(tmp_path):
    """Zero matches without close filenames or content stay plain."""
    (tmp_path / "main.py").write_text("x = 42\n", encoding="utf-8")
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "zzzzqqqq"}))
    assert r["error"] == ""
    assert "No lines matching" in r["output"]
    assert "Did you mean" not in r["output"]
    assert "near-miss" not in r["output"]


@pytest.mark.parametrize("include_ignored", [False, True])
def test_search_text_near_miss_respects_sandbox_override(tmp_path, include_ignored):
    """Near-miss suggestions obey the same explicit override as matches."""
    (tmp_path / ".sandbox_tmp").mkdir()
    (tmp_path / ".sandbox_tmp" / "notes.py").write_text(
        "compression config\n", encoding="utf-8"
    )
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute(
        "search_text", {"pattern": "compresion", "include_ignored": include_ignored}
    ))
    assert r["error"] == ""
    assert "No lines matching" in r["output"]
    assert (".sandbox_tmp" in r["output"]) is include_ignored


def test_search_text_no_match_no_suggestion_when_unrelated(tmp_path):
    """Zero matches without close filenames stays a plain message."""
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    reg = ToolRegistry()
    register_file_tools(reg, str(tmp_path))
    r = run(reg.execute("search_text", {"pattern": "zzzzqqqq"}))
    assert r["error"] == ""
    assert "No lines matching" in r["output"]
    assert "Did you mean" not in r["output"]


# ── git_diff files scoping ─────────────────────────────────────────


def test_git_diff_without_files_is_optional(tmp_path):
    """Omitting files still works (it's not required)."""
    import subprocess, os
    env = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    full_env = {**os.environ, **env}
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, env=full_env)
    # Create at least one commit so HEAD is valid
    (tmp_path / "readme").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "readme"], cwd=str(tmp_path), check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True, env=full_env)

    reg = ToolRegistry()
    register_git_tools(reg, str(tmp_path))
    # Call with no optional arguments — must use the HEAD default.
    r = run(reg.execute("git_diff", {}))
    assert r["error"] == ""
    assert "no diff" in r["output"] or r["output"] == ""


def test_git_diff_with_files_scopes(tmp_path):
    """Files param restricts diff to listed paths."""
    import subprocess, os
    env = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    full_env = {**os.environ, **env}
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True, env=full_env)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "with space.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True, env=full_env)
    (tmp_path / "a.py").write_text("x = 99\n", encoding="utf-8")
    (tmp_path / "with space.py").write_text("y = 99\n", encoding="utf-8")

    reg = ToolRegistry()
    register_git_tools(reg, str(tmp_path))

    # Full diff includes both
    full = run(reg.execute("git_diff", {"target": "HEAD"}))
    assert "a.py" in full["output"]
    assert "with space.py" in full["output"]

    # Scoped diff preserves a path containing spaces.
    scoped = run(
        reg.execute(
            "git_diff",
            {"target": "HEAD", "files": ["with space.py"]},
        )
    )
    assert "with space.py" in scoped["output"]
    assert "a.py" not in scoped["output"]
