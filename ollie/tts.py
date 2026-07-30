"""Text to speech via the macOS `say` binary.

We synthesise to a WAV file and play it ourselves rather than letting `say`
talk to the speakers directly. That costs a few hundred milliseconds but buys
two things: a precise notion of when speech starts and ends, and a real
amplitude envelope to drive the orb while it is speaking.

If synthesis-to-file fails for any reason we fall back to plain `say`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import wave

import numpy as np

from .config import Config
from .state import AppState

log = logging.getLogger("ollie.tts")


# Voices nobody wants narrating their code (sound effects and gag voices).
_NOVELTY = {
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos",
    "Deranged", "Good News", "Jester", "Organ", "Superstar", "Trinoids",
    "Whisper", "Wobble", "Zarvox", "Ralph", "Fred", "Junior", "Kathy",
}

_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s{2,}(?P<locale>[a-zA-Z]{2}[_-][A-Za-z_-]+)\s+#")


def parse_voice_listing(listing: str) -> list[tuple[str, str]]:
    """Parse `say -v ?` output into (name, locale) pairs."""
    voices = []
    for line in listing.splitlines():
        match = _VOICE_LINE.match(line)
        if match:
            voices.append((match.group("name").strip(), match.group("locale")))
    return voices


def list_voices(english_only: bool = True, include_novelty: bool = False) -> list[tuple[str, str]]:
    """Installed `say` voices, curated for narration."""
    try:
        proc = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
        voices = parse_voice_listing(proc.stdout)
    except Exception as exc:
        log.warning("could not list voices: %s", exc)
        return []
    if english_only:
        voices = [v for v in voices if v[1].lower().startswith("en")]
    if not include_novelty:
        voices = [v for v in voices if v[0] not in _NOVELTY]
    return sorted(voices)


class SayTTS:
    def __init__(self, cfg: Config, state: AppState) -> None:
        self.cfg = cfg
        self.state = state
        self._interrupt = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def speak(self, text: str) -> None:
        """Blocking. Returns when speech finishes or is interrupted."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._interrupt.clear()
            path = self._synthesize(text)
            if path is None:
                self._speak_direct(text)
                return
            try:
                self._play(path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                self.state.set_amplitude(0.0)

    def stop(self) -> None:
        """Ask any in-flight speech to stop (used for barge-in)."""
        self._interrupt.set()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    # ------------------------------------------------------------------
    def _synthesize(self, text: str) -> str | None:
        handle, path = tempfile.mkstemp(prefix="ollie-", suffix=".wav")
        os.close(handle)
        cmd = ["say"]
        if self.cfg.voice:
            cmd += ["-v", self.cfg.voice]
        if self.cfg.rate:
            cmd += ["-r", str(int(self.cfg.rate))]
        cmd += ["-o", path, "--data-format=LEI16@22050", "--file-format=WAVE", "--", text]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception as exc:
            log.warning("say synthesis failed: %s", exc)
            proc = None
        if proc is None or proc.returncode != 0 or not os.path.getsize(path):
            if proc is not None and proc.returncode != 0:
                log.warning("say exited %s: %s", proc.returncode, proc.stderr.decode()[:200])
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        return path

    def _speak_direct(self, text: str) -> None:
        cmd = ["say"]
        if self.cfg.voice:
            cmd += ["-v", self.cfg.voice]
        if self.cfg.rate:
            cmd += ["-r", str(int(self.cfg.rate))]
        cmd += ["--", text]
        try:
            proc = subprocess.Popen(cmd)
        except Exception as exc:
            log.error("cannot run say: %s", exc)
            return
        while proc.poll() is None:
            if self._interrupt.is_set():
                proc.terminate()
                break
            self.state.set_amplitude(0.45)
            threading.Event().wait(0.08)
        self.state.set_amplitude(0.0)

    def _play(self, path: str) -> None:
        import sounddevice as sd

        with wave.open(path, "rb") as handle:
            channels = handle.getnchannels()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())

        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        if audio.size == 0:
            return

        done = threading.Event()
        cursor = {"i": 0}

        def callback(outdata, frame_count, _time, _status):
            start = cursor["i"]
            end = start + frame_count
            block = audio[start:end]
            cursor["i"] = end
            if block.size:
                level = float(np.sqrt(np.mean(np.square(block))))
                self.state.set_amplitude(min(1.0, level * 2.5))
            if block.size < frame_count:
                outdata[: block.size, 0] = block
                outdata[block.size:, 0] = 0.0
                raise sd.CallbackStop
            outdata[:, 0] = block

        stream = sd.OutputStream(
            samplerate=rate,
            channels=1,
            dtype="float32",
            callback=callback,
            finished_callback=done.set,
            blocksize=1024,
        )
        with stream:
            while not done.wait(0.05):
                if self._interrupt.is_set():
                    break
