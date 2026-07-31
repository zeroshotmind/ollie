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
        self.hover = False          # pointer over the orb window
        self.hover_level = 0.0      # smoothed, drives the toggle fade
        self.tip_key = None         # toggle under the pointer, for the tooltip
        return self

    def updateTrackingAreas(self):
        from AppKit import (
            NSTrackingActiveAlways,
            NSTrackingArea,
            NSTrackingInVisibleRect,
            NSTrackingMouseEnteredAndExited,
            NSTrackingMouseMoved,
        )

        objc.super(OrbView, self).updateTrackingAreas()
        for area in list(self.trackingAreas() or []):
            self.removeTrackingArea_(area)
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved
            | NSTrackingActiveAlways | NSTrackingInVisibleRect,
            self, None,
        ))

    def mouseEntered_(self, event):
        self.hover = True
        # The pointer always hovers before it can right-click: prefetch the
        # slow menu ingredients (Ollama models, say voices, the AX window
        # walk) now, so the menu itself builds from warm caches instantly.
        self._prefetch_menu_data()

    @objc.python_method
    def _prefetch_menu_data(self):
        import threading as _threading
        import time as _time

        if getattr(self, "_prefetching", False):
            return
        self._prefetching = True

        def refresh():
            try:
                now = _time.time()
                cache = getattr(self, "_models_cache", None)
                if not cache or now - cache[0] >= 30.0:
                    from .config import Config
                    from .filter import list_models

                    cfg = getattr(self.controller, "cfg", None) or Config.load({})
                    self._models_cache = (_time.time(), list_models(cfg))
                cache = getattr(self, "_voices_cache", None)
                if not cache or now - cache[0] >= 300.0:
                    from .tts import list_voices

                    self._voices_cache = (_time.time(), list_voices())
                from .readers.window import list_windows

                self._windows_cache = (_time.time(), list_windows())
            except Exception:
                log.exception("menu prefetch failed")
            finally:
                self._prefetching = False

        _threading.Thread(target=refresh, daemon=True).start()

    def mouseExited_(self, event):
        self.hover = False
        self.tip_key = None

    def mouseMoved_(self, event):
        # macOS never shows native tooltips over a non-activating panel, so
        # the hovered button is tracked here and the tip drawn by hand
        local = self.convertPoint_fromView_(event.locationInWindow(), None)
        key = None
        for k, bx, by, r, on in self._buttons():
            if (local.x - bx) ** 2 + (local.y - by) ** 2 <= (r * 1.25) ** 2:
                key = k
                break
        self.tip_key = key

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

        target = 1.0 if self.hover else 0.0
        self.hover_level += (target - self.hover_level) * 0.30
        if self.hover_level > 0.02:
            self._draw_toggles(self.hover_level)

        if getattr(self.controller, "muted", False):
            slash = NSBezierPath.bezierPath()
            slash.moveToPoint_(NSMakePoint(cx - radius * 0.62, cy - radius * 0.62))
            slash.lineToPoint_(NSMakePoint(cx + radius * 0.62, cy + radius * 0.62))
            slash.setLineWidth_(radius * 0.13)
            slash.setLineCapStyle_(1)
            _color((0.05, 0.05, 0.07), 0.75).set()
            slash.stroke()

    @objc.python_method
    def _buttons(self):
        """The three hover toggles: (key, x, y, r, on, accent). Laid out in an
        arc over the orb, inside the window's transparent margin."""
        bounds = self.bounds()
        cx = bounds.size.width / 2.0
        cy = bounds.size.height / 2.0
        unit = min(cx, cy)
        # below the orb and hugging it, so the caption bubble (anchored
        # beside it) doesn't reach them
        dist, r = unit * 0.70, unit * 0.185
        pilot = getattr(self.controller, "autopilot", None)
        cfg = getattr(self.controller, "cfg", None)
        spec = [
            ("mute", 210.0, bool(getattr(self.controller, "muted", False))),
            ("pilot", 270.0, bool(pilot is not None and pilot.enabled)),
            ("computer", 330.0, bool(getattr(cfg, "computer_use", False))),
        ]
        out = []
        for key, deg, on in spec:
            a = math.radians(deg)
            out.append((key, cx + dist * math.cos(a), cy + dist * math.sin(a), r, on))
        return out

    _SYMBOLS = {
        # (symbol when on, symbol when off)
        "mute": ("speaker.slash.fill", "speaker.wave.2.fill"),
        "pilot": ("steeringwheel", "steeringwheel"),
        "computer": ("cursorarrow.click.2", "cursorarrow.click.2"),
    }

    @objc.python_method
    def _symbol_image(self, name, point_size, bright):
        """SF Symbol rendered white (or dimmed), cached — imageWithSystemSymbol
        allocates on every call and this runs at frame rate."""
        from AppKit import NSColor, NSImage, NSImageSymbolConfiguration

        from AppKit import (
            NSCompositingOperationSourceIn,
            NSCompositingOperationSourceOver,
            NSRectFillUsingOperation,
            NSZeroRect,
        )

        cache = getattr(self, "_symbol_cache", None)
        if cache is None:
            cache = self._symbol_cache = {}
        key = (name, round(point_size), bright)
        if key not in cache:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
            if image is None:
                cache[key] = None
            else:
                conf = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                    point_size, 5)   # semibold
                image = image.imageWithSymbolConfiguration_(conf)
                # hard-tint through the glyph's alpha: the symbol-configuration
                # colour routes don't reliably render in a plain drawRect
                size = image.size()
                tinted = NSImage.alloc().initWithSize_(size)
                tinted.lockFocus()
                image.drawAtPoint_fromRect_operation_fraction_(
                    NSMakePoint(0, 0), NSZeroRect,
                    NSCompositingOperationSourceOver, 1.0)
                NSColor.colorWithWhite_alpha_(1.0, 1.0).set()
                NSRectFillUsingOperation(
                    NSMakeRect(0, 0, size.width, size.height),
                    NSCompositingOperationSourceIn)
                tinted.unlockFocus()
                cache[key] = tinted
        return cache[key]

    @objc.python_method
    def _draw_toggles(self, alpha):
        from AppKit import NSCompositingOperationSourceOver, NSZeroRect

        for key, bx, by, r, on in self._buttons():
            # solid black disc, hairline rim — bright white icon on black;
            # state reads from icon brightness and the rim weight
            _color((0.0, 0.0, 0.0), 0.92 * alpha).set()
            _circle(bx, by, r).fill()
            ring = _circle(bx, by, r)
            ring.setLineWidth_(1.6 if on else 1.0)
            _color((1.0, 1.0, 1.0), (0.90 if on else 0.28) * alpha).set()
            ring.stroke()
            # generous padding: the glyph fills ~55% of the disc
            image = self._symbol_image(self._SYMBOLS[key][0 if on else 1],
                                       r * 0.95, on)
            if image is not None:
                size = image.size()
                image.drawInRect_fromRect_operation_fraction_(
                    NSMakeRect(bx - size.width / 2.0, by - size.height / 2.0,
                               size.width, size.height),
                    NSZeroRect, NSCompositingOperationSourceOver, alpha)
        if self.tip_key is not None and alpha > 0.6:
            self._draw_tip(self.tip_key)

    _TIP_LABELS = {
        "mute": ("Unmute voice", "Mute voice"),
        "pilot": ("Disarm autopilot", "Arm autopilot"),
        "computer": ("Computer use: on", "Computer use: off"),
    }

    @objc.python_method
    def _draw_tip(self, tip_key):
        """Hand-drawn tooltip: a pill just under the orb naming the hovered
        toggle (native tooltips never appear over non-activating panels)."""
        from AppKit import (
            NSFont,
            NSFontAttributeName,
            NSForegroundColorAttributeName,
        )
        from Foundation import NSString

        entry = next((b for b in self._buttons() if b[0] == tip_key), None)
        if entry is None:
            return
        on = entry[4]
        label = self._TIP_LABELS[tip_key][0 if on else 1]
        bounds = self.bounds()
        cx = bounds.size.width / 2.0
        cy = bounds.size.height / 2.0
        unit = min(cx, cy)

        text = NSString.stringWithString_(label)
        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(max(9.0, unit * 0.155)),
            NSForegroundColorAttributeName: _color((1.0, 1.0, 1.0), 0.95),
        }
        size = text.sizeWithAttributes_(attrs)
        pad_x, pad_y = 7.0, 3.0
        width = size.width + 2 * pad_x
        x = min(max(cx - width / 2.0, 2.0), bounds.size.width - width - 2.0)
        y = cy - unit * 0.47 - size.height / 2.0   # between orb edge and buttons
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y - pad_y, width, size.height + 2 * pad_y),
            (size.height + 2 * pad_y) / 2.0, (size.height + 2 * pad_y) / 2.0)
        _color((0.0, 0.0, 0.0), 0.90).set()
        pill.fill()
        text.drawAtPoint_withAttributes_(NSMakePoint(x + pad_x, y), attrs)

    @objc.python_method
    def _hit_button(self, local):
        if self.hover_level < 0.3:
            return None
        for key, bx, by, r, on in self._buttons():
            if (local.x - bx) ** 2 + (local.y - by) ** 2 <= (r * 1.25) ** 2:
                return key
        return None

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
        if self._hit_button(local) is not None:
            return self
        return None

    def mouseDown_(self, event):
        local = self.convertPoint_fromView_(event.locationInWindow(), None)
        key = self._hit_button(local)
        if key is not None and self.controller is not None:
            import threading

            action = {"mute": "toggle_mute", "pilot": "toggle_autopilot",
                      "computer": "toggle_computer_use"}[key]
            threading.Thread(target=getattr(self.controller, action),
                             daemon=True).start()
            return
        window = self.window()
        try:
            window.performWindowDragWithEvent_(event)
        except Exception:
            pass

    def rightMouseDown_(self, event):
        menu = NSMenu.alloc().initWithTitle_("Ollie")

        muted = bool(getattr(self.controller, "muted", False))
        self._add(menu, "Unmute voice" if muted else "Mute voice", "toggleMute:")
        captions = bool(getattr(getattr(self.controller, "cfg", None), "captions", True))
        item = self._add(menu, "Captions", "toggleCaptions:")
        item.setState_(1 if captions else 0)

        computer_use = bool(getattr(getattr(self.controller, "cfg", None),
                                    "computer_use", True))
        item = self._add(menu, "Computer use — let autopilot click", "toggleComputerUse:")
        item.setState_(1 if computer_use else 0)

        pilot = getattr(self.controller, "autopilot", None)
        if pilot is not None:
            if pilot.enabled:
                goal = (pilot.goal[:40] + "…") if len(pilot.goal) > 40 else pilot.goal
                label = (f"Autopilot ON ({pilot.turns}/{pilot.cfg.autopilot_max_turns}"
                         f"{': ' + goal if goal else ', waiting for goal'}) — disarm")
            else:
                label = "Autopilot — arm (then speak the goal)"
            self._add(menu, label, "toggleAutopilot:")
            if not pilot.enabled:
                self._add(menu, "Autopilot — goal from file…", "pickGoalFile:")
        menu.addItem_(NSMenuItem.separatorItem())

        self._submenu(menu, "Watch", self._source_items(), "pickSource:")
        menu.addItem_(NSMenuItem.separatorItem())

        cfg = getattr(self.controller, "cfg", None)
        current = getattr(cfg, "style", "brief")
        style_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Style", None, "")
        style_sub = NSMenu.alloc().initWithTitle_("Style")
        for style, label in (("brief", "Brief — one-line summaries"),
                             ("full", "Full — loss-less retelling"),
                             ("verbatim", "Verbatim — the agent's own words")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, objc.selector(getattr(self, f"setStyle{style.capitalize()}_"),
                                     signature=b"v@:@"), "")
            item.setTarget_(self)
            item.setState_(1 if style == current else 0)
            style_sub.addItem_(item)
        style_parent.setSubmenu_(style_sub)
        menu.addItem_(style_parent)

        engine = getattr(cfg, "tts_engine", "say")
        self._submenu(menu, "Engine", [
            ("macOS say — instant, robotic", "say", engine == "say"),
            ("Kokoro — neural, natural", "kokoro", engine == "kokoro"),
        ], "pickEngine:")
        self._submenu(menu, "Voice", self._voice_items(cfg), "pickVoice:")
        self._submenu(menu, "Tone", self._tone_items(cfg), "pickTone:")

        models = self._model_names(cfg)
        if models:
            models_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Models", None, "")
            models_sub = NSMenu.alloc().initWithTitle_("Models")
            current_filter = getattr(cfg, "ollama_model", "")
            current_pilot = getattr(cfg, "autopilot_model", "") or current_filter
            current_ground = getattr(cfg, "grounding_model", "")
            for title, chosen, action in (
                ("Narration", current_filter, "pickFilterModel:"),
                ("Autopilot", current_pilot, "pickAutopilotModel:"),
                ("Grounding", current_ground, "pickGroundingModel:"),
            ):
                self._submenu(models_sub, title,
                              [(m, m, m == chosen) for m in models], action)
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Check & download missing…",
                objc.selector(self.runDoctor_, signature=b"v@:@"), "")
            item.setTarget_(self)
            models_sub.addItem_(NSMenuItem.separatorItem())
            models_sub.addItem_(item)
            models_parent.setSubmenu_(models_sub)
            menu.addItem_(models_parent)
        else:
            self._add(menu, "Check models & download missing…", "runDoctor:")

        self._permissions_submenu(menu)

        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "History…", "openHistory:")
        self._add(menu, "Settings & dependencies…", "openReport:")
        self._add(menu, "Open log", "openLog:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quit Ollie", "quitOllie:", "q")

        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self)

    @objc.python_method
    def _permissions_submenu(self, menu):
        """One 'Permissions' submenu, every entry carrying its live status.

        The checks are all local and instant (no prompting), so running them
        at menu-open time is fine — and a glance shows what is missing
        instead of four opaque 'settings…' items.
        """
        from .permissions import (
            AUTHORIZED,
            input_monitoring_status,
            microphone_status,
            screen_recording_granted,
        )

        try:
            from ApplicationServices import AXIsProcessTrusted

            ax = bool(AXIsProcessTrusted())
        except Exception:
            ax = False

        def mark(ok, why):
            return "✓" if ok else f"⚠ {why}"

        entries = [
            (f"Accessibility {mark(ax, 'needed to read windows')}…", "openAccess:"),
            (f"Microphone {mark(microphone_status() == AUTHORIZED, 'needed to hear you')}…",
             "openMic:"),
            (f"Input Monitoring {mark(input_monitoring_status() == AUTHORIZED, 'needed for push-to-talk')}…",
             "openInput:"),
            (f"Screen Recording {mark(screen_recording_granted(), 'needed for computer use')}…",
             "openScreen:"),
        ]
        parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Permissions", None, "")
        # surface trouble without having to open the submenu
        broken = sum(1 for label, _ in entries if "⚠" in label)
        if broken:
            parent.setTitle_(f"Permissions — ⚠ {broken} missing")
        sub = NSMenu.alloc().initWithTitle_("Permissions")
        for label, action in entries:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, objc.selector(getattr(self, action.replace(":", "_")),
                                     signature=b"v@:@"), "")
            item.setTarget_(self)
            sub.addItem_(item)
        parent.setSubmenu_(sub)
        menu.addItem_(parent)

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

    def toggleCaptions_(self, sender):
        if self.controller is not None:
            self.controller.toggle_captions()

    def toggleComputerUse_(self, sender):
        if self.controller is not None:
            self.controller.toggle_computer_use()

    def toggleAutopilot_(self, sender):
        if self.controller is not None:
            self.controller.toggle_autopilot()

    def pickGoalFile_(self, sender):
        if self.controller is None:
            return
        from AppKit import NSApplication, NSOpenPanel

        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["md", "markdown", "txt"])
        panel.setMessage_("Choose a markdown file describing the autopilot goal")
        # we are an accessory app: without activating, the panel opens behind
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if panel.runModal() == 1 and panel.URLs():
            path = str(panel.URLs()[0].path())
            import threading

            threading.Thread(
                target=self.controller.arm_autopilot_from_file,
                args=(path,), daemon=True,
            ).start()

    @objc.python_method
    def _voice_items(self, cfg):
        if getattr(cfg, "tts_engine", "say") == "kokoro":
            from .tts import KOKORO_VOICES

            current = getattr(cfg, "kokoro_voice", "")
            return [(v, v, v == current) for v in KOKORO_VOICES]
        current = getattr(cfg, "voice", "")
        cache = getattr(self, "_voices_cache", None)
        if cache:
            voices = cache[1]
        else:                          # first open before any prefetch landed
            from .tts import list_voices

            voices = list_voices()
            import time as _time

            self._voices_cache = (_time.time(), voices)
        names = [name for name, _ in voices][:14]
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
            import time as _time

            cache = getattr(self, "_windows_cache", None)
            if cache and _time.time() - cache[0] < 6.0:
                windows = cache[1]
            else:
                from .readers.window import list_windows

                windows = list_windows()
                self._windows_cache = (_time.time(), windows)
            for win in windows:
                title = win["title"]
                if len(title) > 46:
                    title = title[:46] + "…"
                label = f"{win['app']} — {title}"
                value = f"{win['pid']}:{win['index']}:{label}"
                checked = (win["pid"], win["index"]) == pinned
                items.append((label, value, checked))
        except Exception:
            log.exception("could not list windows")
        if len(items) == 1:
            # An empty window list is an Accessibility problem — but there
            # are two distinct ones, and the checkbox in Settings can lie:
            # the grant is keyed to the app's signature when it was given,
            # so a rebuilt Ollie shows "granted" yet is not trusted.
            try:
                from ApplicationServices import AXIsProcessTrusted

                trusted = bool(AXIsProcessTrusted())
            except Exception:
                trusted = False
            if trusted:
                items.append(("No windows found — odd; see the log",
                              "grant-ax", False))
            else:
                items.append(("Ollie isn't trusted — click to reset the stale "
                              "grant and re-request it…", "grant-ax", False))
        return items

    @objc.python_method
    def _model_names(self, cfg):
        """Installed Ollama models; cached briefly so the menu opens fast."""
        import time as _time

        cache = getattr(self, "_models_cache", None)
        if cache:
            # possibly stale is fine — the hover prefetch refreshes it; never
            # block the click on a 3s HTTP timeout
            return cache[1]
        from .config import Config
        from .filter import list_models

        names = list_models(cfg or Config.load({}))
        self._models_cache = (_time.time(), names)
        return names

    def pickFilterModel_(self, sender):
        if self.controller is not None:
            self.controller.set_filter_model(sender.representedObject())

    def pickGroundingModel_(self, sender):
        if self.controller:
            self.controller.set_grounding_model(sender.representedObject())

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
        if value == "grant-ax":
            self._repair_accessibility()
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

    def openHistory_(self, sender):
        import threading

        from .history import open_page

        threading.Thread(target=open_page, daemon=True).start()

    def openReport_(self, sender):
        import threading

        from .config import Config
        from .settings_report import open_report

        cfg = getattr(self.controller, "cfg", None) or Config.load({})
        threading.Thread(target=open_report, args=(cfg,), daemon=True).start()

    def runDoctor_(self, sender):
        if self.controller is not None:
            import threading

            threading.Thread(
                target=lambda: self.controller.check_models(announce_ok=True),
                daemon=True).start()

    def openAccess_(self, sender):
        from .permissions import open_settings

        try:
            from ApplicationServices import AXIsProcessTrusted

            trusted = bool(AXIsProcessTrusted())
        except Exception:
            trusted = False
        if trusted:
            open_settings("accessibility")
        else:
            # the Settings checkbox may show "on" while the grant is keyed to
            # a signature this build no longer has — reset and re-request
            self._repair_accessibility()

    @objc.python_method
    def _repair_accessibility(self):
        import threading

        from .permissions import repair_accessibility

        def work():
            outcome = repair_accessibility()
            show = getattr(self.controller, "_show_error", None)
            if show is not None:
                show(outcome)

        threading.Thread(target=work, daemon=True).start()

    def openMic_(self, sender):
        from .permissions import open_settings

        open_settings("microphone")

    def openInput_(self, sender):
        from .permissions import open_settings

        open_settings("input_monitoring")

    def openScreen_(self, sender):
        # raise the system prompt if never asked, then open the pane so the
        # user can flip the switch for Ollie
        try:
            from Quartz import CGRequestScreenCaptureAccess

            CGRequestScreenCaptureAccess()
        except Exception:
            pass
        from .permissions import open_settings

        open_settings("screen_recording")

    def openLog_(self, sender):
        import subprocess

        from .config import LOG_PATH

        subprocess.Popen(["open", "-t", str(LOG_PATH)])

    def quitOllie_(self, sender):
        self.app_state.stop()
        NSApplication.sharedApplication().terminate_(None)


class _BubbleView(NSView):
    """Rounded dark card behind the caption text."""

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(rect)
        card = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), 13.0, 13.0
        )
        # glassy near-black with a violet cast, matching the speaking orb
        gradient = NSGradient.alloc().initWithStartingColor_endingColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.16, 0.13, 0.24, 0.96),
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.07, 0.13, 0.96),
        )
        gradient.drawInBezierPath_angle_(card, -90.0)
        card.setLineWidth_(1.2)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.70, 0.48, 0.98, 0.55).set()
        card.stroke()


class CaptionBubble:
    """A chat bubble beside the orb showing the line being spoken.

    Managed from the orb's frame timer: appears when speaking starts (and
    captions are enabled), lingers briefly, then fades away.
    """

    WIDTH = 300
    PAD = 12
    LINGER = 1.2      # seconds the bubble outlives the speech

    def __init__(self, orb_window) -> None:
        from AppKit import NSFont, NSTextField

        self.orb_window = orb_window
        self._last_label = ""
        self._hide_at = 0.0

        self.panel = OrbWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, 60),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.panel.setIgnoresMouseEvents_(True)

        view = _BubbleView.alloc().initWithFrame_(NSMakeRect(0, 0, self.WIDTH, 60))
        self.field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(self.PAD, self.PAD, self.WIDTH - 2 * self.PAD, 36)
        )
        self.field.setEditable_(False)
        self.field.setSelectable_(False)
        self.field.setBezeled_(False)
        self.field.setDrawsBackground_(False)
        self.field.setFont_(NSFont.systemFontOfSize_(13.0))
        self.field.setTextColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.92, 0.93, 0.97, 1.0))
        view.addSubview_(self.field)
        self.panel.setContentView_(view)

    def update(self, state, enabled: bool) -> None:
        # spoken lines and labelled thinking (autopilot narrating its own
        # decision process) both belong in the bubble
        speaking = (state.state in (State.SPEAKING, State.THINKING)
                    and bool(state.label))
        if not enabled:
            self._last_label = ""
            self.panel.orderOut_(None)
            return
        if speaking:
            self._hide_at = time.time() + self.LINGER
            if state.label != self._last_label:
                self._last_label = state.label
                self._layout(state.label)
            else:
                self._position()      # follow the orb while it is dragged
            self.panel.orderFrontRegardless()
        elif time.time() > self._hide_at:
            self._last_label = ""
            self.panel.orderOut_(None)
        elif self._last_label:
            self._position()          # keep following during the linger too

    def _layout(self, text: str) -> None:
        text = text if len(text) <= 360 else text[:360] + "…"
        self.field.setStringValue_(text)
        inner = self.WIDTH - 2 * self.PAD
        size = self.field.cell().cellSizeForBounds_(NSMakeRect(0, 0, inner, 800))
        self._height = min(200.0, size.height) + 2 * self.PAD
        self.field.setFrame_(NSMakeRect(self.PAD, self.PAD, inner, min(200.0, size.height)))
        self._position()

    def _position(self) -> None:
        height = getattr(self, "_height", 60.0)
        orb = self.orb_window.frame()
        screen = self.orb_window.screen()
        # The visible circle is much smaller than the orb window (radius
        # ~0.40 of half the window, plus halo) — position against the circle,
        # not the window edge, or the bubble floats disconnected in space.
        cx = orb.origin.x + orb.size.width / 2.0
        edge = orb.size.width / 2.0 * 0.52          # visible radius + breath
        x = cx - edge - self.WIDTH - 4
        if screen is not None and x < screen.visibleFrame().origin.x + 8:
            x = cx + edge + 4                        # flip to the right side
        y = orb.origin.y + (orb.size.height - height) / 2.0
        self.panel.setFrame_display_(NSMakeRect(x, y, self.WIDTH, height), True)


class _BorderView(NSView):
    """Glowing rounded border, drawn just inside the panel bounds."""

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(rect)
        bounds = self.bounds()
        pulse = 0.72 + 0.28 * math.sin(time.time() * 2.4)
        # widening, fading strokes make the glow; green = "this one is shared"
        for width, alpha in ((10.0, 0.10), (6.5, 0.20), (3.5, 0.45), (1.8, 0.95)):
            inset = width / 2.0 + 1.0
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(bounds.origin.x + inset, bounds.origin.y + inset,
                           bounds.size.width - 2 * inset,
                           bounds.size.height - 2 * inset),
                10.0, 10.0,
            )
            path.setLineWidth_(width)
            _color((0.30, 0.86, 0.58), alpha * pulse).set()
            path.stroke()


class PinBorder:
    """Follows the pinned window with a glowing border, like the highlight a
    video-call app draws around the window being shared."""

    def __init__(self) -> None:
        self.panel = OrbWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 100, 100),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(False)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.panel.setIgnoresMouseEvents_(True)
        self.view = _BorderView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100))
        self.panel.setContentView_(self.view)
        self._visible = False
        self._pinned = None
        self._shown_at = 0.0

    SHOW_FOR = 3.0    # the glow confirms the pick, then gets out of the way

    def update(self, reader) -> None:
        frame = None
        if getattr(reader, "name", "") == "window":
            identity = (reader.pid, reader.window_index)
            if identity != self._pinned:
                self._pinned = identity
                self._shown_at = time.time()
            if time.time() - self._shown_at < self.SHOW_FOR:
                frame = reader.frame_on_screen()
        else:
            self._pinned = None
        if frame is None:
            if self._visible:
                self.panel.orderOut_(None)
                self._visible = False
            return
        x, top_y, w, h = frame
        # AX coordinates have a top-left origin on the primary display;
        # Cocoa windows use bottom-left. Convert via the primary screen.
        screens = NSScreen.screens()
        primary_h = screens[0].frame().size.height if screens else 900.0
        pad = 6.0
        cocoa = NSMakeRect(x - pad, primary_h - (top_y + h) - pad,
                           w + 2 * pad, h + 2 * pad)
        self.panel.setFrame_display_(cocoa, True)
        self.view.setNeedsDisplay_(True)
        if not self._visible:
            self.panel.orderFrontRegardless()
            self._visible = True


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
    window.setAcceptsMouseMovedEvents_(True)

    view = OrbView.alloc().initWithFrame_state_controller_(
        NSMakeRect(0, 0, size, size), state, controller
    )
    window.setContentView_(view)
    window.orderFrontRegardless()

    bubble = CaptionBubble(window)
    border = PinBorder()
    tick = {"n": 0}

    def frame(_timer):
        view.setNeedsDisplay_(True)
        try:
            enabled = bool(getattr(getattr(controller, "cfg", None), "captions", True))
            bubble.update(state, enabled)
            # the bubble raises itself every frame while visible; while the
            # hover toggles are out, keep the orb above it so they stay usable
            if getattr(view, "hover_level", 0.0) > 0.05:
                window.orderFrontRegardless()
            tick["n"] += 1
            if tick["n"] % 3 == 0:      # AX frame reads at ~10 Hz is plenty
                border.update(getattr(controller, "reader", None))
        except Exception:
            log.exception("caption bubble update failed")

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.0 / FPS, True, frame)

    def supervise(_timer):
        if not state.running:
            app.terminate_(None)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.25, True, supervise)

    log.info("orb running at %.0f,%.0f", x, y)
    app.run()
