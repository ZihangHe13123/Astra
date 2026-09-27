#!/usr/bin/env python3
"""Compatibility entry point for the opt-in Windows/macOS native host installer."""
import os  # noqa: F401 - compatibility for existing installer integrations
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.runtime.browser_control_install import (  # noqa: E402,F401
    BROWSERS, HOST_NAME, _paths, install, main, repair, status, uninstall,
)

if __name__ == '__main__':
    raise SystemExit(main())
