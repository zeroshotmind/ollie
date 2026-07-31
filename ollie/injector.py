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
import time

from .config import Config

log = logging.getLogger("ollie.injector")

_KEY_V = 9
_KEY_RETURN = 36
_CHUNK = 16


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
    def click(self, x: float, y: float) -> bool:
        """Left-click at global screen coordinates (top-left origin)."""
        try:
            from Quartz import (
                CGEventCreateMouseEvent,
                CGEventPost,
                CGEventSetFlags,
                kCGEventLeftMouseDown,
                kCGEventLeftMouseUp,
                kCGEventMouseMoved,
                kCGHIDEventTap,
                kCGMouseButtonLeft,
            )

            point = (float(x), float(y))
            for kind in (kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp):
                event = CGEventCreateMouseEvent(None, kind, point, kCGMouseButtonLeft)
                CGEventSetFlags(event, 0)     # same inherited-modifier trap as _tap
                CGEventPost(kCGHIDEventTap, event)
                time.sleep(0.04)
            return True
        except Exception as exc:
            log.error("click failed: %s", exc)
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
