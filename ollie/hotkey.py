"""Global push-to-talk hotkey, implemented as a Quartz event tap.

The first implementation used pynput, and it failed in a characteristic way:
macOS *disables* an event tap whose callback does not return within about a
second, and pynput never re-enables it. Any long GIL hold — a Whisper
transcription, a model warm-up — during a keypress killed the tap silently,
so push-to-talk worked once and then never again, with nothing in the log.

This tap:

* listens for flagsChanged (modifier keys are not keyDown events) as well as
  keyDown/keyUp,
* does almost nothing in the callback, and
* when it receives kCGEventTapDisabledByTimeout / ByUserInput, turns itself
  back on and says so in the log.

Keys can be named the way they are printed on a Mac keyboard — "right option",
"caps lock", "f13" — or by their old pynput names ("alt_r", "cmd_r").

Listening globally needs the Input Monitoring / Accessibility permission.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("ollie.hotkey")

# ----------------------------------------------------------------------
# key naming
# ----------------------------------------------------------------------
ALIASES = {
    "option": "alt", "opt": "alt", "alt": "alt",
    "right_option": "alt_r", "right_opt": "alt_r", "roption": "alt_r",
    "option_right": "alt_r", "alt_r": "alt_r", "ralt": "alt_r",
    "left_option": "alt_l", "left_opt": "alt_l", "alt_l": "alt_l", "lalt": "alt_l",
    "command": "cmd", "cmd": "cmd", "meta": "cmd",
    "right_command": "cmd_r", "right_cmd": "cmd_r", "cmd_r": "cmd_r",
    "left_command": "cmd_l", "left_cmd": "cmd_l", "cmd_l": "cmd_l",
    "control": "ctrl", "ctrl": "ctrl",
    "right_control": "ctrl_r", "right_ctrl": "ctrl_r", "ctrl_r": "ctrl_r",
    "left_control": "ctrl_l", "left_ctrl": "ctrl_l", "ctrl_l": "ctrl_l",
    "shift": "shift", "right_shift": "shift_r", "shift_r": "shift_r",
    "left_shift": "shift_l", "shift_l": "shift_l",
    "caps": "caps_lock", "caps_lock": "caps_lock", "capslock": "caps_lock",
}

DISPLAY = {
    "alt": "Option (⌥)",
    "alt_r": "right Option (⌥)",
    "alt_l": "left Option (⌥)",
    "cmd": "Command (⌘)",
    "cmd_r": "right Command (⌘)",
    "cmd_l": "left Command (⌘)",
    "ctrl": "Control (⌃)",
    "ctrl_r": "right Control (⌃)",
    "ctrl_l": "left Control (⌃)",
    "shift": "Shift (⇧)",
    "shift_r": "right Shift (⇧)",
    "shift_l": "left Shift (⇧)",
    "caps_lock": "Caps Lock (⇪)",
}

# Hardware keycodes (kVK_*). Modifier and function keys are layout-independent.
KEYCODES = {
    "alt_l": {58}, "alt_r": {61}, "alt": {58, 61},
    "cmd_l": {55}, "cmd_r": {54}, "cmd": {54, 55},
    "ctrl_l": {59}, "ctrl_r": {62}, "ctrl": {59, 62},
    "shift_l": {56}, "shift_r": {60}, "shift": {56, 60},
    "caps_lock": {57},
    "f13": {105}, "f14": {107}, "f15": {113}, "f16": {106},
    "f17": {64}, "f18": {79}, "f19": {80}, "f20": {90},
    "`": {50},
}

MODIFIER_KEYCODES = {54, 55, 56, 57, 58, 59, 60, 61, 62}

KEYCODE_NAMES = {}
for _name, _codes in KEYCODES.items():
    for _code in _codes:
        if len(_codes) == 1 or _name not in ("alt", "cmd", "ctrl", "shift"):
            KEYCODE_NAMES.setdefault(_code, _name)


def normalise(name: str) -> str:
    """Accept 'right option', 'Right-Option', 'alt_r' … and return one spelling."""
    cleaned = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return ALIASES.get(cleaned, cleaned)


def pretty(name: str) -> str:
    """A name a Mac user would recognise, for logs and messages."""
    key = normalise(name)
    if key in DISPLAY:
        return DISPLAY[key]
    if len(key) == 1:
        return f"the {key!r} key"
    return key.replace("_", " ").upper() if key.startswith("f") else key.replace("_", " ")


def _flag_for(keycode: int) -> int:
    from Quartz import (
        kCGEventFlagMaskAlphaShift,
        kCGEventFlagMaskAlternate,
        kCGEventFlagMaskCommand,
        kCGEventFlagMaskControl,
        kCGEventFlagMaskShift,
    )

    return {
        58: kCGEventFlagMaskAlternate, 61: kCGEventFlagMaskAlternate,
        54: kCGEventFlagMaskCommand, 55: kCGEventFlagMaskCommand,
        59: kCGEventFlagMaskControl, 62: kCGEventFlagMaskControl,
        56: kCGEventFlagMaskShift, 60: kCGEventFlagMaskShift,
        57: kCGEventFlagMaskAlphaShift,
    }.get(keycode, 0)


# ----------------------------------------------------------------------
# the tap
# ----------------------------------------------------------------------
class _EventTap:
    """A listen-only session event tap that keeps itself alive.

    ``on_key(keycode, is_down, flags)`` is called for every keyboard event;
    ``is_down`` is None for flagsChanged events, and ``flags`` carries the
    event's own modifier bits. It runs on the tap thread and must return fast.
    """

    def __init__(self, on_key: Callable[[int, bool | None, int], None]) -> None:
        self.on_key = on_key
        self._tap = None
        self._runloop = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._ok = False

    def start(self) -> bool:
        self._started.clear()
        self._thread = threading.Thread(target=self._run, name="event-tap", daemon=True)
        self._thread.start()
        self._started.wait(3.0)
        return self._ok

    def _run(self) -> None:
        from Quartz import (
            CFMachPortCreateRunLoopSource,
            CFRunLoopAddSource,
            CFRunLoopGetCurrent,
            CFRunLoopRun,
            CGEventGetFlags,
            CGEventGetIntegerValueField,
            CGEventTapCreate,
            CGEventTapEnable,
            kCFRunLoopCommonModes,
            kCGEventFlagsChanged,
            kCGEventKeyDown,
            kCGEventKeyUp,
            kCGEventTapDisabledByTimeout,
            kCGEventTapDisabledByUserInput,
            kCGEventTapOptionListenOnly,
            kCGHeadInsertEventTap,
            kCGKeyboardEventKeycode,
            kCGSessionEventTap,
        )

        mask = (1 << kCGEventKeyDown) | (1 << kCGEventKeyUp) | (1 << kCGEventFlagsChanged)

        def callback(_proxy, type_, event, _refcon):
            if type_ in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
                # macOS switched us off (our callback stalled, or heavy input).
                # This is the failure that used to kill push-to-talk for good.
                CGEventTapEnable(self._tap, True)
                why = "timeout" if type_ == kCGEventTapDisabledByTimeout else "user input"
                log.warning("event tap disabled by macOS (%s) — re-enabled", why)
                return event
            try:
                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                flags = CGEventGetFlags(event)
                if type_ == kCGEventKeyDown:
                    self.on_key(keycode, True, flags)
                elif type_ == kCGEventKeyUp:
                    self.on_key(keycode, False, flags)
                elif type_ == kCGEventFlagsChanged:
                    self.on_key(keycode, None, flags)
            except Exception:
                log.exception("tap callback failed")
            return event

        self._tap = CGEventTapCreate(
            kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionListenOnly,
            mask, callback, None,
        )
        if self._tap is None:
            log.error(
                "could not create the keyboard event tap — Input Monitoring / "
                "Accessibility is not granted to this process"
            )
            self._ok = False
            self._started.set()
            return

        source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._runloop, source, kCFRunLoopCommonModes)
        CGEventTapEnable(self._tap, True)
        self._ok = True
        self._started.set()
        CFRunLoopRun()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ok

    @property
    def enabled(self) -> bool:
        if self._tap is None:
            return False
        try:
            from Quartz import CGEventTapIsEnabled

            return bool(CGEventTapIsEnabled(self._tap))
        except Exception:
            return False

    def reenable(self) -> None:
        if self._tap is not None:
            try:
                from Quartz import CGEventTapEnable

                CGEventTapEnable(self._tap, True)
            except Exception:
                pass

    def stop(self) -> None:
        if self._runloop is not None:
            try:
                from Quartz import CFRunLoopStop

                CFRunLoopStop(self._runloop)
            except Exception:
                pass
        self._runloop = None
        self._tap = None
        self._ok = False


# ----------------------------------------------------------------------
# push to talk
# ----------------------------------------------------------------------
class PushToTalk:
    def __init__(self, cfg, on_start: Callable[[], None], on_stop: Callable[[], None]) -> None:
        self.cfg = cfg
        self.on_start = on_start
        self.on_stop = on_stop
        self.active = False
        self._lock = threading.Lock()
        self._targets: set[int] = set()
        self._char = ""
        self._tap: _EventTap | None = None
        self._futile_heals = 0
        self._gave_up = False

    def start(self) -> bool:
        key = normalise(self.cfg.hotkey)
        self._targets = set(KEYCODES.get(key, set()))
        self._char = key if not self._targets and len(key) == 1 else ""
        if not self._targets and not self._char:
            log.error("unknown hotkey %r — push-to-talk disabled", self.cfg.hotkey)
            return False

        from .permissions import AUTHORIZED, input_monitoring_status

        status = input_monitoring_status()
        if status != AUTHORIZED:
            # The tap would be created and then immediately disabled by macOS,
            # over and over. Refuse up front with one clear message instead.
            log.error(
                "push-to-talk disabled — Input Monitoring is '%s' for this app. "
                "System Settings -> Privacy & Security -> Input Monitoring -> "
                "enable Ollie, then relaunch.", status,
            )
            return False

        self._tap = _EventTap(self._on_key)
        if not self._tap.start():
            log.error(
                "push-to-talk disabled — the event tap could not be created. "
                "Grant Ollie under Input Monitoring (or Accessibility), then relaunch."
            )
            return False

        how = "hold to talk" if self.cfg.hotkey_mode == "hold" else "tap to start and stop"
        log.info("push-to-talk: %s — %s", pretty(self.cfg.hotkey), how)
        return True

    def stop(self) -> None:
        if self._tap is not None:
            self._tap.stop()
            self._tap = None

    # ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._tap is not None and self._tap.alive

    @property
    def healthy(self) -> bool:
        return self.alive and self._tap.enabled

    def heal(self) -> None:
        """Called by the core's watchdog: cheap re-enable, full restart if dead.

        If re-enabling never sticks the permission has been revoked underneath
        us — say so once and stop, rather than log-spamming every three seconds.
        """
        if self._tap is None or self._gave_up:
            return

        self._futile_heals += 1
        if self._futile_heals > 3:
            from .permissions import AUTHORIZED, input_monitoring_status

            if input_monitoring_status() != AUTHORIZED:
                log.error(
                    "the event tap will not stay enabled because Input Monitoring "
                    "has been revoked. Re-enable Ollie there and relaunch. "
                    "Giving up on push-to-talk for this run."
                )
                self._gave_up = True
                self.stop()
                return
            self._futile_heals = 0

        if self._tap.alive and not self._tap.enabled:
            log.warning("event tap found disabled — re-enabling")
            self._tap.reenable()
        elif not self._tap.alive:
            log.warning("event tap thread died — restarting")
            self.stop()
            self.active = False
            self.start()

    def note_healthy(self) -> None:
        self._futile_heals = 0

    restart = heal          # old name, kept for compatibility

    # ------------------------------------------------------------------
    def _on_key(self, keycode: int, is_down: bool | None, flags: int) -> None:
        if self._targets:
            if keycode not in self._targets:
                return
            if is_down is None:
                # flagsChanged encodes direction in the event's own flags:
                # the bit is present on press and already gone on release.
                is_down = bool(flags & _flag_for(keycode))
        else:
            if is_down is None or not self._is_char(keycode):
                return

        with self._lock:
            if self.cfg.hotkey_mode == "toggle":
                if not is_down:
                    return
                self.active = not self.active
                fire = self.on_start if self.active else self.on_stop
            else:
                if is_down == self.active:           # repeat or stray release
                    return
                self.active = bool(is_down)
                fire = self.on_start if is_down else self.on_stop
        try:
            fire()
        except Exception:
            log.exception("hotkey callback failed")

    def _is_char(self, keycode: int) -> bool:
        return KEYCODE_NAMES.get(keycode) == self._char


# ----------------------------------------------------------------------
# single-key tap (used for "narrate the focused window")
# ----------------------------------------------------------------------
class TapKey:
    """Fire a callback on a *clean tap* of one key: press then release, with
    no other key in between and within a short window. A modifier used as
    part of a shortcut (⌘C on the same key) therefore never triggers it."""

    MAX_HOLD = 0.6
    COOLDOWN = 2.0    # ignore re-taps right after one fired: "did it register?"
                      # double-taps must not silently toggle the state back

    def __init__(self, key_name: str, on_tap: Callable[[], None]) -> None:
        self.key_name = key_name
        self.on_tap = on_tap
        self._targets: set[int] = set()
        self._tap: _EventTap | None = None
        self._down_at = 0.0
        self._fired_at = 0.0
        self._clean = False

    def start(self) -> bool:
        key = normalise(self.key_name)
        self._targets = set(KEYCODES.get(key, set()))
        if not self._targets:
            log.error("unknown window hotkey %r — window switching by key disabled",
                      self.key_name)
            return False
        self._tap = _EventTap(self._on_key)
        if not self._tap.start():
            return False
        log.info("window hotkey: tap %s to narrate the focused window", pretty(self.key_name))
        return True

    def stop(self) -> None:
        if self._tap is not None:
            self._tap.stop()
            self._tap = None

    def _on_key(self, keycode: int, is_down: bool | None, flags: int) -> None:
        import time as _time

        if keycode not in self._targets:
            self._clean = False          # some other key while ours was held
            return
        if is_down is None:
            is_down = bool(flags & _flag_for(keycode))
        if is_down:
            self._down_at = _time.time()
            self._clean = True
            return
        now = _time.time()
        if (self._clean and now - self._down_at <= self.MAX_HOLD
                and now - self._fired_at >= self.COOLDOWN):
            self._fired_at = now
            try:
                self.on_tap()
            except Exception:
                log.exception("window hotkey callback failed")
        self._clean = False


# ----------------------------------------------------------------------
# diagnostics (used by `ollie --test-hotkey`)
# ----------------------------------------------------------------------
def watch_keys(target_name: str, emit: Callable[[str], None]) -> _EventTap | None:
    """Print every keyboard event; flag the ones matching the configured hotkey."""
    key = normalise(target_name)
    targets = KEYCODES.get(key, set())

    def on_key(keycode: int, is_down: bool | None, _flags: int) -> None:
        name = KEYCODE_NAMES.get(keycode, f"keycode {keycode}")
        if is_down is None:
            action = "mod  "
        else:
            action = "down " if is_down else "up   "
        hit = "   <-- your push-to-talk key" if keycode in targets else ""
        emit(f"  {action} {name}{hit}")

    tap = _EventTap(on_key)
    if not tap.start():
        return None
    return tap
