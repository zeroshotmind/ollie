#!/usr/bin/env python3
"""Register the SessionStart hook in ~/.claude/settings.json (idempotent).

Run with --uninstall to remove it again.
"""

import json
import os
import sys

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
HOOK = os.path.abspath(os.path.join(os.path.dirname(__file__), "session_hook.py"))
COMMAND = f"{sys.executable} {HOOK}"
MARKER = "session_hook.py"


def load():
    if not os.path.exists(SETTINGS):
        return {}
    try:
        with open(SETTINGS) as handle:
            return json.load(handle)
    except Exception:
        print(f"! {SETTINGS} is not valid JSON — fix it first")
        raise SystemExit(1)


def save(data):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.exists(SETTINGS):
        backup = SETTINGS + ".ollie-backup"
        with open(SETTINGS) as src, open(backup, "w") as dst:
            dst.write(src.read())
        print(f"  backed up existing settings to {backup}")
    with open(SETTINGS, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def main() -> int:
    uninstall = "--uninstall" in sys.argv
    data = load()
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("SessionStart", [])

    kept = []
    for entry in entries:
        commands = [h for h in entry.get("hooks", []) if MARKER not in str(h.get("command", ""))]
        if commands:
            entry["hooks"] = commands
            kept.append(entry)
        elif not entry.get("hooks"):
            kept.append(entry)
    removed = len(entries) - len(kept)

    if uninstall:
        hooks["SessionStart"] = kept
        if not kept:
            hooks.pop("SessionStart")
        save(data)
        print(f"removed {removed} Ollie hook(s)")
        return 0

    kept.append({"hooks": [{"type": "command", "command": COMMAND}]})
    hooks["SessionStart"] = kept
    save(data)
    print(f"installed SessionStart hook -> {COMMAND}")
    print("Start a new Claude Code session for it to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
