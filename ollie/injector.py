"""Put transcribed text at the cursor of whatever terminal is focused.

Two strategies:

* ``paste``  — stash the text on the pasteboard, synthesise Cmd+V, restore the
  previous pasteboard contents. Instant regardless of length; the default.
* ``type``   — post the string as unicode keyboard events in small chunks. No
  pasteboard side effects, but slower and occasionally lossy in some terminals.

Both need Accessibility permission for the process that runs Ollie (System
Settings -> Privacy & Security -> Accessibility).
"""

from __future__ import annotations

import logging
import re
import time

from .config import Config

log = logging.getLogger("ollie.injector")

_KEY_V = 9
_KEY_RETURN = 36
_CHUNK = 16

# macOS virtual keycodes for the keys autopilot may name
_KEYCODES = {
    "return": 36, "enter": 36, "escape": 53, "esc": 53, "tab": 48,
    "space": 49, "backspace": 51, "delete": 51, "forward delete": 117,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31,
    "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9,
    "w": 13, "x": 7, "y": 16, "z": 6,
}
_MODIFIERS = {
    "cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17,
    "option": 1 << 19, "opt": 1 << 19, "alt": 1 << 19,
    "control": 1 << 18, "ctrl": 1 << 18,
}


def parse_keyspec(spec: str) -> tuple[int, int] | None:
    """"cmd-a" / "shift tab" / "escape" -> (keycode, modifier flags)."""
    words = [w for w in re.split(r"[\s+\-]+", (spec or "").strip().lower())
             if w and w not in ("press", "the", "key", "arrow")]
    if not words:
        return None
    flags = 0
    for word in words[:-1]:
        if word in _MODIFIERS:
            flags |= _MODIFIERS[word]
        else:
            return None
    keycode = _KEYCODES.get(words[-1])
    return None if keycode is None else (keycode, flags)


def accessibility_trusted() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def request_accessibility(prompt: bool = True) -> bool:
    """Ask macOS for Accessibility access, showing the system dialog.

    The dialog names whichever app is *responsible* for this process. Launched
    from Ollie.app that is Ollie; launched from a shell it is your terminal,
    which is why the app bundle exists.
    """
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions

        try:
            from ApplicationServices import kAXTrustedCheckOptionPrompt as key
        except ImportError:
            key = "AXTrustedCheckOptionPrompt"
        return bool(AXIsProcessTrustedWithOptions({key: bool(prompt)}))
    except Exception as exc:
        log.debug("could not raise the Accessibility prompt: %s", exc)
        return accessibility_trusted()


def open_accessibility_settings() -> None:
    import subprocess

    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
    except Exception:
        pass


class Injector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------
    def inject(self, text: str, press_enter: bool | None = None) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        do_enter = self.cfg.press_enter if press_enter is None else press_enter
        try:
            if self.cfg.inject_mode == "type":
                self._type(text)
            else:
                self._paste(text)
            if do_enter:
                time.sleep(0.15)
                self._tap(_KEY_RETURN, 0)
            return True
        except Exception as exc:
            log.error("injection failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    def click(self, x: float, y: float, count: int = 1) -> bool:
        """Left-click (or double-click) at global screen coordinates
        (top-left origin)."""
        try:
            from Quartz import (
                CGEventCreateMouseEvent,
                CGEventPost,
                CGEventSetFlags,
                CGEventSetIntegerValueField,
                kCGEventLeftMouseDown,
                kCGEventLeftMouseUp,
                kCGEventMouseMoved,
                kCGHIDEventTap,
                kCGMouseButtonLeft,
                kCGMouseEventClickState,
            )

            point = (float(x), float(y))
            move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, point,
                                           kCGMouseButtonLeft)
            CGEventSetFlags(move, 0)     # same inherited-modifier trap as _tap
            CGEventPost(kCGHIDEventTap, move)
            time.sleep(0.04)
            for press in range(1, count + 1):
                for kind in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
                    event = CGEventCreateMouseEvent(None, kind, point,
                                                    kCGMouseButtonLeft)
                    CGEventSetFlags(event, 0)
                    CGEventSetIntegerValueField(event, kCGMouseEventClickState, press)
                    CGEventPost(kCGHIDEventTap, event)
                    time.sleep(0.04)
            return True
        except Exception as exc:
            log.error("click failed: %s", exc)
            return False

    def key(self, spec: str) -> bool:
        """Press one named key, e.g. "escape", "tab", "cmd-a"."""
        parsed = parse_keyspec(spec)
        if parsed is None:
            log.error("unknown keystroke %r", spec)
            return False
        keycode, flags = parsed
        try:
            self._tap(keycode, flags)
            return True
        except Exception as exc:
            log.error("keystroke failed: %s", exc)
            return False

    def clear_field(self) -> bool:
        """Empty the focused text field: select all, then delete."""
        try:
            from Quartz import kCGEventFlagMaskCommand

            self._tap(_KEYCODES["a"], kCGEventFlagMaskCommand)
            time.sleep(0.08)
            self._tap(_KEYCODES["backspace"], 0)
            return True
        except Exception as exc:
            log.error("clear failed: %s", exc)
            return False

    def scroll(self, x: float, y: float, lines: int) -> bool:
        """Scroll at a screen point (positive lines = up). The pointer is
        moved there first — scroll events land under the cursor."""
        try:
            from Quartz import (
                CGEventCreateMouseEvent,
                CGEventCreateScrollWheelEvent,
                CGEventPost,
                CGEventSetFlags,
                kCGEventMouseMoved,
                kCGHIDEventTap,
                kCGMouseButtonLeft,
                kCGScrollEventUnitLine,
            )

            move = CGEventCreateMouseEvent(None, kCGEventMouseMoved,
                                           (float(x), float(y)), kCGMouseButtonLeft)
            CGEventSetFlags(move, 0)
            CGEventPost(kCGHIDEventTap, move)
            time.sleep(0.05)
            for _ in range(3):           # a few small ticks scroll more reliably
                event = CGEventCreateScrollWheelEvent(
                    None, kCGScrollEventUnitLine, 1, lines)
                CGEventSetFlags(event, 0)
                CGEventPost(kCGHIDEventTap, event)
                time.sleep(0.03)
            return True
        except Exception as exc:
            log.error("scroll failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    def _paste(self, text: str) -> None:
        from AppKit import NSPasteboard, NSPasteboardTypeString
        from Quartz import kCGEventFlagMaskCommand

        board = NSPasteboard.generalPasteboard()
        previous = board.stringForType_(NSPasteboardTypeString)

        board.clearContents()
        board.setString_forType_(text, NSPasteboardTypeString)
        time.sleep(self.cfg.inject_delay)
        self._tap(_KEY_V, kCGEventFlagMaskCommand)
        time.sleep(0.25)

        if previous is not None:
            board.clearContents()
            board.setString_forType_(previous, NSPasteboardTypeString)

    def _type(self, text: str) -> None:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventKeyboardSetUnicodeString,
            CGEventPost,
            kCGHIDEventTap,
        )

        for start in range(0, len(text), _CHUNK):
            piece = text[start:start + _CHUNK]
            for is_down in (True, False):
                event = CGEventCreateKeyboardEvent(None, 0, is_down)
                CGEventKeyboardSetUnicodeString(event, len(piece), piece)
                CGEventPost(kCGHIDEventTap, event)
            time.sleep(0.006)

    def _tap(self, keycode: int, flags: int) -> None:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSetFlags,
            kCGHIDEventTap,
        )

        down = CGEventCreateKeyboardEvent(None, keycode, True)
        up = CGEventCreateKeyboardEvent(None, keycode, False)
        # Always set the flags, including to zero: a fresh CGEvent inherits
        # the *current* modifier state, so a Return synthesized right after
        # our ⌘V goes out as ⌘-Return — which chat apps silently ignore.
        CGEventSetFlags(down, flags)
        CGEventSetFlags(up, flags)
        CGEventPost(kCGHIDEventTap, down)
        time.sleep(0.02)
        CGEventPost(kCGHIDEventTap, up)
