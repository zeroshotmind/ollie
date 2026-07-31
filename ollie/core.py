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
from .grounding import Grounder
from .history import History
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
        self.grounder = Grounder(cfg)
        self.history = History(cfg)
        self.ptt = PushToTalk(cfg, self._on_talk_start, self._on_talk_stop)
        self.window_key = TapKey(cfg.window_hotkey, self._on_window_key)
        self.autopilot = Autopilot(
            cfg,
            inject=self._autopilot_inject,
            speak=self._speak_aside,
            frontmost=_frontmost_app,
        )
        self.autopilot.status = self._autopilot_status
        self.autopilot.error = self._show_error
        self.autopilot.window_shot = self._window_screenshot
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
        self._spawn(self.check_models, "model-doctor")

    def stop(self) -> None:
        self.state.stop()
        self.tts.stop()
        self.ptt.stop()
        self.window_key.stop()
        self.reader.stop()
        self.filter.close()
        self.autopilot.close()
        self.grounder.close()

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

            # Muting silences the voice only. With captions on, narration is
            # still produced and shown in the bubble; with both off, skip the
            # filter entirely.
            if self.muted and not self.cfg.captions:
                continue

            self.state.set(State.THINKING)
            try:
                line = self.filter.process(batch)
            except Exception as exc:
                log.exception("filter failed")
                self._show_error(f"Narration filter failed: {exc} — is Ollama running?")
                line = None

            if line:
                log.info("speak: %s", line)
                self.history.record(
                    "narration", line,
                    source=_context_key(self.reader),
                    source_label=self.reader.describe(),
                    muted=self.muted, style=self.cfg.style,
                )
                self.state.set(State.SPEAKING, line)
                if self.muted:
                    # hold the caption on screen for a natural reading time
                    time.sleep(min(7.0, 1.2 + 0.3 * len(line.split())))
                else:
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
        except Exception as exc:
            log.exception("recording failed")
            self._show_error(f"Recording failed: {exc}")
            text = ""
        if not text and getattr(self.stt, "last_error", ""):
            self._show_error(f"Transcription failed: {self.stt.last_error}")
        if text and self.autopilot.awaiting_goal:
            self.history.record("speech", text, role="autopilot-goal",
                                source=_context_key(self.reader))
            self.autopilot.set_goal(text)
            self.state.set(State.IDLE)
            return
        if text:
            target = _frontmost_app()
            if self.injector.inject(text):
                log.info("injected into %s: %s", target, text)
                self.history.record("speech", text, target=target,
                                    source=_context_key(self.reader))
            else:
                log.error("could not inject text — check Accessibility permission")
                self._show_error("Couldn't type into the app — "
                                 "check the Accessibility permission.")
        else:
            log.info("nothing transcribed")
        self.state.set(State.IDLE)

    # ------------------------------------------------------------------
    def _autopilot_inject(self, text: str) -> bool:
        ok = self.injector.inject(text, press_enter=True)
        if ok:
            self.history.record("autopilot", text,
                                source=_context_key(self.reader),
                                target=_frontmost_app())
        return ok

    def _autopilot_status(self, text: str) -> None:
        """Autopilot narrating its own decision process — shown in the caption
        bubble (labelled thinking state), never spoken, never queued."""
        if text:
            self.state.set(State.THINKING, text)
        elif self.state.state is State.THINKING:
            self.state.set(State.IDLE)

    def _window_screenshot(self):
        """Current screenshot of the pinned window (None off window mode) —
        autopilot's eyes when deciding its next computer-use action."""
        reader = self.reader
        if getattr(reader, "name", "") != "window":
            return None
        frame = reader.frame_on_screen()
        if frame is None:
            return None
        return self.grounder.screenshot(reader.pid, frame)

    def _autopilot_click(self, description: str, double: bool = False) -> bool:
        """Ground a described element in the pinned window and click it."""
        reader = self.reader
        if getattr(reader, "name", "") != "window":
            return False
        frame = reader.frame_on_screen()
        if frame is None:
            return False
        point = self.grounder.locate(description, reader.pid, frame)
        if point is None:
            self._speak_aside(f"I couldn't find {description}.")
            return False
        ok = self.injector.click(*point, count=2 if double else 1)
        if ok:
            verb = "Double-clicking" if double else "Clicking"
            self._speak_aside(f"{verb} {description}.")
            self.history.record("autopilot", f"{verb.lower()} {description}",
                                source=_context_key(reader),
                                target=self.autopilot.extra_app)
        return ok

    def _autopilot_key(self, spec: str) -> bool:
        ok = self.injector.key(spec)
        if ok:
            self.history.record("autopilot", f"pressed {spec}",
                                source=_context_key(self.reader),
                                target=self.autopilot.extra_app)
        return ok

    def _autopilot_clear(self) -> bool:
        ok = self.injector.clear_field()
        if ok:
            self.history.record("autopilot", "cleared the text field",
                                source=_context_key(self.reader),
                                target=self.autopilot.extra_app)
        return ok

    def _autopilot_scroll(self, direction: str) -> bool:
        reader = self.reader
        frame = getattr(reader, "frame_on_screen", lambda: None)()
        if frame is None:
            return False
        x, y, w, h = frame
        ok = self.injector.scroll(x + w / 2, y + h / 2,
                                  5 if direction == "up" else -5)
        if ok:
            self.history.record("autopilot", f"scrolled {direction}",
                                source=_context_key(reader),
                                target=self.autopilot.extra_app)
        return ok

    def _show_error(self, text: str) -> None:
        """Surface a failure in the caption bubble (never spoken). Recorded
        with type "error", which the history views deliberately skip — the
        trajectory stays about the conversation, not the plumbing."""
        self.history.record("error", text, source=_context_key(self.reader))
        if not self.cfg.captions:
            return
        label = f"⚠️ {text}"

        def run() -> None:
            self.state.set(State.THINKING, label)
            time.sleep(min(6.0, 1.5 + 0.3 * len(text.split())))
            if self.state.state is State.THINKING and self.state.label == label:
                self.state.set(State.IDLE)

        threading.Thread(target=run, daemon=True).start()

    def _speak_aside(self, text: str) -> None:
        """Status lines (source switches, autopilot) — spoken and captioned,
        never queued. Muted: caption only; captions off too: dropped."""
        if self.muted and not self.cfg.captions:
            return

        def run() -> None:
            self.state.set(State.SPEAKING, text)
            if self.muted:
                time.sleep(min(5.0, 1.2 + 0.3 * len(text.split())))
            else:
                self.tts.speak(text)
            # only step down if nothing else has claimed the state since
            if self.state.state is State.SPEAKING and self.state.label == text:
                self.state.set(State.IDLE)

        threading.Thread(target=run, daemon=True).start()

    def toggle_autopilot(self) -> bool:
        return self.autopilot.toggle()

    def arm_autopilot_from_file(self, path: str) -> None:
        """Arm autopilot with a goal written in a markdown file.

        Same loop as a spoken goal — the file is just a roomier way to
        describe the task (steps, constraints, acceptance criteria).
        """
        import os

        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                goal = fh.read().strip()
        except OSError as exc:
            log.error("cannot read goal file %s: %s", path, exc)
            self._speak_aside("I couldn't read that goal file.")
            return
        if not goal:
            self._speak_aside("That goal file is empty.")
            return
        # keep well inside the model's context window
        if len(goal) > 4000:
            goal = goal[:4000] + "\n(…truncated)"
        name = os.path.basename(path)
        log.info("autopilot goal from %s (%d chars)", path, len(goal))
        self.history.record("speech", f"goal file: {name}", role="autopilot-goal",
                            source=_context_key(self.reader), target=name)
        self.autopilot.enabled = True
        self.autopilot.set_goal(goal)

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
        # window mode can also drive the GUI: clicks (grounded by the vision
        # model), keystrokes, field clears and scrolling
        gui_on = self.cfg.computer_use and self.cfg.grounding_model
        self.autopilot.click = self._autopilot_click if gui_on else None
        self.autopilot.key = self._autopilot_key if gui_on else None
        self.autopilot.clear = self._autopilot_clear if gui_on else None
        self.autopilot.scroll = self._autopilot_scroll if gui_on else None
        self._speak_aside(f"Now watching {label}.")

    def narrate_transcript(self) -> None:
        """Back to the default Claude Code transcript reader."""
        from .readers.claude_code import ClaudeCodeReader

        self._swap_reader(ClaudeCodeReader(self.cfg))
        self.autopilot.extra_app = ""
        self.autopilot.prepare = None
        self.autopilot.click = None
        self.autopilot.key = None
        self.autopilot.clear = None
        self.autopilot.scroll = None
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
        self.history.record("source", reader.describe(),
                            source=_context_key(reader),
                            source_label=reader.describe())
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

    def set_filter_model(self, model: str) -> None:
        """Runtime switch of the narration filter model (read per request)."""
        if not model or model == self.cfg.ollama_model:
            return
        self.cfg.ollama_model = model
        self._persist()
        log.info("filter model: %s", model)
        self._speak_aside(f"Narration now filtered by {model.split(':')[0]}.")

    def set_autopilot_model(self, model: str) -> None:
        if not model or model == self.cfg.autopilot_model:
            return
        self.cfg.autopilot_model = model
        self._persist()
        log.info("autopilot model: %s", model)
        self._speak_aside(f"Autopilot now driven by {model.split(':')[0]}.")

    def set_grounding_model(self, model: str) -> None:
        if not model or model == self.cfg.grounding_model:
            return
        self.cfg.grounding_model = model
        self._persist()
        log.info("grounding model: %s", model)
        self._speak_aside(f"Clicks now grounded by {model.split(':')[0]}.")

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

    def check_models(self, announce_ok: bool = False) -> None:
        """Verify every configured model exists; download what's missing.

        Runs automatically at startup and from the orb menu. Progress lands
        in the caption bubble; failures use the error path. Serialised so a
        menu click during the startup pass doesn't double-download.
        """
        from . import doctor

        lock = getattr(self, "_doctor_lock", None)
        if lock is None:
            lock = self._doctor_lock = threading.Lock()
        if not lock.acquire(blocking=False):
            return
        try:
            missing = doctor.missing_models(self.cfg)
            if not missing:
                if announce_ok:
                    self._speak_aside("All models are ready.")
                return
            names = ", ".join(name for name, _ in missing)
            self._speak_aside(
                f"Setting up: {len(missing)} model"
                f"{'s' if len(missing) > 1 else ''} to download — {names}. "
                "I'll keep working meanwhile.")
            for name, why in missing:
                ok = doctor.acquire(
                    self.cfg, name,
                    lambda text: self.state.set(State.THINKING, text))
                if ok:
                    self._speak_aside(f"{name} is ready ({why}).")
                else:
                    self._show_error(f"Couldn't get {name} ({why}) — "
                                     "see the log for details.")
            if self.state.state is State.THINKING:
                self.state.set(State.IDLE)
        finally:
            lock.release()

    def toggle_computer_use(self) -> bool:
        self.cfg.computer_use = not self.cfg.computer_use
        self._persist()
        # apply immediately if a window is pinned right now
        on_window = getattr(self.reader, "name", "") == "window"
        gui_on = self.cfg.computer_use and self.cfg.grounding_model and on_window
        self.autopilot.click = self._autopilot_click if gui_on else None
        self.autopilot.key = self._autopilot_key if gui_on else None
        self.autopilot.clear = self._autopilot_clear if gui_on else None
        self.autopilot.scroll = self._autopilot_scroll if gui_on else None
        log.info("computer use %s", "on" if self.cfg.computer_use else "off")
        self._speak_aside("Computer use on. I can click in pinned windows."
                          if self.cfg.computer_use else "Computer use off.")
        return self.cfg.computer_use

    def toggle_captions(self) -> bool:
        self.cfg.captions = not self.cfg.captions
        self._persist()
        log.info("captions %s", "on" if self.cfg.captions else "off")
        return self.cfg.captions

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self.tts.stop()
        log.info("narration %s", "muted" if self.muted else "unmuted")
        return self.muted
