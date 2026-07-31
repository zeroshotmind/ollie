"""The shared core: reader -> filter -> TTS, and hotkey -> STT -> injection.

Nothing in here knows what a Claude Code transcript looks like. Swap the reader
and the rest keeps working.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from .config import Config
from .autopilot import Autopilot
from .filter import OllamaFilter
from .hotkey import PushToTalk, TapKey
from .injector import Injector
from .message import Chunk
from .readers.base import Reader
from .state import AppState, State
from .stt import WhisperSTT
from .tts import make_tts

log = logging.getLogger("ollie.core")


def _context_key(reader: Reader) -> str:
    """Stable identity of a narration source, for per-source spoken memory."""
    if getattr(reader, "name", "") == "window":
        return f"window:{reader.pid}:{reader.window_index}"
    return "claude"


def _frontmost_app() -> str:
    """Which app the text is about to be pasted into."""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.localizedName() if app is not None else "?"
    except Exception:
        return "?"


class Narrator:
    def __init__(self, cfg: Config, reader: Reader) -> None:
        self.cfg = cfg
        self.state = AppState()
        self.reader = reader
        self.filter = OllamaFilter(cfg)
        self.tts = make_tts(cfg, self.state)
        self.stt = WhisperSTT(cfg, self.state)
        self.injector = Injector(cfg)
        self.ptt = PushToTalk(cfg, self._on_talk_start, self._on_talk_stop)
        self.window_key = TapKey(cfg.window_hotkey, self._on_window_key)
        self.autopilot = Autopilot(
            cfg,
            inject=lambda text: self.injector.inject(text, press_enter=True),
            speak=self._speak_aside,
            frontmost=_frontmost_app,
        )
        self.queue: queue.Queue[Chunk] = queue.Queue()
        self.muted = False
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    def start(self) -> None:
        ok, message = self.filter.health()
        log.info("%s", message)

        self.reader.start()
        log.info("reader: %s", self.reader.describe())

        self._spawn(self._read_loop, "reader")
        self._spawn(self._narrate_loop, "narrator")
        self._spawn(self.stt.warmup, "whisper-warmup")
        if hasattr(self.tts, "warmup"):
            self._spawn(self.tts.warmup, "tts-warmup")
        if self.ptt.start():
            self._spawn(self._watch_hotkey, "hotkey-watchdog")
        self.window_key.start()

    def stop(self) -> None:
        self.state.stop()
        self.tts.stop()
        self.ptt.stop()
        self.window_key.stop()
        self.reader.stop()
        self.filter.close()
        self.autopilot.close()

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # ------------------------------------------------------------------
    # narration path
    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        while self.state.running:
            try:
                for chunk in self.reader.poll():
                    self.queue.put(chunk)
            except Exception:
                log.exception("reader poll failed")
                time.sleep(1.0)
            time.sleep(self.cfg.poll_interval)

    def _narrate_loop(self) -> None:
        while self.state.running:
            batch = self._collect_batch()
            if not batch:
                continue

            for chunk in batch:
                self.autopilot.observe(chunk)
            batch = [c for c in batch if c.role != "turn_end"]
            if not batch:
                continue

            # Never talk over the user. Wait for them to finish their utterance.
            while self.state.running and self.state.state is State.LISTENING:
                time.sleep(0.1)
                batch.extend(self._drain())

            if self.muted:
                continue

            self.state.set(State.THINKING)
            try:
                line = self.filter.process(batch)
            except Exception:
                log.exception("filter failed")
                line = None

            if line:
                log.info("speak: %s", line)
                self.state.set(State.SPEAKING, line)
                self.tts.speak(line)

            if self.state.state is not State.LISTENING:
                self.state.set(State.IDLE)

    def _watch_hotkey(self) -> None:
        # Two distinct failures: the tap thread dying (rare), and macOS
        # flipping the tap off after a stalled callback (the one that bit us).
        while self.state.running:
            time.sleep(3.0)
            if self.ptt.healthy:
                self.ptt.note_healthy()
            else:
                self.ptt.heal()

    def _collect_batch(self) -> list[Chunk]:
        """Block for one chunk, then gather everything that lands shortly after."""
        try:
            first = self.queue.get(timeout=0.3)
        except queue.Empty:
            return []
        batch = [first]
        deadline = time.time() + self.cfg.batch_debounce
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                batch.append(self.queue.get(timeout=remaining))
            except queue.Empty:
                break
        return batch

    def _drain(self) -> list[Chunk]:
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                return out

    # ------------------------------------------------------------------
    # voice path
    # ------------------------------------------------------------------
    def _on_talk_start(self) -> None:
        # Opening the microphone takes long enough that doing it here would
        # stall the key listener. macOS disables an event tap whose callback
        # overruns, which silently kills push-to-talk after a few uses.
        threading.Thread(target=self._begin_utterance, daemon=True).start()

    def _begin_utterance(self) -> None:
        self.tts.stop()                       # barge-in
        if not self.stt.ready:
            log.info("whisper still warming up — recording anyway")
        if self.stt.start_recording():
            self.state.set(State.LISTENING)
            log.info("listening…")
        else:
            log.error("could not open the microphone")

    def _on_talk_stop(self) -> None:
        # Transcription takes ~1s; do not block the key listener thread.
        threading.Thread(target=self._finish_utterance, daemon=True).start()

    def _finish_utterance(self) -> None:
        self.state.set(State.THINKING)
        try:
            text = self.stt.stop_recording()
        except Exception:
            log.exception("recording failed")
            text = ""
        if text and self.autopilot.awaiting_goal:
            self.autopilot.set_goal(text)
            self.state.set(State.IDLE)
            return
        if text:
            target = _frontmost_app()
            if self.injector.inject(text):
                log.info("injected into %s: %s", target, text)
            else:
                log.error("could not inject text — check Accessibility permission")
        else:
            log.info("nothing transcribed")
        self.state.set(State.IDLE)

    # ------------------------------------------------------------------
    def _speak_aside(self, text: str) -> None:
        """Status lines from autopilot — spoken unless muted, never queued."""
        if self.muted:
            return
        threading.Thread(target=self.tts.speak, args=(text,), daemon=True).start()

    def toggle_autopilot(self) -> bool:
        return self.autopilot.toggle()

    # ------------------------------------------------------------------
    # source switching (orb menu → Narrate window)
    # ------------------------------------------------------------------
    def narrate_window(self, pid: int, index: int, label: str) -> None:
        """Pin narration to one specific window, like sharing it on a call."""
        from .readers.window import WindowReader

        reader = WindowReader(self.cfg, pid, index, label)
        self._swap_reader(reader)
        # autopilot may inject into the pinned window's app, not just terminals,
        # and focuses that window first so the text lands in the right place
        self.autopilot.extra_app = label.split(" — ")[0].strip()
        self.autopilot.prepare = reader.focus
        self._speak_aside(f"Now narrating {label}.")

    def narrate_transcript(self) -> None:
        """Back to the default Claude Code transcript reader."""
        from .readers.claude_code import ClaudeCodeReader

        self._swap_reader(ClaudeCodeReader(self.cfg))
        self.autopilot.extra_app = ""
        self.autopilot.prepare = None
        self._speak_aside("Back to the Claude Code session.")

    def _on_window_key(self) -> None:
        """Tap the window hotkey: narrate the focused window; tap it again on
        the same window to go back to the Claude Code transcript."""
        # The tap callback must return fast — do the AX work off-thread.
        threading.Thread(target=self._pin_frontmost, daemon=True).start()

    def _pin_frontmost(self) -> None:
        from .readers.window import frontmost_window

        win = frontmost_window()
        if win is None:
            self._speak_aside("I can't see which window is focused.")
            return
        reader = self.reader
        if (getattr(reader, "name", "") == "window"
                and getattr(reader, "pid", None) == win["pid"]
                and getattr(reader, "window_index", None) == win["index"]):
            self.narrate_transcript()
            return
        title = win["title"]
        if len(title) > 46:
            title = title[:46] + "…"
        self.narrate_window(win["pid"], win["index"], f"{win['app']} — {title}")

    def _swap_reader(self, reader: Reader) -> None:
        old = self.reader
        reader.start()
        # _read_loop re-reads self.reader every iteration, so assignment is
        # the whole handover; stop the old one after so no poll gap opens
        self.reader = reader
        self.filter.set_context(_context_key(reader))
        try:
            old.stop()
        except Exception:
            log.exception("old reader did not stop cleanly")
        self._drain()          # drop queued chunks from the old source
        self.autopilot.reset_observations()
        log.info("reader: %s", reader.describe())

    def set_style(self, style: str) -> None:
        """Runtime style switch (also persisted, so it survives restarts)."""
        if style not in ("brief", "full", "verbatim"):
            return
        self.cfg.style = style
        self._persist()
        log.info("narration style: %s", style)

    def set_engine(self, engine: str) -> None:
        if engine not in ("say", "kokoro") or engine == self.cfg.tts_engine:
            return
        self.cfg.tts_engine = engine
        self._persist()
        self.tts.stop()
        self.tts = make_tts(self.cfg, self.state)
        log.info("tts engine: %s", type(self.tts).__name__)
        if hasattr(self.tts, "warmup"):
            def warm_and_preview():
                self.tts.warmup()
                self.tts.speak("Switched to the neural voice.")
            threading.Thread(target=warm_and_preview, daemon=True).start()
        else:
            self._speak_aside("Switched to the system voice.")

    def set_voice(self, voice: str) -> None:
        if self.cfg.tts_engine == "kokoro":
            self.cfg.kokoro_voice = voice
            self._persist()
            log.info("kokoro voice: %s", voice)
            self._speak_aside(f"This is the {voice.split('_')[-1]} voice.")
            return
        self.cfg.voice = voice
        self._persist()
        log.info("voice: %s", voice)
        # A short preview so the choice is audible immediately.
        threading.Thread(
            target=self.tts.speak,
            args=(f"Hi, this is {voice}. I'll narrate your session.",),
            daemon=True,
        ).start()

    def set_tone(self, tone: str) -> None:
        from .filter import TONES

        if tone not in TONES:
            return
        self.cfg.tone = tone
        self._persist()
        log.info("tone: %s", tone)

    def _persist(self) -> None:
        try:
            self.cfg.save()
        except Exception:
            log.debug("could not persist config change", exc_info=True)

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self.tts.stop()
        log.info("narration %s", "muted" if self.muted else "unmuted")
        return self.muted
