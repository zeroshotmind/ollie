"""Speech to text with MLX Whisper (Apple Silicon native).

Recording is push-to-talk: the hotkey opens the mic, releasing it closes it and
hands the buffer to Whisper. Mic amplitude is published to AppState so the orb
reacts while you talk.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from .config import Config
from .state import AppState

log = logging.getLogger("ollie.stt")

# Whisper's own filler output for near-silent input.
# Below this peak the buffer is silence, not speech. A denied microphone
# yields exactly 0.0; a live-but-quiet room still peaks well above this.
SILENCE_PEAK = 0.004

_JUNK = {
    "", ".", "you", "thank you.", "thanks for watching!", "bye.",
    "thank you for watching!", "[blank_audio]", "(silence)",
}


class WhisperSTT:
    def __init__(self, cfg: Config, state: AppState) -> None:
        self.cfg = cfg
        self.state = state
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._ready = threading.Event()
        self.last_error = ""     # why the last transcription returned nothing
        self._mlx = None

    # ------------------------------------------------------------------
    def warmup(self) -> None:
        """Import MLX and prime the model so the first utterance is not slow."""
        try:
            # Hugging Face draws a download bar on stderr on every startup.
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            import mlx_whisper

            self._mlx = mlx_whisper
            silence = np.zeros(self.cfg.sample_rate // 2, dtype=np.float32)
            from .mlxexec import run as mlx_run

            mlx_run(
                mlx_whisper.transcribe,
                silence, path_or_hf_repo=self.cfg.whisper_repo,
                language="en", verbose=None,
            )
            log.info("whisper ready (%s)", self.cfg.whisper_repo)
        except Exception as exc:
            log.error("whisper warmup failed: %s", exc)
        finally:
            self._ready.set()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def probe_microphone(self) -> str:
        """Ask for the microphone up front and report the real answer.

        Opening the stream is not a permission check: when access is denied
        CoreAudio opens it happily and returns zeros. So ask TCC directly.
        """
        import sounddevice as sd

        from .permissions import AUTHORIZED, request_microphone

        status = request_microphone()
        try:
            device = sd.query_devices(kind="input")["name"]
        except Exception:
            device = "?"

        if status == AUTHORIZED:
            log.info("microphone: %s (%s)", status, device)
        else:
            log.error(
                "microphone: %s — Ollie will record silence. "
                "Grant it in System Settings, Privacy & Security, Microphone.",
                status,
            )
        return status

    # ------------------------------------------------------------------
    def start_recording(self) -> bool:
        import sounddevice as sd

        with self._lock:
            if self._stream is not None:
                return False
            self._frames = []
            self._started_at = time.time()

            def callback(indata, _frames, _time, status):
                if status:
                    log.debug("input status: %s", status)
                block = indata[:, 0].copy()
                self._frames.append(block)
                level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
                self.state.set_amplitude(min(1.0, level * 8.0))

            try:
                self._stream = sd.InputStream(
                    samplerate=self.cfg.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=callback,
                    blocksize=1024,
                )
                self._stream.start()
            except Exception as exc:
                log.error("cannot open microphone: %s", exc)
                self._stream = None
                return False
        return True

    def stop_recording(self) -> str:
        with self._lock:
            stream = self._stream
            self._stream = None
            frames = self._frames
            self._frames = []
        self.state.set_amplitude(0.0)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if not frames:
            return ""

        audio = np.concatenate(frames).astype(np.float32)
        duration = audio.size / float(self.cfg.sample_rate)
        if duration < 0.35:
            log.info("recording too short (%.2fs), ignoring", duration)
            return ""

        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if peak < SILENCE_PEAK:
            from .permissions import AUTHORIZED, microphone_status

            status = microphone_status()
            if status != AUTHORIZED:
                log.error(
                    "captured %.1fs of pure silence — microphone access is %s. "
                    "System Settings, Privacy & Security, Microphone, enable Ollie.",
                    duration, status,
                )
            else:
                log.warning(
                    "captured %.1fs at peak %.4f — nothing audible reached the mic. "
                    "Check the input device and that you are not muted.",
                    duration, peak,
                )
            return ""
        log.debug("captured %.1fs, peak %.3f", duration, peak)
        max_samples = int(self.cfg.max_record_seconds * self.cfg.sample_rate)
        if audio.size > max_samples:
            audio = audio[-max_samples:]

        return self.transcribe(audio)

    # ------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray) -> str:
        if self._mlx is None:
            try:
                import mlx_whisper

                self._mlx = mlx_whisper
            except Exception as exc:
                log.error("mlx_whisper unavailable: %s", exc)
                self.last_error = f"mlx_whisper unavailable: {exc}"
                return ""
        started = time.time()
        self.last_error = ""
        try:
            from .mlxexec import run as mlx_run

            # verbose=None (not False) is what silences mlx-whisper's tqdm bar.
            # Run on the shared MLX thread: this method is called from a fresh
            # thread per utterance, and MLX streams are thread-bound.
            result = mlx_run(
                self._mlx.transcribe,
                audio,
                path_or_hf_repo=self.cfg.whisper_repo,
                language="en",
                verbose=None,
                condition_on_previous_text=False,
            )
        except Exception as exc:
            log.error("transcription failed: %s", exc)
            self.last_error = str(exc)
            return ""
        text = str(result.get("text", "")).strip()
        log.info("transcribed %.1fs of audio in %.2fs: %r",
                 audio.size / self.cfg.sample_rate, time.time() - started, text)
        if text.lower().strip() in _JUNK:
            return ""
        return text

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def elapsed(self) -> float:
        return time.time() - self._started_at if self._stream is not None else 0.0
