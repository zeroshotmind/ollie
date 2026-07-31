"""The floating orb pet.

An always-on-top, borderless, transparent window that sits over your terminal
and shows one thing: what Ollie is doing right now.

  idle       calm slow breathing, cool blue
  listening  green, radius driven by microphone amplitude
  thinking   amber, a highlight sweeping around the rim
  speaking   violet, radius driven by the outgoing audio

Clicks outside the orb pass straight through to the window underneath, so it
never gets in the way. Drag it anywhere; right-click for the menu.
"""

from __future__ import annotations

import logging
import math
import time

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSColorSpace,
    NSGradient,
    NSMenu,
    NSMenuItem,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakePoint, NSMakeRect, NSTimer

from .state import AppState, State

log = logging.getLogger("ollie.orb")

PALETTE = {
    State.IDLE:      ((0.38, 0.52, 0.78), (0.16, 0.22, 0.38)),
    State.LISTENING: ((0.30, 0.86, 0.58), (0.08, 0.34, 0.24)),
    State.THINKING:  ((0.98, 0.74, 0.30), (0.40, 0.26, 0.06)),
    State.SPEAKING:  ((0.70, 0.48, 0.98), (0.26, 0.14, 0.44)),
}
FPS = 30.0


def _color(rgb, alpha):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _circle(cx, cy, radius):
    return NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2)
    )


class OrbView(NSView):
    def initWithFrame_state_controller_(self, frame, state, controller):
        self = objc.super(OrbView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.app_state = state
        self.controller = controller
        self.level = 0.0
        self.born = time.time()
        return self

    def initWithFrame_state_(self, frame, state):
        return self.initWithFrame_state_controller_(frame, state, None)

    # -- painting ------------------------------------------------------
    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(rect)

        bounds = self.bounds()
        cx = bounds.size.width / 2.0
        cy = bounds.size.height / 2.0
        unit = min(cx, cy)

        state = self.app_state.state
        target = self.app_state.amplitude
        # Asymmetric smoothing: jump up quickly, fall away gently.
        gain = 0.55 if target > self.level else 0.12
        self.level += (target - self.level) * gain

        elapsed = time.time() - self.born
        bright, dark = PALETTE.get(state, PALETTE[State.IDLE])

        if state is State.IDLE:
            scale = 1.0 + 0.035 * math.sin(elapsed * 1.1)
            glow = 0.32
        elif state is State.THINKING:
            scale = 1.0 + 0.05 * math.sin(elapsed * 3.4)
            glow = 0.45 + 0.12 * math.sin(elapsed * 2.0)
        else:
            scale = 1.0 + 0.30 * self.level
            glow = 0.40 + 0.45 * self.level

        radius = unit * 0.40 * scale

        # Outer halo. A two-stop gradient to alpha 0 renders almost flat with a
        # hard edge, so the falloff is spelled out stop by stop.
        peak = min(0.26, glow * 0.30)
        halo = NSGradient.alloc().initWithColors_atLocations_colorSpace_(
            [
                _color(bright, peak),
                _color(bright, peak * 0.55),
                _color(bright, peak * 0.20),
                _color(bright, 0.0),
            ],
            [0.0, 0.42, 0.72, 1.0],
            NSColorSpace.genericRGBColorSpace(),
        )
        halo.drawInBezierPath_relativeCenterPosition_(
            _circle(cx, cy, min(unit * 0.99, radius * 1.95)), NSMakePoint(0.0, 0.0)
        )

        # body — the shaded side is blended back toward the hue so the orb
        # reads as one object instead of a dark disc inside a glow
        shade = tuple(b * 0.42 + d * 0.58 for b, d in zip(bright, dark))
        gradient = NSGradient.alloc().initWithStartingColor_endingColor_(
            _color(bright, 0.97), _color(shade, 0.95)
        )
        gradient.drawInBezierPath_relativeCenterPosition_(
            _circle(cx, cy, radius), NSMakePoint(-0.25, 0.30)
        )

        # rim
        rim = _circle(cx, cy, radius)
        rim.setLineWidth_(1.4)
        _color(bright, 0.55 + 0.35 * self.level).set()
        rim.stroke()

        if state is State.THINKING:
            self._draw_sweep(cx, cy, radius * 1.30, elapsed)

        # specular highlight
        _color((1.0, 1.0, 1.0), 0.20 + 0.18 * self.level).set()
        _circle(cx - radius * 0.28, cy + radius * 0.30, radius * 0.20).fill()

        pilot = getattr(self.controller, "autopilot", None)
        if pilot is not None and pilot.enabled:
            ring = _circle(cx, cy, radius * 1.42)
            ring.setLineWidth_(2.2)
            pulse = 0.55 + 0.25 * math.sin(elapsed * 2.6)
            _color((1.0, 0.62, 0.18), pulse).set()
            ring.stroke()

        if getattr(self.controller, "muted", False):
            slash = NSBezierPath.bezierPath()
            slash.moveToPoint_(NSMakePoint(cx - radius * 0.62, cy - radius * 0.62))
            slash.lineToPoint_(NSMakePoint(cx + radius * 0.62, cy + radius * 0.62))
            slash.setLineWidth_(radius * 0.13)
            slash.setLineCapStyle_(1)
            _color((0.05, 0.05, 0.07), 0.75).set()
            slash.stroke()

    @objc.python_method
    def _draw_sweep(self, cx, cy, radius, elapsed):
        angle = (elapsed * 110.0) % 360.0
        arc = NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(cx, cy), radius, angle, angle + 62.0
        )
        arc.setLineWidth_(2.4)
        arc.setLineCapStyle_(1)
        _color((1.0, 0.90, 0.62), 0.75).set()
        arc.stroke()

    # -- interaction ---------------------------------------------------
    def hitTest_(self, point):
        """Only the orb itself is clickable; the rest of the window is a hole."""
        local = self.convertPoint_fromView_(point, None)
        bounds = self.bounds()
        cx = bounds.size.width / 2.0
        cy = bounds.size.height / 2.0
        radius = min(cx, cy) * 0.62
        if (local.x - cx) ** 2 + (local.y - cy) ** 2 <= radius * radius:
            return self
        return None

    def mouseDown_(self, event):
        window = self.window()
        try:
            window.performWindowDragWithEvent_(event)
        except Exception:
            pass

    def rightMouseDown_(self, event):
        menu = NSMenu.alloc().initWithTitle_("Ollie")

        muted = bool(getattr(self.controller, "muted", False))
        self._add(menu, "Unmute narration" if muted else "Mute narration", "toggleMute:")

        pilot = getattr(self.controller, "autopilot", None)
        if pilot is not None:
            if pilot.enabled:
                goal = (pilot.goal[:40] + "…") if len(pilot.goal) > 40 else pilot.goal
                label = (f"Autopilot ON ({pilot.turns}/{pilot.cfg.autopilot_max_turns}"
                         f"{': ' + goal if goal else ', waiting for goal'}) — disarm")
            else:
                label = "Autopilot — arm (then speak the goal)"
            self._add(menu, label, "toggleAutopilot:")
        menu.addItem_(NSMenuItem.separatorItem())

        current = getattr(getattr(self.controller, "cfg", None), "style", "brief")
        for style, label in (("brief", "Brief — one-line summaries"),
                             ("full", "Full — loss-less retelling"),
                             ("verbatim", "Verbatim — the agent's own words")):
            item = self._add(menu, label, f"setStyle{style.capitalize()}:")
            item.setState_(1 if style == current else 0)
        cfg = getattr(self.controller, "cfg", None)
        engine = getattr(cfg, "tts_engine", "say")
        self._submenu(menu, "Engine", [
            ("macOS say — instant, robotic", "say", engine == "say"),
            ("Kokoro — neural, natural", "kokoro", engine == "kokoro"),
        ], "pickEngine:")
        self._submenu(menu, "Voice", self._voice_items(cfg), "pickVoice:")
        self._submenu(menu, "Tone", self._tone_items(cfg), "pickTone:")
        models = self._model_names(cfg)
        if models:
            current_filter = getattr(cfg, "ollama_model", "")
            current_pilot = getattr(cfg, "autopilot_model", "") or current_filter
            self._submenu(menu, "Narration model",
                          [(m, m, m == current_filter) for m in models],
                          "pickFilterModel:")
            self._submenu(menu, "Autopilot model",
                          [(m, m, m == current_pilot) for m in models],
                          "pickAutopilotModel:")

        menu.addItem_(NSMenuItem.separatorItem())
        self._submenu(menu, "Narrate", self._source_items(), "pickSource:")

        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Settings & dependencies…", "openReport:")
        self._add(menu, "Accessibility settings…", "openAccess:")
        self._add(menu, "Input Monitoring settings…", "openInput:")
        self._add(menu, "Microphone settings…", "openMic:")
        self._add(menu, "Open log", "openLog:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quit Ollie", "quitOllie:", "q")

        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)

    @objc.python_method
    def _add(self, menu, title, action, key=""):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, objc.selector(getattr(self, action.replace(":", "_")), signature=b"v@:@"), key
        )
        item.setTarget_(self)
        menu.addItem_(item)
        return item

    def toggleMute_(self, sender):
        if self.controller is not None:
            self.controller.toggle_mute()

    def toggleAutopilot_(self, sender):
        if self.controller is not None:
            self.controller.toggle_autopilot()

    @objc.python_method
    def _voice_items(self, cfg):
        if getattr(cfg, "tts_engine", "say") == "kokoro":
            from .tts import KOKORO_VOICES

            current = getattr(cfg, "kokoro_voice", "")
            return [(v, v, v == current) for v in KOKORO_VOICES]
        from .tts import list_voices

        current = getattr(cfg, "voice", "")
        names = [name for name, _ in list_voices()][:14]
        if current and current not in names:
            names.insert(0, current)
        return [(name, name, name == current) for name in names]

    @objc.python_method
    def _source_items(self):
        """What to narrate: the Claude Code transcript, or any open window —
        picked the way video-call apps pick a window to share."""
        reader = getattr(self.controller, "reader", None)
        on_transcript = getattr(reader, "name", "") != "window"
        pinned = (getattr(reader, "pid", None), getattr(reader, "window_index", None))
        items = [("Claude Code session (default)", "claude", on_transcript)]
        try:
            from .readers.window import list_windows

            for win in list_windows():
                title = win["title"]
                if len(title) > 46:
                    title = title[:46] + "…"
                label = f"{win['app']} — {title}"
                value = f"{win['pid']}:{win['index']}:{label}"
                checked = (win["pid"], win["index"]) == pinned
                items.append((label, value, checked))
        except Exception:
            log.exception("could not list windows")
        return items

    @objc.python_method
    def _model_names(self, cfg):
        """Installed Ollama models; cached briefly so the menu opens fast."""
        import time as _time

        cache = getattr(self, "_models_cache", None)
        if cache and _time.time() - cache[0] < 30.0:
            return cache[1]
        from .config import Config
        from .filter import list_models

        names = list_models(cfg or Config.load({}))
        self._models_cache = (_time.time(), names)
        return names

    def pickFilterModel_(self, sender):
        if self.controller is not None:
            self.controller.set_filter_model(sender.representedObject())

    def pickAutopilotModel_(self, sender):
        if self.controller is not None:
            self.controller.set_autopilot_model(sender.representedObject())

    def pickSource_(self, sender):
        if self.controller is None:
            return
        value = sender.representedObject()
        if value == "claude":
            self.controller.narrate_transcript()
            return
        pid, index, label = value.split(":", 2)
        self.controller.narrate_window(int(pid), int(index), label)

    @objc.python_method
    def _tone_items(self, cfg):
        current = getattr(cfg, "tone", "neutral")
        return [
            ("Neutral", "neutral", current == "neutral"),
            ("Warm", "warm", current == "warm"),
            ("Snarky", "snarky", current == "snarky"),
            ("Minimal", "minimal", current == "minimal"),
        ]

    @objc.python_method
    def _submenu(self, menu, title, items, action):
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        sub = NSMenu.alloc().initWithTitle_(title)
        for label, value, checked in items:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, objc.selector(getattr(self, action.replace(":", "_")),
                                     signature=b"v@:@"), ""
            )
            item.setTarget_(self)
            item.setRepresentedObject_(value)
            item.setState_(1 if checked else 0)
            sub.addItem_(item)
        parent.setSubmenu_(sub)
        menu.addItem_(parent)

    def pickEngine_(self, sender):
        if self.controller is not None:
            self.controller.set_engine(sender.representedObject())

    def pickVoice_(self, sender):
        if self.controller is not None:
            self.controller.set_voice(sender.representedObject())

    def pickTone_(self, sender):
        if self.controller is not None:
            self.controller.set_tone(sender.representedObject())

    @objc.python_method
    def _set_style(self, style):
        if self.controller is not None:
            self.controller.set_style(style)

    def setStyleBrief_(self, sender):
        self._set_style("brief")

    def setStyleFull_(self, sender):
        self._set_style("full")

    def setStyleVerbatim_(self, sender):
        self._set_style("verbatim")

    def openReport_(self, sender):
        import threading

        from .config import Config
        from .settings_report import open_report

        cfg = getattr(self.controller, "cfg", None) or Config.load({})
        threading.Thread(target=open_report, args=(cfg,), daemon=True).start()

    def openAccess_(self, sender):
        from .permissions import open_settings

        open_settings("accessibility")

    def openMic_(self, sender):
        from .permissions import open_settings

        open_settings("microphone")

    def openInput_(self, sender):
        from .permissions import open_settings

        open_settings("input_monitoring")

    def openLog_(self, sender):
        import subprocess

        from .config import LOG_PATH

        subprocess.Popen(["open", "-t", str(LOG_PATH)])

    def quitOllie_(self, sender):
        self.app_state.stop()
        NSApplication.sharedApplication().terminate_(None)


class OrbWindow(NSPanel):
    """A non-activating panel: clicking or dragging the orb must never move
    focus away from the terminal, or speech would be pasted into the wrong
    window (or nowhere)."""

    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


def run_orb(state: AppState, size: int = 130, margin: int = 28, controller=None) -> None:
    """Runs the Cocoa event loop. Must be called on the main thread."""
    app = NSApplication.sharedApplication()
    # Accessory policy: no Dock icon, and we never steal focus from the terminal.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    screen = NSScreen.mainScreen()
    visible = screen.visibleFrame() if screen is not None else NSMakeRect(0, 0, 1440, 900)
    x = visible.origin.x + visible.size.width - size - margin
    y = visible.origin.y + margin
    frame = NSMakeRect(x, y, size, size)

    window = OrbWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame,
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    window.setBecomesKeyOnlyIfNeeded_(True)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setHasShadow_(False)
    window.setLevel_(NSStatusWindowLevel)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    window.setIgnoresMouseEvents_(False)

    view = OrbView.alloc().initWithFrame_state_controller_(
        NSMakeRect(0, 0, size, size), state, controller
    )
    window.setContentView_(view)
    window.orderFrontRegardless()

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        1.0 / FPS, True, lambda _timer: view.setNeedsDisplay_(True)
    )

    def supervise(_timer):
        if not state.running:
            app.terminate_(None)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.25, True, supervise)

    log.info("orb running at %.0f,%.0f", x, y)
    app.run()
