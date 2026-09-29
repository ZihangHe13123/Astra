"""A packaged desktop owns a standard runtime lease until its stdin closes."""
from __future__ import annotations

import argparse
import json
import sys

from .installation import discover
from .locking import RuntimeLease


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease", action="store_true", required=True)
    parser.parse_args()
    install = discover()
    if install.kind != "desktop":
        parser.error("The desktop lease requires a packaged installation.")
    with RuntimeLease(install, "desktop"):
        print(json.dumps({"ready": True}), flush=True)
        sys.stdin.buffer.read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
