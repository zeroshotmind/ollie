"""Render the orb's four states offscreen into an animated GIF (no GUI session needed).

Each state plays its real behaviour: idle breathes on its own clock, listening
and speaking swell with the audio level, thinking sweeps the rim. The loop is
the one shown in the README (docs/orb.gif).
"""
import math
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
OUT = str(pathlib.Path(__file__).resolve().parents[1] / "docs" / "orb.gif")

from AppKit import (NSApplication, NSApplicationActivationPolicyProhibited, NSBitmapImageRep,
                    NSColor, NSImage, NSPNGFileType, NSBackingStoreBuffered, NSWindow,
                    NSWindowStyleMaskBorderless, NSBezierPath,
                    NSFont, NSAttributedString, NSForegroundColorAttributeName,
                    NSFontAttributeName)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize, NSURL
import Quartz

from ollie.orb import OrbView
from ollie.state import AppState, State

app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)

SIZE = 150
LABEL_H = 26

# Match the animation constants in ollie/orb.py so the GIF is honest:
IDLE_PERIOD = 2 * math.pi / 1.1      # idle breathing, sin(elapsed * 1.1)
SWEEP_PERIOD = 360.0 / 110.0         # thinking rim sweep, 110 deg/second

# (state, elapsed, amplitude, per-frame delay) — a full loop in ~4 seconds
SEGMENTS = (
    # idle: one full breath, slowed ~4x so the loop stays short
    [(State.IDLE, k * IDLE_PERIOD / 8, 0.0, 0.2) for k in range(8)],
    # listening: the mic peaks and settles
    [(State.LISTENING, 0.0, a, 0.15) for a in (0.35, 0.85, 0.50, 0.95)],
    # thinking: one full sweep around the rim
    [(State.THINKING, k * SWEEP_PERIOD / 8, 0.0, 0.18) for k in range(8)],
    # speaking: the outgoing audio driving the radius
    [(State.SPEAKING, 0.0, a, 0.15) for a in (0.60, 0.30, 0.85, 0.45)],
)


def render_frame(state: State, elapsed: float, amplitude: float) -> NSBitmapImageRep:
    st = AppState()
    st.set(state, "")
    st.set_amplitude(amplitude)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, SIZE, SIZE), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    view = OrbView.alloc().initWithFrame_state_(NSMakeRect(0, 0, SIZE, SIZE), st)
    window.setContentView_(view)
    # Fake the view's clock to the chosen point in the animation, then settle
    # the smoothed level so listening/speaking show their real radius.
    view.born = time.time() - elapsed
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    for _ in range(12):
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)

    canvas = NSImage.alloc().initWithSize_(NSMakeSize(SIZE, SIZE + LABEL_H))
    canvas.lockFocus()
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.06, 0.07, 0.09, 1.0).set()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, SIZE, SIZE + LABEL_H))

    image = NSImage.alloc().initWithSize_(NSMakeSize(SIZE, SIZE))
    image.addRepresentation_(rep)
    image.drawAtPoint_fromRect_operation_fraction_(
        NSMakePoint(0, LABEL_H), NSMakeRect(0, 0, SIZE, SIZE), 2, 1.0)

    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(12),
             NSForegroundColorAttributeName: NSColor.whiteColor()}
    label = NSAttributedString.alloc().initWithString_attributes_(state.value, attrs)
    label.drawAtPoint_(NSMakePoint(12, 7))
    canvas.unlockFocus()

    # ImageIO's GIF writer only takes 8-bit images; the canvas renders as
    # 16-bit float, so bounce through a PNG.
    png = NSBitmapImageRep.imageRepWithData_(canvas.TIFFRepresentation())
    png = png.representationUsingType_properties_(NSPNGFileType, {})
    return NSBitmapImageRep.imageRepWithData_(png)


frames = [f for segment in SEGMENTS for f in segment]

url = NSURL.fileURLWithPath_(OUT)
dest = Quartz.CGImageDestinationCreateWithURL(url, "com.compuserve.gif", len(frames), None)
Quartz.CGImageDestinationSetProperties(
    dest, {Quartz.kCGImagePropertyGIFDictionary: {Quartz.kCGImagePropertyGIFLoopCount: 0}})
for state, elapsed, amplitude, delay in frames:
    rep = render_frame(state, elapsed, amplitude)
    Quartz.CGImageDestinationAddImage(
        dest, rep.CGImage(),
        {Quartz.kCGImagePropertyGIFDictionary: {Quartz.kCGImagePropertyGIFDelayTime: delay}})
assert Quartz.CGImageDestinationFinalize(dest), "GIF finalisation failed"
print("wrote", OUT)
