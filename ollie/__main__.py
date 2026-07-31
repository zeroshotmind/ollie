"""Entry point.

    python -m ollie                 run the narrator with the orb
    python -m ollie --doctor        check every dependency and permission
    python -m ollie --list-sessions show discoverable Claude Code transcripts
    python -m ollie --no-orb        headless (useful for logs / debugging)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from .config import LOG_PATH, STATE_DIR, Config
from .core import Narrator
from .readers.claude_code import ClaudeCodeReader


def _fix_multiprocessing() -> None:
    """Point child processes at the real interpreter, not the app bundle.

    Inside Ollie.app sys.executable is Contents/MacOS/Ollie. Anything that
    spawns a helper through multiprocessing would otherwise launch a second
    copy of Ollie.
    """
    python = os.environ.get("OLLIE_PYTHON")
    if not python or not os.path.exists(python):
        return
    try:
        import multiprocessing

        multiprocessing.set_executable(python)
    except Exception:
        pass


# Kept alive for the life of the process: see _setup_logging.
_CONSOLE = None


def _setup_logging(verbose: bool) -> None:
    """Configure logging to a private duplicate of stderr.

    PortAudio's CoreAudio backend points fd 2 at /dev/null while it initialises,
    to hide its own chatter, and restores it afterwards. Anything logged during
    that window is lost — which silently ate the startup lines. Duplicating the
    descriptor first gives logging a handle PortAudio cannot reach.
    """
    global _CONSOLE

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"

    try:
        _CONSOLE = os.fdopen(os.dup(sys.stderr.fileno()), "w", buffering=1)
        console = logging.StreamHandler(_CONSOLE)
    except Exception:
        console = logging.StreamHandler(sys.stderr)

    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S",
                        handlers=[console, logging.FileHandler(LOG_PATH)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="ollie", description="Voice narrator for coding agents")
    p.add_argument("--session", dest="session_file", help="pin to one transcript path")
    p.add_argument("--from-start", dest="from_start", action="store_true", default=None,
                   help="replay the whole transcript instead of joining at the tail")
    p.add_argument("--no-orb", dest="orb", action="store_false", default=None,
                   help="run without the floating orb window")
    p.add_argument("--engine", dest="tts_engine", choices=["say", "kokoro"],
                   help="TTS engine: say (instant, zero setup) or kokoro (neural)")
    p.add_argument("--voice", help="macOS voice name (say -v '?' to list)")
    p.add_argument("--rate", type=int, help="words per minute")
    p.add_argument("--model", dest="ollama_model", help="Ollama model for the filter")
    p.add_argument("--autopilot-model", dest="autopilot_model",
                   help="Ollama model that authors autopilot turns "
                        "(default: the filter model; a bigger one drives better)")
    p.add_argument("--autopilot", action="store_true", default=None,
                   help="arm autopilot at startup (give the goal with --goal)")
    p.add_argument("--goal", dest="autopilot_goal",
                   help="the goal autopilot should drive the agent toward")
    p.add_argument("--tone", choices=["neutral", "warm", "snarky", "minimal"],
                   help="delivery tone for brief/full narration")
    p.add_argument("--list-voices", action="store_true",
                   help="list installed narration voices and exit")
    p.add_argument("--style", choices=["brief", "full", "verbatim"],
                   help="narration style: brief (terse), full (loss-less), "
                        "verbatim (the agent's own words)")
    p.add_argument("--hotkey",
                   help="push-to-talk key, e.g. 'right option', 'caps lock', f13")
    p.add_argument("--hotkey-mode", dest="hotkey_mode", choices=["hold", "toggle"])
    p.add_argument("--tool-results", dest="speak_tool_results", action="store_true", default=None,
                   help="also narrate tool output (noisy)")
    p.add_argument("--no-tools", dest="speak_tool_use", action="store_false", default=None,
                   help="narrate only prose, never tool calls")
    p.add_argument("--press-enter", dest="press_enter", action="store_true", default=None,
                   help="submit immediately after injecting speech")
    p.add_argument("-v", "--verbose", action="store_true", default=None)
    p.add_argument("--doctor", action="store_true", help="run preflight checks and exit")
    p.add_argument("--settings", action="store_true",
                   help="show what Ollie depends on (models, APIs, permissions) and exit")
    p.add_argument("--list-sessions", action="store_true", help="list transcripts and exit")
    p.add_argument("--history", nargs="?", const=50, type=int, metavar="N",
                   help="show the last N trajectory events (default 50) and exit")
    p.add_argument("--say", help="speak a phrase and exit (TTS smoke test)")
    p.add_argument("--test-hotkey", action="store_true",
                   help="print every key you press, to find a working push-to-talk key")
    return p.parse_args(argv)


def _test_hotkey(cfg: Config) -> int:
    """Print each key as it arrives. Answers 'is the key even reaching us?'"""
    from .hotkey import normalise, pretty, watch_keys
    from .permissions import AUTHORIZED, input_monitoring_status

    status = input_monitoring_status()
    if status != AUTHORIZED:
        print(f"Input Monitoring for this process is '{status}' — keys may not arrive.\n")

    print(f"Push-to-talk is set to: {pretty(cfg.hotkey)}  ({normalise(cfg.hotkey)})")
    print("Press keys now. Ctrl-C to stop.\n")

    tap = watch_keys(cfg.hotkey, lambda line: print(line, flush=True))
    if tap is None:
        print("Could not create the event tap — grant Input Monitoring (or")
        print("Accessibility) to this process and try again.")
        return 1
    try:
        while True:
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        tap.stop()
    print("\nIf nothing appeared, the keys never reached this process.")
    return 0


def _doctor(cfg: Config) -> int:
    from .filter import OllamaFilter
    from .injector import accessibility_trusted

    problems = 0

    def report(ok, good, bad):
        nonlocal problems
        print(f"  {'✓' if ok else '✗'}  {good if ok else bad}")
        if not ok:
            problems += 1

    print("Ollie preflight\n")

    reader = ClaudeCodeReader(cfg)
    session = reader.pick_session()
    report(session is not None,
           f"Claude Code transcript found: {session}",
           f"no transcripts under {cfg.claude_projects_dir} — start a Claude Code session first")

    ollama = OllamaFilter(cfg)
    ok, message = ollama.health()
    report(ok, message, message)
    ollama.close()

    try:
        import mlx_whisper  # noqa: F401
        report(True, "mlx-whisper importable", "")
    except Exception as exc:
        report(False, "", f"mlx-whisper not importable: {exc}")

    try:
        import sounddevice as sd
        inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        report(bool(inputs), f"input device found ({inputs[0]['name'] if inputs else '-'})",
               "no input device found")
    except Exception as exc:
        report(False, "", f"sounddevice failed: {exc}")

    from .permissions import AUTHORIZED, input_monitoring_status, microphone_status
    listen = input_monitoring_status()
    report(listen == AUTHORIZED,
           "Input Monitoring permission granted (hotkey events can arrive)",
           f"Input Monitoring is '{listen}' — the push-to-talk key will never be seen.\n"
           "     System Settings -> Privacy & Security -> Input Monitoring -> enable Ollie")

    mic = microphone_status()
    report(mic == AUTHORIZED,
           "Microphone permission granted",
           f"Microphone permission is '{mic}' — recording returns pure silence, with\n"
           "     no error. System Settings -> Privacy & Security -> Microphone -> enable Ollie")

    import shutil
    report(shutil.which("say") is not None, "macOS `say` available", "`say` not on PATH")

    if cfg.tts_engine == "kokoro":
        try:
            import mlx_audio  # noqa: F401
            import misaki  # noqa: F401
            report(True, f"kokoro engine ready (voice {cfg.kokoro_voice})", "")
        except Exception as exc:
            report(False, "", f"kokoro engine selected but not installed ({exc}).\n"
                   "     uv pip install mlx-audio 'misaki[en]'  — or switch back with --engine say")
    else:
        print("  ·  tts engine: say (neural alternative: --engine kokoro)")

    from .hotkey import pretty as pretty_key
    verb = "hold" if cfg.hotkey_mode == "hold" else "tap"
    print(f"  ·  push-to-talk: {verb} {pretty_key(cfg.hotkey)}")

    try:
        import AppKit  # noqa: F401
        import Quartz  # noqa: F401
        report(True, "PyObjC (AppKit + Quartz) available", "")
    except Exception as exc:
        report(False, "", f"PyObjC missing: {exc}")

    trusted = accessibility_trusted()
    bundled = ".app/Contents/" in sys.executable or os.environ.get("OLLIE_BUNDLED") == "1"
    report(trusted,
           "Accessibility permission granted",
           "Accessibility NOT granted — push-to-talk and text injection will not work.\n"
           "     Best fix: build the app bundle, which asks for permission as itself:\n"
           "       .venv/bin/python scripts/make_app.py --run\n"
           "     Or grant your terminal: System Settings -> Privacy & Security -> Accessibility")
    if trusted and not bundled:
        print("     (granted to your terminal — the app bundle keeps this scoped to Ollie)")

    print(f"\n{'All good.' if not problems else str(problems) + ' problem(s) above.'}")
    return 0 if not problems else 1


def _request_permissions(cfg: Config) -> None:
    """Trigger the macOS permission dialogs up front, once.

    Inside Ollie.app these dialogs name Ollie and the grant sticks to the app.
    Run from a shell they name your terminal instead — which works, but grants
    every script that terminal ever runs the same power.
    """
    from .injector import accessibility_trusted, request_accessibility
    from .permissions import AUTHORIZED, request_input_monitoring
    from .state import AppState
    from .stt import WhisperSTT

    log = logging.getLogger("ollie")

    listen = request_input_monitoring()
    if listen == AUTHORIZED:
        log.info("input monitoring: granted")
    else:
        log.warning("input monitoring: %s — the push-to-talk key cannot be observed", listen)

    if accessibility_trusted():
        log.info("accessibility: granted")
    else:
        log.warning("accessibility: not granted — raising the system prompt")
        request_accessibility(prompt=True)
        log.warning(
            "Ollie will still narrate, but push-to-talk and text injection stay "
            "disabled until Accessibility is granted. Grant it, then relaunch Ollie."
        )

    # The microphone prompt blocks until the user answers it, so it must not sit
    # on the startup path — otherwise the orb never appears and Ollie looks dead.
    # probe_microphone() reports the outcome itself.
    threading.Thread(
        target=WhisperSTT(cfg, AppState()).probe_microphone,
        name="mic-probe", daemon=True,
    ).start()


def main(argv=None) -> int:
    _fix_multiprocessing()
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    overrides = {k: v for k, v in vars(args).items()
                 if k not in ("doctor", "list_sessions", "say", "test_hotkey",
                              "list_voices", "settings", "history")
                 and v is not None}
    cfg = Config.load(overrides)
    _setup_logging(cfg.verbose)

    if args.list_voices:
        from .tts import list_voices
        for name, locale in list_voices():
            marker = "*" if name == cfg.voice else " "
            print(f"{marker} {name:28} {locale}")
        print("\nPreview one with:  ./run.sh --voice NAME --say 'hello there'")
        return 0

    if args.list_sessions:
        reader = ClaudeCodeReader(cfg)
        active = reader.pick_session()
        for path, mtime in reader.list_sessions():
            marker = "*" if path == active else " "
            age = time.time() - mtime
            print(f"{marker} {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))} "
                  f"({age/60:6.1f}m ago)  {path}")
        return 0

    if args.history is not None:
        from .history import render, tail
        print(render(tail(args.history)))
        return 0

    if args.doctor:
        return _doctor(cfg)

    if args.settings:
        from .settings_report import gather, render_text
        print(render_text(gather(cfg)))
        return 0

    if args.test_hotkey:
        return _test_hotkey(cfg)

    if args.say:
        from .state import AppState
        from .tts import SayTTS
        SayTTS(cfg, AppState()).speak(args.say)
        return 0

    _request_permissions(cfg)

    narrator = Narrator(cfg, ClaudeCodeReader(cfg))

    def shutdown(*_):
        narrator.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    narrator.start()
    if cfg.autopilot or cfg.autopilot_goal:
        narrator.autopilot.arm(cfg.autopilot_goal)
    from .hotkey import pretty as pretty_key

    verb = "Hold" if cfg.hotkey_mode == "hold" else "Tap"
    logging.getLogger("ollie").info(
        "listening to your session — %s %s to talk", verb, pretty_key(cfg.hotkey)
    )

    if cfg.orb:
        from .orb import run_orb
        try:
            run_orb(narrator.state, cfg.orb_size, cfg.orb_margin, controller=narrator)
        finally:
            narrator.stop()
    else:
        try:
            while narrator.state.running:
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass
        finally:
            narrator.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
