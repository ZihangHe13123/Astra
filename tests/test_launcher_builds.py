"""A source checkout's interface builds follow its sources, whoever changed them.

The checkout here is a stand-in with the real layout. The launcher is the real one, entered where
`astra` enters it, and its builds run through the real npm and Node.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.launcher import cli, dependencies
from agent.launcher.common import write_json
from agent.launcher.installation import discover
from agent.launcher.locking import RuntimeLease, active_instances

ROOT = Path(__file__).resolve().parents[1]

# The shared core's build: what the terminal interface loads, made from the package's source. Like tsc,
# it writes its output even when the source does not compile, and then fails.
CORE_BUILD = """
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
const source = readFileSync("src/protocol.ts", "utf8");
appendFileSync("../builds.log", "ui-core\\n");
// A test holds a build open here while another start arrives.
if (existsSync("../hold-builds")) {
  writeFileSync("../build-held", "");
  while (existsSync("../hold-builds")) await new Promise(resolve => setTimeout(resolve, 50));
}
mkdirSync("dist", { recursive: true });
writeFileSync("dist/protocol.js", source);
if (source.includes("does not compile")) { console.error("src/protocol.ts: does not compile"); process.exit(2); }
"""
# The desktop's build: one bundle of its own source, the shared core's build and the terminal client's module.
# Like the real script, it builds the shared core again first.
GUI_BUILD = """
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
appendFileSync("../builds.log", "ui-gui\\n");
mkdirSync("../ui-core/dist", { recursive: true });
writeFileSync("../ui-core/dist/protocol.js", readFileSync("../ui-core/src/protocol.ts", "utf8"));
const bundle = ["src/main.ts", "index.html", "phone-public/sw.js", "../ui-core/dist/protocol.js",
  "../ui-tui/src/appshot-client.ts"].map(name => readFileSync(name, "utf8")).join("");
for (const folder of ["dist/renderer", "dist/phone"]) mkdirSync(folder, { recursive: true });
for (const name of ["dist/main.cjs", "dist/preload.cjs", "dist/renderer/index.html", "dist/phone/bridge.js"])
  writeFileSync(name, bundle);
"""
# Stands in for the terminal interface: it loads the shared core's build as it starts and notes what it got.
INTERFACE = """
import { appendFileSync, readFileSync } from "node:fs";
appendFileSync("interface-started.log", readFileSync("ui-core/dist/protocol.js", "utf8"));
"""
# Stands in for the desktop: it notes the build it was started with, then reports its window open.
DESKTOP = """
import sys
from pathlib import Path
with open("desktop-started.log", "a", encoding="utf-8") as log:
    log.write(Path("ui-gui/dist/main.cjs").read_text(encoding="utf-8"))
Path(sys.argv[1]).write_text("{}", encoding="utf-8")
"""
# The launcher in a process of its own. Only the probe of the Python environment is left out: the
# stand-in checkout has no packages installed.
START = """
import sys
from pathlib import Path
from agent.launcher import cli, dependencies
dependencies.python_health = lambda install: None
raise SystemExit(cli.main(sys.argv[2:], root=Path(sys.argv[1])))
"""
DESKTOP_SOURCES = ("ui-gui/src/main.ts", "ui-gui/index.html", "ui-gui/phone-public/sw.js",
                   "ui-core/src/protocol.ts", "ui-tui/src/appshot-client.ts")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def package(directory: Path, script: str) -> None:
    write(directory / "package.json", '{"name": "%s", "private": true, "scripts": {"build": "%s"}}\n'
          % (directory.name, script))
    write(directory / "package-lock.json", "{}\n")


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A source installation that setup has prepared, with the desktop enabled and nothing built yet."""
    root = tmp_path / "Astra 中文 with spaces"
    write(root / "pyproject.toml", '[project]\nname="agent-lab-local"\nversion="0.2.0"\n')
    write(root / "uv.lock", "fixture lock\n")
    write(root / "agent/__init__.py", "")
    package(root / "ui-core", "node build.mjs")
    write(root / "ui-core/build.mjs", CORE_BUILD)
    write(root / "ui-core/src/protocol.ts", "core 1\n")
    package(root / "ui-tui", "node -e 0")
    write(root / "ui-tui/src/index.tsx", "// the terminal interface\n")
    write(root / "ui-tui/src/appshot-client.ts", "appshot 1\n")
    write(root / "ui-tui/node_modules/tsx/dist/cli.mjs", INTERFACE)
    package(root / "ui-gui", "node build.mjs")
    write(root / "ui-gui/build.mjs", GUI_BUILD)
    write(root / "ui-gui/src/main.ts", "desktop 1\n")
    write(root / "ui-gui/index.html", "page 1\n")
    write(root / "ui-gui/phone-public/sw.js", "phone 1\n")
    write(root / "ui-gui/node_modules/electron/cli.js", "")
    write(root / "ui-gui/node_modules/electron/path.txt", "electron")
    write(root / "ui-gui/node_modules/electron/dist/electron", "")
    install = discover(root)
    write(install.python, "")
    write_json(install.control / "installation.json", {"schema": 1, "root": str(root), "gui": True})
    write_json(install.control / "environment.json",
               {"schema": 1, "root": str(root), "fingerprint": dependencies.fingerprint(install)})
    write_json(install.control / "gui-environment.json",
               {"schema": 1, "root": str(root), "fingerprint": dependencies.gui_fingerprint(install)})
    monkeypatch.setattr(dependencies, "python_health", lambda install: None)
    # The supervisor would start Electron. Everything up to it, and its start, are the launcher's own.
    real = subprocess.Popen

    def popen(args, **options):
        if "agent.launcher.gui" in args:
            args = [sys.executable, "-c", DESKTOP, args[args.index("--ready") + 1]]
        return real(args, **options)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return install


def builds(install) -> list[str]:
    log = install.root / "builds.log"
    return log.read_text(encoding="utf-8").split() if log.is_file() else []


def started(install, interface: str = "interface") -> str:
    """What the interface found at its latest start."""
    log = install.root / f"{interface}-started.log"
    text = log.read_text(encoding="utf-8") if log.is_file() else ""
    log.unlink(missing_ok=True)
    return text


def set_times(install, names: tuple[str, ...], when: float) -> None:
    for name in names:
        folder = install.root / name
        for path in [folder, *folder.rglob("*")] if folder.is_dir() else [folder]:
            os.utime(path, (when, when))


def test_a_checkout_changed_outside_the_updater_is_rebuilt_before_the_interface_starts(checkout, capfd):
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n" and builds(checkout) == ["ui-core"]
    # A merge, a pull or an edit: the source moves on and the updater never sees it.
    write(checkout.root / "ui-core/src/protocol.ts", "core 2\n")
    capfd.readouterr()
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 2\n"
    assert builds(checkout) == ["ui-core", "ui-core"]
    assert "the build of ui-core does not match its source" in capfd.readouterr().err
    # The desktop's build is another start's business.
    assert not (checkout.root / "ui-gui/dist").exists()


def test_a_current_build_starts_as_it_is(checkout, capfd):
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n"
    capfd.readouterr()
    # None of these is a source: a file manager's and an editor's leftovers, notes for people, tests.
    for name in ("ui-core/src/.DS_Store", "ui-core/src/protocol.ts~", "ui-core/src/.protocol.ts.swp",
                 "ui-core/AGENTS.md", "ui-core/tests/protocol.test.ts", "ui-core/.cache/state"):
        write(checkout.root / name, "not a source\n")
    for _ in range(2):
        assert cli.main([], root=checkout.root) == 0
        assert started(checkout) == "core 1\n"
    assert builds(checkout) == ["ui-core"]
    assert "rebuilding" not in capfd.readouterr().err


@pytest.mark.parametrize("changed", ["ui-core/src/protocol.ts", "ui-core/src/state/new-module.ts",
                                     "ui-core/tsconfig.json", "ui-core/package.json"])
def test_every_file_the_build_reads_counts(checkout, changed):
    assert cli.main([], root=checkout.root) == 0
    path = checkout.root / changed
    write(path, (path.read_text(encoding="utf-8") if path.is_file() else "") + "\n")
    if changed.endswith("package.json"):
        # Setup verifies an edited manifest; the build is a separate question.
        write_json(checkout.control / "environment.json", {
            "schema": 1, "root": str(checkout.root), "fingerprint": dependencies.fingerprint(checkout)})
    assert cli.main([], root=checkout.root) == 0
    assert builds(checkout) == ["ui-core", "ui-core"]


def test_a_removed_source_file_counts(checkout):
    write(checkout.root / "ui-core/src/retired.ts", "retired\n")
    assert cli.main([], root=checkout.root) == 0
    (checkout.root / "ui-core/src/retired.ts").unlink()
    assert cli.main([], root=checkout.root) == 0
    assert builds(checkout) == ["ui-core", "ui-core"]


def test_a_build_from_before_records_is_kept_while_nothing_changed_after_it(checkout, capfd):
    # An installation updated to this launcher: built by an earlier one, which kept no record.
    now = time.time()
    write(checkout.root / "ui-core/dist/protocol.js", "core 1\n")
    set_times(checkout, ("ui-core", "ui-tui", "ui-gui"), now - 3600)
    set_times(checkout, ("ui-core/dist",), now - 1800)
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n" and builds(checkout) == []
    assert "rebuilding" not in capfd.readouterr().err
    # From here on the record decides, whatever the times say.
    write(checkout.root / "ui-core/src/protocol.ts", "core 2\n")
    set_times(checkout, ("ui-core/src",), now - 3600)
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 2\n" and builds(checkout) == ["ui-core"]


def test_a_build_from_before_records_is_rebuilt_when_its_source_is_newer(checkout):
    # The case that stopped the terminal interface: built, then merged into, never rebuilt.
    now = time.time()
    write(checkout.root / "ui-core/dist/protocol.js", "core 0\n")
    set_times(checkout, ("ui-core",), now - 3600)
    set_times(checkout, ("ui-core/src/protocol.ts",), now - 60)
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n" and builds(checkout) == ["ui-core"]


def test_a_failed_rebuild_stops_the_start_and_is_tried_again(checkout, capfd):
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n"
    write(checkout.root / "ui-core/src/protocol.ts", "core 2 does not compile\n")
    for attempt in (1, 2):
        capfd.readouterr()
        assert cli.main([], root=checkout.root) == 1
        error = capfd.readouterr().err
        assert "Could not rebuild ui-core" in error and "astra setup --repair" in error
        assert "src/protocol.ts: does not compile" in error  # The build's own report reaches the terminal.
        assert started(checkout) == ""
        # Its output was written and is newer than the source, as tsc leaves it. That is not a current build.
        assert (checkout.root / "ui-core/dist/protocol.js").read_text(encoding="utf-8") == "core 2 does not compile\n"
        assert builds(checkout) == ["ui-core"] * (attempt + 1)
    assert active_instances(checkout) == []
    write(checkout.root / "ui-core/src/protocol.ts", "core 3\n")
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 3\n"


def by_hand(install, name: str) -> None:
    """`npm run build` in the package, as a developer runs it."""
    subprocess.run([dependencies.npm_command(), "run", "build"], cwd=install.root / name, check=True,
                   capture_output=True)


def test_a_build_made_by_hand_from_other_source_is_not_taken_for_the_recorded_one(checkout):
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n"
    # An experiment: edit, build by hand, try it, put the source back. The source is the recorded one again.
    write(checkout.root / "ui-core/src/protocol.ts", "core 2, an experiment\n")
    by_hand(checkout, "ui-core")
    write(checkout.root / "ui-core/src/protocol.ts", "core 1\n")
    assert (checkout.root / "ui-core/dist/protocol.js").read_text(encoding="utf-8") == "core 2, an experiment\n"
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n"
    assert builds(checkout) == ["ui-core"] * 3
    assert cli.main([], root=checkout.root) == 0
    assert builds(checkout) == ["ui-core"] * 3


def test_building_again_from_the_same_source_changes_nothing(checkout):
    assert cli.main([], root=checkout.root) == 0
    by_hand(checkout, "ui-core")
    # Newer files with the same content: what was built is what was recorded.
    set_times(checkout, ("ui-core/dist",), time.time() + 3600)
    assert cli.main([], root=checkout.root) == 0
    assert builds(checkout) == ["ui-core"] * 2


@pytest.mark.parametrize("damage", ["emptied", "unreadable record", "interrupted"])
def test_a_record_without_a_finished_build_beside_it_means_rebuild(checkout, damage):
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n"
    record = checkout.root / "ui-core" / dependencies.BUILD_RECORD
    if damage == "emptied":
        # `rm -rf dist/*`: the shell's pattern leaves the hidden record.
        (checkout.root / "ui-core/dist/protocol.js").unlink()
    elif damage == "unreadable record":
        write(record, "{ cut off")
    else:
        # A build that was stopped half way: it started, and never recorded what it was made from.
        write_json(record, {"schema": 1, "sources": None})
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 1\n" and builds(checkout) == ["ui-core", "ui-core"]
    assert cli.main([], root=checkout.root) == 0
    assert builds(checkout) == ["ui-core", "ui-core"]


def test_the_text_cli_and_management_commands_build_nothing(checkout, monkeypatch, capfd):
    write(checkout.root / "ui-core/dist/protocol.js", "core 0\n")
    write_json(checkout.root / "ui-core" / dependencies.BUILD_RECORD, {"schema": 1, "sources": "of another source"})
    commands = []
    real = subprocess.run

    def run(args, **options):
        if args[0] == str(checkout.python):
            commands.append(args[1:])
            return subprocess.CompletedProcess(args, 0)
        return real(args, **options)

    monkeypatch.setattr(subprocess, "run", run)
    assert cli.main(["--cli"], root=checkout.root) == 0
    assert cli.main(["activity", "status"], root=checkout.root) == 0
    assert cli.main(["version", "--json"], root=checkout.root) == 0
    assert commands == [["-m", "agent.cli.main"], ["-m", "agent.cli.main", "activity", "status"]]
    assert builds(checkout) == []
    assert "rebuilding" not in capfd.readouterr().err


def test_setup_and_update_record_what_they_build(checkout, monkeypatch):
    monkeypatch.setattr(dependencies, "uv_command", lambda *args: "uv")
    dependencies.synchronize(checkout, [], python=False, node=False)
    assert builds(checkout) == ["ui-core", "ui-gui"]
    # The times now say that every source is newer than its build. The record says what was built.
    set_times(checkout, ("ui-core/src", "ui-gui/src", "ui-tui/src"), time.time() + 3600)
    assert cli.main([], root=checkout.root) == 0 and cli.main(["--gui"], root=checkout.root) == 0
    assert builds(checkout) == ["ui-core", "ui-gui"]
    assert started(checkout) == "core 1\n"
    assert started(checkout, "desktop") == "desktop 1\npage 1\nphone 1\ncore 1\nappshot 1\n"
    write(checkout.root / "ui-core/src/protocol.ts", "core 2\n")
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "core 2\n" and builds(checkout) == ["ui-core", "ui-gui", "ui-core"]


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks require Windows Developer Mode")
def test_a_build_shared_from_another_checkout_is_left_to_its_owner(checkout, tmp_path, capfd):
    owner = tmp_path / "owner/ui-core/dist"
    write(owner / "protocol.js", "the owner's core\n")
    (checkout.root / "ui-core/dist").symlink_to(owner, target_is_directory=True)
    set_times(checkout, ("ui-core/src",), time.time() + 3600)  # Newer than that build, and still not ours to redo.
    assert cli.main([], root=checkout.root) == 0
    assert started(checkout) == "the owner's core\n"
    assert builds(checkout) == [] and sorted(path.name for path in owner.iterdir()) == ["protocol.js"]


def start(install, *arguments: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", START, str(install.root), *arguments], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")


def test_starts_that_arrive_together_rebuild_once(checkout):
    hold, held = checkout.root / "hold-builds", checkout.root / "build-held"
    hold.touch()
    first = second = None
    try:
        first = start(checkout)
        deadline = time.monotonic() + 60
        while not held.exists():
            assert first.poll() is None and time.monotonic() < deadline, "the first start did not reach its build"
            time.sleep(0.05)
        second = start(checkout)
        # It says that it waits, and then builds nothing: the first start's build is the one it wanted.
        assert second.stderr is not None
        assert "another start is rebuilding the interface" in second.stderr.readline()
        assert second.poll() is None
        hold.unlink()
        for process in (first, second):
            output, error = process.communicate(timeout=60)
            assert process.returncode == 0, output + error
    finally:
        hold.unlink(missing_ok=True)
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
    assert builds(checkout) == ["ui-core"]
    assert started(checkout) == "core 1\ncore 1\n"
    assert active_instances(checkout) == []


def desktop_bundle(**versions: int) -> str:
    parts = {"desktop": 1, "page": 1, "phone": 1, "core": 1, "appshot": 1, **versions}
    return "".join(f"{name} {version}\n" for name, version in parts.items())


@pytest.fixture
def desktop(checkout, monkeypatch):
    """The checkout with its desktop built, as `astra setup --gui` leaves it."""
    monkeypatch.setattr(dependencies, "uv_command", lambda *args: "uv")
    dependencies.synchronize(checkout, [], python=False, node=False)
    (checkout.root / "builds.log").unlink()
    return checkout


@pytest.mark.parametrize("changed", DESKTOP_SOURCES)
def test_the_desktop_is_rebuilt_from_changed_sources_before_its_window_opens(desktop, capfd, changed):
    assert cli.main(["--gui"], root=desktop.root) == 0
    assert started(desktop, "desktop") == desktop_bundle() and builds(desktop) == []
    path = desktop.root / changed
    name = path.read_text(encoding="utf-8").split()[0]
    write(path, f"{name} 2\n")
    capfd.readouterr()
    assert cli.main(["--gui"], root=desktop.root) == 0
    # The shared core comes first: the desktop's bundle takes it from that build.
    assert builds(desktop) == (["ui-core", "ui-gui"] if name == "core" else ["ui-gui"])
    assert started(desktop, "desktop") == desktop_bundle(**{name: 2})
    captured = capfd.readouterr()
    assert "the build of ui-gui does not match its source" in captured.err
    assert "Astra desktop opened" in captured.out
    assert active_instances(desktop) == []
    assert cli.main(["--gui"], root=desktop.root) == 0
    assert builds(desktop) == (["ui-core", "ui-gui"] if name == "core" else ["ui-gui"])


def test_an_open_desktop_keeps_the_build_it_loads_from(desktop, capfd):
    write(desktop.root / "ui-gui/src/main.ts", "desktop 2\n")
    with RuntimeLease(desktop, "desktop"):
        capfd.readouterr()
        # This start only brings the open window forward.
        assert cli.main(["--gui"], root=desktop.root) == 0
        assert "the open desktop's build does not match its source" in capfd.readouterr().err
        assert builds(desktop) == []
        assert started(desktop, "desktop") == desktop_bundle()
        # The terminal interface beside it still gets a current core: the desktop carries its own copy.
        write(desktop.root / "ui-core/src/protocol.ts", "core 2\n")
        assert cli.main([], root=desktop.root) == 0
        assert started(desktop) == "core 2\n" and builds(desktop) == ["ui-core"]
    assert cli.main(["--gui"], root=desktop.root) == 0
    assert builds(desktop) == ["ui-core", "ui-gui"]
    assert started(desktop, "desktop") == desktop_bundle(desktop=2, core=2)


def test_a_desktop_rebuild_leaves_the_shared_core_current(desktop, capfd):
    write(desktop.root / "ui-gui/src/main.ts", "desktop 2\n")
    assert cli.main(["--gui"], root=desktop.root) == 0
    assert builds(desktop) == ["ui-gui"]
    # The desktop's script built the shared core again on the way, from the same source.
    capfd.readouterr()
    with RuntimeLease(desktop, "desktop"):
        assert cli.main(["--gui"], root=desktop.root) == 0
        assert "does not match its source" not in capfd.readouterr().err
    assert cli.main([], root=desktop.root) == 0
    assert started(desktop) == "core 1\n" and builds(desktop) == ["ui-gui"]


def test_a_failed_desktop_rebuild_opens_no_window(desktop, capfd):
    write(desktop.root / "ui-gui/src/main.ts", "desktop 2\n")
    (desktop.root / "ui-gui/index.html").unlink()  # The build reads it and fails.
    assert cli.main(["--gui"], root=desktop.root) == 1
    assert "Could not rebuild ui-gui" in capfd.readouterr().err
    assert started(desktop, "desktop") == "" and active_instances(desktop) == []
    write(desktop.root / "ui-gui/index.html", "page 1\n")
    assert cli.main(["--gui"], root=desktop.root) == 0
    assert started(desktop, "desktop") == desktop_bundle(desktop=2)


def test_every_tracked_interface_source_is_one_the_launcher_follows():
    """A file added beside the sources is either read by a build, and followed, or named here as not read."""
    if not (ROOT / ".git").exists():
        pytest.skip("needs the Git checkout to know which files are sources")
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "ui-core", "ui-gui"],
                             capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    install = discover(ROOT)
    followed = {path.relative_to(ROOT).as_posix()
                for name in dependencies.BUILD_SOURCES for path in dependencies.build_sources(install, name)}
    not_read = [name for name in tracked if name and name not in followed
                and "/tests/" not in name and not name.endswith(".md")]
    assert not_read == []
    # The desktop bundles modules of the terminal client, and whatever they import.
    assert "ui-tui/src/appshot-client.ts" in followed
