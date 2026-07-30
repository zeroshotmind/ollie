"""Render each orb state offscreen to a PNG contact sheet (no GUI session needed)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
OUT = str(pathlib.Path(__file__).resolve().parents[1] / "docs" / "orb-states.png")

from AppKit import (NSApplication, NSApplicationActivationPolicyProhibited, NSBitmapImageRep,
                    NSColor, NSImage, NSPNGFileType, NSBackingStoreBuffered, NSWindow,
                    NSWindowStyleMaskBorderless, NSGraphicsContext, NSBezierPath,
                    NSFont, NSAttributedString, NSForegroundColorAttributeName,
                    NSFontAttributeName)
from Foundation import NSMakeRect, NSMakePoint, NSMakeSize

from ollie.orb import OrbView
from ollie.state import AppState, State

app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)

SIZE = 150
STATES = [(State.IDLE, 0.0), (State.LISTENING, 0.75), (State.THINKING, 0.0), (State.SPEAKING, 0.55)]

sheet = NSImage.alloc().initWithSize_(NSMakeSize(SIZE * len(STATES), SIZE + 26))
sheet.lockFocus()
NSColor.colorWithCalibratedRed_green_blue_alpha_(0.06, 0.07, 0.09, 1.0).set()
NSBezierPath.fillRect_(NSMakeRect(0, 0, SIZE * len(STATES), SIZE + 26))

for index, (state, amplitude) in enumerate(STATES):
    st = AppState()
    st.set(state)
    st.set_amplitude(amplitude)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, SIZE, SIZE), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    view = OrbView.alloc().initWithFrame_state_(NSMakeRect(0, 0, SIZE, SIZE), st)
    window.setContentView_(view)
    # Settle the smoothed level so listening/speaking show their real radius.
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    for _ in range(12):
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)

    image = NSImage.alloc().initWithSize_(NSMakeSize(SIZE, SIZE))
    image.addRepresentation_(rep)
    image.drawAtPoint_fromRect_operation_fraction_(
        NSMakePoint(index * SIZE, 26), NSMakeRect(0, 0, SIZE, SIZE), 2, 1.0)

    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(12),
             NSForegroundColorAttributeName: NSColor.whiteColor()}
    label = NSAttributedString.alloc().initWithString_attributes_(state.value, attrs)
    label.drawAtPoint_(NSMakePoint(index * SIZE + 12, 7))

sheet.unlockFocus()

tiff = sheet.TIFFRepresentation()
png = NSBitmapImageRep.imageRepWithData_(tiff).representationUsingType_properties_(NSPNGFileType, {})
png.writeToFile_atomically_(OUT, True)
print("wrote", OUT)
