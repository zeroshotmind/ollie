#!/usr/bin/env python
"""POC: read any terminal window's text via the Accessibility API and print
clean, meaningful diffs — the de-risking step for a generic terminal reader.

Usage:
    .venv/bin/python scripts/poc_ax_reader.py                # frontmost terminal
    .venv/bin/python scripts/poc_ax_reader.py --app iTerm2   # by app name
    .venv/bin/python scripts/poc_ax_reader.py --list         # show candidate apps

Run it, then type / run commands (including over SSH) in the tracked terminal.
New lines are printed here prefixed with `+`. Spinner/progress churn should
NOT appear — that is the thing this POC exists to verify per terminal app.

Needs the Accessibility grant for whatever runs it (your terminal, when run
from a shell). No dependencies beyond pyobjc, which Ollie already ships.
"""

from __future__ import annotations

import argparse
import sys
import time

import ApplicationServices as AX
from AppKit import NSWorkspace

# Apps we consider terminals when auto-picking. Anything can be tracked
# explicitly with --app.
TERMINAL_APPS = {
    "Terminal", "iTerm2", "Ghostty", "Warp", "Alacritty", "kitty",
    "WezTerm", "Hyper", "Tabby", "rio",
}

POLL_INTERVAL = 0.5      # seconds between snapshots
STABLE_FOR = 1.0         # screen must be unchanged this long before we diff
MAX_WALK = 4000          # AX tree node budget per snapshot


# ---------------------------------------------------------------- AX plumbing
def ax_attr(element, name):
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def find_app(name_filter: str | None):
    """Pick the target app: exact name match, else frontmost terminal."""
    apps = NSWorkspace.sharedWorkspace().runningApplications()
    if name_filter:
        for app in apps:
            if app.localizedName() == name_filter:
                return app
        return None
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.localizedName() in TERMINAL_APPS:
        return front
    # frontmost is us (the invoking terminal) or not a terminal — pick the
    # most recently active known terminal instead
    for app in apps:
        if app.localizedName() in TERMINAL_APPS and app.isActive():
            return app
    for app in apps:
        if app.localizedName() in TERMINAL_APPS:
            return app
    return None


def collect_text(element, depth=0, budget=None) -> list[str]:
    """Depth-first walk collecting text from AXTextArea/AXStaticText nodes."""
    if budget is None:
        budget = [MAX_WALK]
    if budget[0] <= 0 or depth > 25:
        return []
    budget[0] -= 1
    out = []
    role = ax_attr(element, AX.kAXRoleAttribute)
    if role in ("AXTextArea", "AXStaticText"):
        value = ax_attr(element, AX.kAXValueAttribute)
        if isinstance(value, str) and value.strip():
            out.append(value)
    children = ax_attr(element, AX.kAXChildrenAttribute) or []
    for child in children:
        out.extend(collect_text(child, depth + 1, budget))
    return out


def snapshot(app_element) -> str | None:
    """Text of the app's focused (or first) window, or None if unreadable."""
    window = ax_attr(app_element, AX.kAXFocusedWindowAttribute)
    if window is None:
        windows = ax_attr(app_element, AX.kAXWindowsAttribute) or []
        window = windows[0] if windows else None
    if window is None:
        return None
    blocks = collect_text(window)
    return "\n".join(blocks) if blocks else None


# ---------------------------------------------------------------- diffing
import re

# spinner glyphs (braille, asterisk-family), counters and digits: what changes
# when a line updates *in place* without carrying new information
_CHURN = re.compile(r"[⠀-⣿✢✳✶✻✽·◂⏵]|\d+(\.\d+)?[smhk%]?|\.{2,}|…")


def skeleton(line: str) -> str:
    """A line with its churn-prone parts removed — stable across spinner
    frames, elapsed-time ticks and progress-bar updates."""
    return _CHURN.sub("", line).strip()


def skeletons(lines: list[str]) -> list[str]:
    return [skeleton(l) for l in lines]


def clean_lines(text: str) -> list[str]:
    """Snapshot -> lines, with churn-prone content normalised away."""
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        lines.append(line)
    return lines


def new_lines(prev: list[str], cur: list[str]) -> list[str]:
    """Lines in cur that are appended after the common overlap with prev.

    Terminals scroll: the tail of prev should appear somewhere in cur (or has
    scrolled off). We find the longest suffix of prev that is a contiguous
    slice of cur, and return what follows it. A repaint with identical content
    yields nothing; an in-place spinner update changes a line rather than
    appending, and is ignored on purpose.
    """
    if not prev:
        return cur
    if prev == cur:
        return []
    # try to anchor on the last few lines of prev
    for anchor_len in range(min(len(prev), 10), 0, -1):
        anchor = prev[-anchor_len:]
        for start in range(len(cur) - anchor_len, -1, -1):
            if cur[start:start + anchor_len] == anchor:
                return cur[start + anchor_len:]
    # no overlap at all (cleared screen / fast scroll): everything is new,
    # but suppress if it's just a full repaint of similar content
    prev_set = set(prev)
    return [l for l in cur if l not in prev_set]


def meaningful(delta: list[str], prev: list[str]) -> list[str]:
    """Drop delta lines that are in-place updates of an existing line —
    same skeleton as something already on screen (spinners, titles, timers)."""
    prev_skel = set(skeletons(prev))
    return [l for l in delta if skeleton(l) and skeleton(l) not in prev_skel]


# ---------------------------------------------------------------- main loop
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", help="track this app by exact name")
    parser.add_argument("--list", action="store_true", help="list candidate apps")
    parser.add_argument("--raw", action="store_true",
                        help="print full snapshots instead of diffs")
    args = parser.parse_args()

    if args.list:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            name = app.localizedName()
            if name in TERMINAL_APPS or (args.app and name == args.app):
                print(f"  {name}  (pid {app.processIdentifier()})")
        return 0

    if not AX.AXIsProcessTrusted():
        print("✗ this process lacks the Accessibility grant — enable it for "
              "your terminal in System Settings → Privacy & Security → "
              "Accessibility, then rerun", file=sys.stderr)
        return 1

    app = find_app(args.app)
    if app is None:
        print(f"✗ no terminal app found{' named ' + args.app if args.app else ''} "
              f"(try --list)", file=sys.stderr)
        return 1
    print(f"● tracking: {app.localizedName()} (pid {app.processIdentifier()}) — "
          f"type in that window, including over ssh. Ctrl-C to stop.\n")

    element = AX.AXUIElementCreateApplication(app.processIdentifier())
    # seed with the current screen so we never replay history at startup
    first = snapshot(element)
    prev: list[str] = clean_lines(first) if first else []
    pending: list[str] | None = None
    stable_since = 0.0

    while True:
        time.sleep(POLL_INTERVAL)
        text = snapshot(element)
        if text is None:
            continue
        cur = clean_lines(text)
        if args.raw:
            print("─" * 60)
            print("\n".join(cur[-40:]))
            continue

        # stability is judged on skeletons, so a spinner ticking in place
        # does not hold narration hostage forever
        if pending is None or skeletons(cur) != skeletons(pending):
            pending = cur
            stable_since = time.time()
            continue
        pending = cur
        if time.time() - stable_since < STABLE_FOR:
            continue

        delta = meaningful(new_lines(prev, cur), prev)
        if delta:
            for line in delta:
                print(f"+ {line}")
            sys.stdout.flush()
        prev = cur


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
