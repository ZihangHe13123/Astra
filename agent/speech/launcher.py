"""Run a local speech server for as long as someone is using it.

    python -m agent.speech.launcher --heartbeat FILE --idle SECONDS -- COMMAND...

The server is stopped when this process's stdin closes (the Astra session that
started it has gone, however it ended) or when no session has touched the
heartbeat file for the idle period. A speech model can hold several gigabytes,
so neither an orphan nor an unused server may linger.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.speech.launcher")
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--idle", type=float, required=True)
    parser.add_argument("command", nargs="+")
    args = parser.parse_args(argv)

    owner_gone = threading.Event()

    def watch_owner() -> None:
        # Read the descriptor itself: a thread blocked inside sys.stdin's buffer
        # would hold its lock and abort the interpreter at exit.
        try:
            while os.read(sys.stdin.fileno(), 4096):
                pass
        except OSError:
            pass
        owner_gone.set()

    threading.Thread(target=watch_owner, daemon=True).start()
    server = subprocess.Popen(args.command, stdin=subprocess.DEVNULL)
    try:
        while server.poll() is None:
            try:
                unused = time.time() - args.heartbeat.stat().st_mtime
            except OSError:
                unused = 0.0
            if owner_gone.is_set() or unused > args.idle:
                break
            time.sleep(0.5)
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
