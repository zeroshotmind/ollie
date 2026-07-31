"""Read any window's text through the Accessibility API.

This is the "works with anything" reader: it tracks one specific window —
picked from the orb menu, the way video-call apps pick a window to share —
and narrates whatever new text appears in it. Because it reads what is
painted on screen, SSH sessions, tmux, containers and remote consoles all
work with zero setup on the other end.

The hard part is churn: spinners, progress bars and window titles rewrite
themselves in place constantly. Two defences, proven in scripts/poc_ax_reader.py:

  * lines are compared by *skeleton* — spinner glyphs, timers, counters and
    digits stripped — so in-place updates are invisible to both the
    stability gate and the diff
  * nothing is emitted until the screen has been skeleton-stable for a
    moment; agents pause when they finish something, spinners never do

Unlike the transcript reader there is no role information: everything is
emitted as one assistant-role chunk and the filter sorts out what matters.
"""

from __future__ import annotations

import logging
import time

import ApplicationServices as AX
from AppKit import NSWorkspace

from ..config import Config
from ..message import Chunk
from .base import Reader

log = logging.getLogger("ollie.reader.window")

import re

# What changes when a line updates *in place* without carrying new
# information: spinner glyphs (braille and asterisk family), counters,
# digits, ellipses.
_CHURN = re.compile(r"[⠀-⣿✢✳✶✻✽·◂⏵]|\d+(\.\d+)?[smhk%]?|\.{2,}|…")

MAX_WALK = 4000          # AX tree node budget per snapshot
STABLE_FOR = 1.0         # seconds the screen must hold still before we diff
MAX_LINES_PER_CHUNK = 80 # cap a single emission (window switches, clears)
TURN_QUIET = 6.0         # quiet seconds after output before a turn is over


def _ax_attr(element, name):
    err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _collect_text(element, depth=0, areas=None, statics=None, budget=None) -> list[str]:
    """Window text. Text areas (the terminal content) are preferred; static
    texts (title bars, labels) are used only when no text area exists —
    otherwise the ever-present title line pollutes the diff anchoring."""
    top = areas is None
    if top:
        areas, statics, budget = [], [], [MAX_WALK]
    if budget[0] <= 0 or depth > 25:
        return []
    budget[0] -= 1
    role = _ax_attr(element, AX.kAXRoleAttribute)
    if role in ("AXTextArea", "AXStaticText"):
        value = _ax_attr(element, AX.kAXValueAttribute)
        if isinstance(value, str) and value.strip():
            (areas if role == "AXTextArea" else statics).append(value)
    for child in _ax_attr(element, AX.kAXChildrenAttribute) or []:
        _collect_text(child, depth + 1, areas, statics, budget)
    if top:
        return areas if areas else statics
    return []


def skeleton(line: str) -> str:
    return _CHURN.sub("", line).strip()


def _clean_lines(text: str) -> list[str]:
    return [l.rstrip() for l in text.splitlines() if l.strip()]


def _new_lines(prev: list[str], cur: list[str]) -> list[str]:
    """Lines appended after the longest suffix of prev found intact in cur.

    A repaint of identical content yields nothing; an in-place update changes
    a line rather than appending and is ignored on purpose.
    """
    if not prev:
        return cur
    if prev == cur:
        return []
    for anchor_len in range(min(len(prev), 10), 0, -1):
        anchor = prev[-anchor_len:]
        for start in range(len(cur) - anchor_len, -1, -1):
            if cur[start:start + anchor_len] == anchor:
                delta = cur[start + anchor_len:]
                if delta:
                    return delta
                # empty delta but the screen changed: a static trailing
                # element (window title) can anchor at the very end while new
                # content landed above it — fall through to the set diff
                break
        else:
            continue
        break
    prev_set = set(prev)
    return [l for l in cur if l not in prev_set]


def _meaningful(delta: list[str], prev: list[str]) -> list[str]:
    """Drop delta lines that are in-place rewrites of something on screen."""
    prev_skel = {skeleton(l) for l in prev}
    return [l for l in delta if skeleton(l) and skeleton(l) not in prev_skel]


# ---------------------------------------------------------------- picking
def list_windows(max_windows: int = 24) -> list[dict]:
    """Enumerate pickable windows: [{pid, index, app, title}].

    Ordinary apps only (no menu-bar accessories, no Ollie itself), most
    recently active app first — the same shape a screen-share picker shows.
    """
    import os

    out = []
    apps = NSWorkspace.sharedWorkspace().runningApplications()
    for app in sorted(apps, key=lambda a: not a.isActive()):
        if app.activationPolicy() != 0:          # regular apps only
            continue
        pid = app.processIdentifier()
        if pid == os.getpid():
            continue
        element = AX.AXUIElementCreateApplication(pid)
        windows = _ax_attr(element, AX.kAXWindowsAttribute) or []
        for index, win in enumerate(windows):
            title = _ax_attr(win, AX.kAXTitleAttribute) or ""
            out.append({
                "pid": pid,
                "index": index,
                "app": app.localizedName() or "?",
                "title": title.strip() or "(untitled)",
            })
            if len(out) >= max_windows:
                return out
    return out


def frontmost_window() -> dict | None:
    """The window the user is looking at right now: {pid, index, app, title}.

    Used by the window hotkey — tap it with any window focused and narration
    pins to that window, no menu needed.
    """
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    pid = app.processIdentifier()
    element = AX.AXUIElementCreateApplication(pid)
    focused = _ax_attr(element, AX.kAXFocusedWindowAttribute)
    if focused is None:
        return None
    windows = _ax_attr(element, AX.kAXWindowsAttribute) or []
    index = 0
    for i, win in enumerate(windows):
        if win == focused:               # CFEqual via pyobjc
            index = i
            break
    title = (_ax_attr(focused, AX.kAXTitleAttribute) or "").strip() or "(untitled)"
    return {"pid": pid, "index": index,
            "app": app.localizedName() or "?", "title": title}


class WindowReader(Reader):
    """Narrate one pinned window, wherever its output comes from."""

    name = "window"

    def __init__(self, cfg: Config, pid: int, window_index: int, label: str = "") -> None:
        self.cfg = cfg
        self.pid = pid
        self.window_index = window_index
        self.label = label or f"pid {pid}"
        self._window = None
        self._prev: list[str] = []
        self._pending: list[str] | None = None
        self._stable_since = 0.0
        self._misses = 0
        self._counter = 0
        self._last_emit = 0.0        # when we last emitted content
        self._turn_open = False      # content emitted, turn_end not yet sent

    # -- Reader contract -----------------------------------------------
    def start(self) -> None:
        app = AX.AXUIElementCreateApplication(self.pid)
        windows = _ax_attr(app, AX.kAXWindowsAttribute) or []
        if self.window_index < len(windows):
            self._window = windows[self.window_index]
        if self._window is None:
            log.error("window %s not found (pid %s)", self.window_index, self.pid)
            return
        # join at the tail: never replay what is already on screen
        text = self._snapshot()
        self._prev = _clean_lines(text) if text else []
        log.info("pinned window: %s", self.label)

    def poll(self) -> list[Chunk]:
        if self._window is None:
            return []
        text = self._snapshot()
        if text is None:
            self._misses += 1
            if self._misses == 10:
                log.warning("window '%s' unreadable — closed?", self.label)
            return []
        self._misses = 0
        cur = _clean_lines(text)

        # stability gate on skeletons: a ticking spinner neither triggers
        # narration nor holds it hostage forever
        cur_skel = [skeleton(l) for l in cur]
        pending_skel = None if self._pending is None else [skeleton(l) for l in self._pending]
        if pending_skel is None or cur_skel != pending_skel:
            self._pending = cur
            self._stable_since = time.time()
            return []
        self._pending = cur
        if time.time() - self._stable_since < STABLE_FOR:
            return []

        delta = _meaningful(_new_lines(self._prev, cur), self._prev)
        self._prev = cur
        if not delta:
            # A screen that stays quiet after producing output = end of turn.
            # This is what lets autopilot drive arbitrary windows: the same
            # signal the transcript reader gets from explicit turn records.
            if self._turn_open and time.time() - self._last_emit > TURN_QUIET:
                self._turn_open = False
                self._counter += 1
                return [Chunk(
                    role="turn_end", text="", source="window",
                    key=f"window:{self.pid}:{self.window_index}:{self._counter}",
                )]
            return []
        if len(delta) > MAX_LINES_PER_CHUNK:
            delta = delta[-MAX_LINES_PER_CHUNK:]
        self._counter += 1
        self._last_emit = time.time()
        self._turn_open = True
        return [Chunk(
            role="assistant",
            text="\n".join(delta),
            source="window",
            key=f"window:{self.pid}:{self.window_index}:{self._counter}",
        )]

    def stop(self) -> None:
        self._window = None

    def describe(self) -> str:
        return f"window reader — {self.label}"

    def frame_on_screen(self) -> tuple[float, float, float, float] | None:
        """The pinned window's (x, y, w, h) in AX coordinates (top-left
        origin) — used to draw the selection border around it."""
        if self._window is None:
            return None
        try:
            point_type = getattr(AX, "kAXValueCGPointType", 1)
            size_type = getattr(AX, "kAXValueCGSizeType", 2)
            err, pos_val = AX.AXUIElementCopyAttributeValue(
                self._window, AX.kAXPositionAttribute, None)
            if err != 0:
                return None
            err, size_val = AX.AXUIElementCopyAttributeValue(
                self._window, AX.kAXSizeAttribute, None)
            if err != 0:
                return None
            ok_p, point = AX.AXValueGetValue(pos_val, point_type, None)
            ok_s, size = AX.AXValueGetValue(size_val, size_type, None)
            if not (ok_p and ok_s):
                return None
            return (float(point.x), float(point.y),
                    float(size.width), float(size.height))
        except Exception:
            return None

    def focus(self) -> bool:
        """Bring the pinned window to the front so injection lands in it."""
        if self._window is None:
            return False
        try:
            from AppKit import NSRunningApplication

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
            if app is None:
                return False
            app.activateWithOptions_(1 << 1)   # NSApplicationActivateIgnoringOtherApps
            AX.AXUIElementPerformAction(self._window, "AXRaise")
            return True
        except Exception:
            log.exception("could not focus pinned window")
            return False

    # -- internals ------------------------------------------------------
    def _snapshot(self) -> str | None:
        blocks = _collect_text(self._window)
        return "\n".join(blocks) if blocks else None
