#!/usr/bin/env python3
"""Claude Code SessionStart hook.

Claude Code pipes a JSON blob on stdin containing ``session_id`` and
``transcript_path``. We record it at ~/.ollie/current_session.json so Ollie
attaches to the right transcript instantly instead of guessing the newest file.

Install with:  python scripts/install_hook.py
"""

import json
import os
import sys
import time

TARGET = os.path.join(os.path.expanduser("~"), ".ollie", "current_session.json")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                      # never break the user's session

    transcript = payload.get("transcript_path")
    if not transcript:
        return 0

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    tmp = TARGET + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(
            {
                "session_id": payload.get("session_id"),
                "transcript_path": transcript,
                "cwd": payload.get("cwd"),
                "written_at": time.time(),
            },
            handle,
        )
    os.replace(tmp, TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
