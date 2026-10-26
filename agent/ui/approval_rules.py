"""The person's standing approval rules ("以后都允许"), listed and removed from the desktop's permissions panel.

Each rule was added by the person from an approval card. A running Astra reads the rules again whenever
settings.json changes, so a removed rule stops applying with the next request.
"""

from __future__ import annotations

import json
import re
import sys

from agent.cli.model_preferences import model_settings_path
from agent.runtime.tools.grants import AllowRules


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    rules = AllowRules(model_settings_path())
    try:
        if args in ([], ["list"]):
            print(json.dumps({"ok": True, "rules": rules.all()}))
            return 0
        if len(args) == 2 and args[0] == "remove" and re.fullmatch(r"[0-9a-f]{32}", args[1]):
            print(json.dumps({"ok": rules.remove(args[1])}))
            return 0
        print(json.dumps({"ok": False, "error": "Unsupported approval rule change."}))
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
