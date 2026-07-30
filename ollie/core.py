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
from .filter import OllamaFilter
from .hotkey import PushToTalk
from .injector import Injector
from .message import Chunk
from .readers.base import Reader
from .state import AppState, State
from .stt import WhisperSTT
from .tts import SayTTS

log = logging.getLogger("ollie.core")


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
        self.tts = SayTTS(cfg, self.state)
        self.stt = WhisperSTT(cfg, self.state)
        self.injector = Injector(cfg)
        self.ptt = PushToTalk(cfg, self._on_talk_start, self._on_talk_stop)
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
        if self.ptt.start():
            self._spawn(self._watch_hotkey, "hotkey-watchdog")

    def stop(self) -> None:
        self.state.stop()
        self.tts.stop()
        self.ptt.stop()
        self.reader.stop()
        self.filter.close()

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
    def set_style(self, style: str) -> None:
        """Runtime style switch (also persisted, so it survives restarts)."""
        if style not in ("brief", "full", "verbatim"):
            return
        self.cfg.style = style
        try:
            self.cfg.save()
        except Exception:
            log.debug("could not persist style change", exc_info=True)
        log.info("narration style: %s", style)

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self.tts.stop()
        log.info("narration %s", "muted" if self.muted else "unmuted")
        return self.muted
