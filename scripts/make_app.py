#!/usr/bin/env python3
"""Build Ollie.app — a real macOS bundle that owns its own permissions.

Why this exists: macOS grants Accessibility and Microphone access to an
*application*, not to a script. Run `python -m ollie` from a shell and the
grant attaches to your terminal, which means every script that terminal ever
runs inherits the ability to read your keystrokes and drive your machine.
Inside a bundle the grant attaches to Ollie alone, and macOS shows the dialog
with Ollie's name on it.

The bundle is a thin wrapper: its launcher execs this project's virtualenv, so
the app stays a few hundred kilobytes and code changes take effect on the next
launch with no rebuild. The tradeoff is that the app depends on this checkout
staying where it is.

    python scripts/make_app.py             build into ~/Applications
    python scripts/make_app.py --run       build, then launch it
    python scripts/make_app.py --dest DIR  build somewhere else
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

BUNDLE_ID = "com.swastikroy.ollie"
VERSION = "0.1.0"
ICON_SIZES = [16, 32, 128, 256, 512]

LAUNCHER_SRC = Path(__file__).resolve().parent / "launcher" / "main.c"
ICON_CACHE = Path(__file__).resolve().parents[1] / "docs" / "Ollie.icns"

FALLBACK_LAUNCHER = """#!/bin/bash
# Fallback launcher, used only when the C launcher cannot be compiled.
# NOTE: this execs the interpreter, so macOS attributes permissions to
# "python", not to Ollie. Install the Xcode command line tools and rebuild.
set -uo pipefail
PROJECT="{project}"
mkdir -p "$HOME/.ollie"
cd "$PROJECT" || exit 1
exec "$PROJECT/.venv/bin/python" -u -m ollie >> "$HOME/.ollie/app.log" 2>&1
"""


def venv_layout() -> dict:
    """Where the interpreter, its stdlib and the project packages actually live."""
    venv_python = (PROJECT / ".venv" / "bin" / "python").resolve()
    if not venv_python.exists():
        raise SystemExit(f"no virtualenv at {PROJECT / '.venv'} — create it first")

    base_prefix = venv_python.parents[1]
    site = sorted((PROJECT / ".venv" / "lib").glob("python3.*/site-packages"))
    if not site:
        raise SystemExit("virtualenv has no site-packages")

    version = site[0].parent.name                    # e.g. python3.12
    libdir = base_prefix / "lib"
    dylib = libdir / f"lib{version}.dylib"
    return {
        "base_prefix": base_prefix,
        "libdir": libdir,
        "dylib": dylib,
        "version": version,
        "site": site[0],
    }


def compile_launcher(target: Path, layout: dict) -> bool:
    """Build the native launcher. Returns False if no compiler is available."""
    if shutil.which("clang") is None or not layout["dylib"].exists():
        return False

    command = [
        "clang", "-arch", "arm64", "-O2", "-Wall",
        "-o", str(target), str(LAUNCHER_SRC),
        f"-DOLLIE_PROJECT=\"{PROJECT}\"",
        f"-DOLLIE_PYTHONHOME=\"{layout['base_prefix']}\"",
        f"-DOLLIE_SITE=\"{layout['site']}\"",
        f"-DOLLIE_PYTHON=\"{PROJECT / '.venv' / 'bin' / 'python'}\"",
        f"-L{layout['libdir']}", f"-l{layout['version']}",
        "-Wl,-rpath," + str(layout["libdir"]),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("  clang failed:\n" + result.stderr.strip()[:800])
        return False
    return True


def render_icon(iconset: Path) -> None:
    """Draw the orb once at 1024 and downsample into a full iconset."""
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyProhibited,
        NSBackingStoreBuffered,
        NSBitmapImageRep,
        NSColor,
        NSImage,
        NSPNGFileType,
        NSWindow,
        NSWindowStyleMaskBorderless,
    )
    from Foundation import NSMakePoint, NSMakeRect, NSMakeSize

    from ollie.orb import OrbView
    from ollie.state import AppState, State

    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyProhibited
    )

    canvas, inner = 1024, 880
    state = AppState()
    state.set(State.SPEAKING)
    state.set_amplitude(0.30)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, inner, inner), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    view = OrbView.alloc().initWithFrame_state_(NSMakeRect(0, 0, inner, inner), state)
    window.setContentView_(view)

    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    for _ in range(12):                      # let the smoothed level settle
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)

    orb = NSImage.alloc().initWithSize_(NSMakeSize(inner, inner))
    orb.addRepresentation_(rep)

    sheet = NSImage.alloc().initWithSize_(NSMakeSize(canvas, canvas))
    sheet.lockFocus()
    offset = (canvas - inner) / 2.0
    orb.drawAtPoint_fromRect_operation_fraction_(
        NSMakePoint(offset, offset), NSMakeRect(0, 0, inner, inner), 2, 1.0
    )
    sheet.unlockFocus()

    png = NSBitmapImageRep.imageRepWithData_(
        sheet.TIFFRepresentation()
    ).representationUsingType_properties_(NSPNGFileType, {})

    iconset.mkdir(parents=True, exist_ok=True)
    master = iconset / "master.png"
    png.writeToFile_atomically_(str(master), True)

    for size in ICON_SIZES:
        for scale in (1, 2):
            pixels = size * scale
            suffix = "@2x" if scale == 2 else ""
            out = iconset / f"icon_{size}x{size}{suffix}.png"
            subprocess.run(
                ["sips", "-z", str(pixels), str(pixels), str(master), "--out", str(out)],
                check=True, capture_output=True,
            )
    master.unlink()


def bundle_info() -> dict:
    return {
        "CFBundleName": "Ollie",
        "CFBundleDisplayName": "Ollie",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "Ollie",
        "CFBundleIconFile": "Ollie",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "13.0",
        # No Dock icon and no menu bar — Ollie is just the floating orb.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "Ollie listens while you hold the push-to-talk key so it can type "
            "what you say into your terminal.",
        "NSAppleEventsUsageDescription":
            "Ollie opens System Settings for you when it needs a permission.",
    }


def build(dest: Path, force: bool = False) -> Path:
    """Rebuild the bundle, but only touch it if something actually changed.

    macOS keys the Accessibility grant to the bundle's code signature. Re-signing
    an unchanged app still produces a new seal and silently revokes the grant, so
    a rebuild that changes nothing must be a genuine no-op.
    """
    app = dest / "Ollie.app"
    layout = venv_layout()
    info = bundle_info()
    manifest = build_manifest(info, layout)

    if not force and app.exists() and installed_manifest(app) == manifest:
        print("  unchanged — leaving the installed app alone so its")
        print("  Accessibility grant survives. (--force to rebuild anyway)")
        return app

    staging = dest / ".Ollie.app.build"
    if staging.exists():
        shutil.rmtree(staging)

    macos = staging / "Contents" / "MacOS"
    resources = staging / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # The icon is drawn by ollie/orb.py, but re-rendering it on every build
    # would make any tweak to the orb revoke the user's permissions. So it is
    # cached, and only redrawn when it is missing or --force is given.
    icns = resources / "Ollie.icns"
    cached = ICON_CACHE
    if cached.exists() and not force:
        shutil.copy2(cached, icns)
        print("  icon: reusing cached render")
    else:
        print("  rendering icon…")
        iconset = resources / "Ollie.iconset"
        render_icon(iconset)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        shutil.rmtree(iconset)
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icns, cached)

    with (staging / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))

    launcher = macos / "Ollie"
    print("  compiling launcher…")
    native = compile_launcher(launcher, layout)
    if native:
        print(f"    linked against {layout['dylib'].name}")
    else:
        print("    !! falling back to a shell launcher — macOS will name the")
        print("       permission 'python' instead of 'Ollie'.")
        launcher.write_text(FALLBACK_LAUNCHER.format(project=PROJECT))
    launcher.chmod(0o755)

    # Ad-hoc signature. Without one, macOS keys the permission grant to a hash
    # of the bundle that changes on every rebuild, so the grant keeps evaporating.
    print("  signing (ad-hoc)…")
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-",
         "--identifier", BUNDLE_ID, str(staging)],
        check=True, capture_output=True,
    )
    subprocess.run(["codesign", "--verify", "--verbose=1", str(staging)],
                   check=True, capture_output=True)

    if app.exists():
        shutil.rmtree(app)
    staging.rename(app)
    print("  !! the signature changed, so macOS has dropped any previous grant.")
    print("     Re-grant Accessibility to Ollie and relaunch it once.")
    return app


MANIFEST = "Contents/Resources/build-manifest.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(info: dict, layout: dict) -> dict:
    """Everything the bundle is derived from.

    Comparing outputs does not work: the linker stamps a fresh LC_UUID into the
    binary on every build, so two identical builds never produce identical
    bytes. Comparing inputs does.
    """
    return {
        "bundle_id": BUNDLE_ID,
        "version": VERSION,
        "launcher_source": _digest(LAUNCHER_SRC),
        "project": str(PROJECT),
        "python_home": str(layout["base_prefix"]),
        "site": str(layout["site"]),
        "info_plist": info,
    }


def installed_manifest(app: Path) -> dict | None:
    path = app / MANIFEST
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Ollie.app")
    parser.add_argument("--dest", default="/Applications",
                        help="where to put the bundle (default: /Applications, so it "
                             "shows up in the permission pickers)")
    parser.add_argument("--run", action="store_true", help="launch it after building")
    parser.add_argument("--force", action="store_true",
                        help="re-sign even if nothing changed (revokes the grant)")
    args = parser.parse_args()

    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    print(f"Building Ollie.app -> {dest}")
    app = build(dest, force=args.force)
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"  built {app}  ({size / 1024:.0f} KB)\n")

    print("Editing the Python needs no rebuild — the bundle points at this")
    print("checkout, so changes apply on the next launch and the grant survives.\n")

    print("First launch:")
    print(f"  open '{app}'")
    print("  macOS will ask for Microphone, then Accessibility — both dialogs")
    print("  now say 'Ollie' rather than the name of your terminal.")
    print("  After granting Accessibility, quit and relaunch Ollie once.\n")
    print("The orb appears bottom-right. Right-click it to mute or quit.")
    print(f"Logs: ~/.ollie/app.log\n")
    print("Note: the bundle execs this checkout's virtualenv, so code edits take")
    print(f"effect on the next launch. Moving {PROJECT} means rebuilding the app.")

    if args.run:
        subprocess.run(["open", str(app)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
