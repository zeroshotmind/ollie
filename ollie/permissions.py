"""macOS permission checks.

Both permissions Ollie needs fail *silently* when denied, which is why they are
worth checking explicitly rather than discovering at use time:

* Microphone — CoreAudio still opens the stream and hands back a buffer of
  zeros. Nothing raises; you just transcribe silence forever.
* Accessibility — key events never arrive and synthesised keystrokes go
  nowhere, again with no error.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("ollie.permissions")

NOT_DETERMINED, RESTRICTED, DENIED, AUTHORIZED, UNKNOWN = (
    "not-determined", "restricted", "denied", "authorized", "unknown",
)

_STATUS = {0: NOT_DETERMINED, 1: RESTRICTED, 2: DENIED, 3: AUTHORIZED}

SETTINGS_PANES = {
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input_monitoring": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}


def screen_recording_granted() -> bool:
    """Whether this process may capture the screen (computer-use screenshots).

    Quartz answers without prompting; CGRequestScreenCaptureAccess is the
    call that raises the system dialog.
    """
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return False

OLLIE_BUNDLE_ID = "com.swastikroy.ollie"


def reset_accessibility(bundle_id: str = OLLIE_BUNDLE_ID) -> bool:
    """Delete Ollie's Accessibility record from the TCC database.

    Needed because the grant is keyed to the app's code signature at the
    moment it was given. Ollie is ad-hoc signed, so any rebuild produces a
    new signature: Settings still shows the checkbox on, but the grant is
    dead and AXIsProcessTrusted() says False. There is no API to re-key it —
    the only fix is to drop the stale row and let macOS ask again.
    """
    try:
        proc = subprocess.run(
            ["tccutil", "reset", "Accessibility", bundle_id],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            log.warning("tccutil reset failed: %s", proc.stderr.strip()[:200])
        return proc.returncode == 0
    except Exception as exc:
        log.warning("could not reset the Accessibility grant: %s", exc)
        return False


def request_accessibility() -> bool:
    """Raise the system Accessibility prompt for this process.

    Unlike merely opening the Settings pane, this makes macOS register the
    *currently running* binary in the Accessibility list, so the row the user
    enables actually matches the signature being checked.
    """
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
    except Exception as exc:
        log.debug("cannot raise the Accessibility prompt: %s", exc)
        return False


def repair_accessibility() -> None:
    """Full recovery from a stale grant: reset, re-prompt, open the pane.

    The prompt only appears when no record exists, so the reset must come
    first; the pane is opened regardless so the user can confirm the toggle.
    """
    reset_accessibility()
    request_accessibility()
    open_settings("accessibility")


_IOHID_LISTEN = 1          # kIOHIDRequestTypeListenEvent


def _iokit():
    import ctypes

    lib = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    lib.IOHIDCheckAccess.restype = ctypes.c_int
    lib.IOHIDCheckAccess.argtypes = [ctypes.c_int]
    lib.IOHIDRequestAccess.restype = ctypes.c_bool
    lib.IOHIDRequestAccess.argtypes = [ctypes.c_int]
    return lib


def input_monitoring_status() -> str:
    """Whether this process may observe keyboard events (kTCCServiceListenEvent).

    A listen-only event tap needs this (Accessibility also satisfies it).
    Like the others it fails silently: the tap just cannot be created.
    """
    try:
        code = _iokit().IOHIDCheckAccess(_IOHID_LISTEN)
    except Exception as exc:
        log.debug("cannot read input-monitoring authorisation: %s", exc)
        return UNKNOWN
    return {0: AUTHORIZED, 1: DENIED, 2: NOT_DETERMINED}.get(code, UNKNOWN)


def request_input_monitoring() -> str:
    status = input_monitoring_status()
    if status != NOT_DETERMINED:
        return status
    try:
        _iokit().IOHIDRequestAccess(_IOHID_LISTEN)
    except Exception as exc:
        log.debug("cannot raise the input-monitoring prompt: %s", exc)
    return input_monitoring_status()


def microphone_status() -> str:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        return _STATUS.get(
            AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio), UNKNOWN
        )
    except Exception as exc:
        log.debug("cannot read microphone authorisation: %s", exc)
        return UNKNOWN


def request_microphone() -> str:
    """Raise the microphone prompt if macOS has not asked yet."""
    status = microphone_status()
    if status != NOT_DETERMINED:
        return status
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, lambda granted: None
        )
    except Exception as exc:
        log.debug("cannot raise the microphone prompt: %s", exc)
    return microphone_status()


def open_settings(pane: str) -> None:
    url = SETTINGS_PANES.get(pane)
    if not url:
        return
    try:
        subprocess.Popen(["open", url])
    except Exception:
        pass
