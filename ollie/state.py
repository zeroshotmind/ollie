"""Shared application state — what the orb draws and the core loop coordinates on."""

from __future__ import annotations

import threading
import time
from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class AppState:
    """Thread-safe holder for the current state plus a live audio amplitude.

    Every component writes here; the orb reads it at frame rate.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = State.IDLE
        self._amplitude = 0.0
        self._label = ""
        self._changed_at = time.time()
        self.stop_event = threading.Event()

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def label(self) -> str:
        with self._lock:
            return self._label

    @property
    def amplitude(self) -> float:
        with self._lock:
            return self._amplitude

    @property
    def age(self) -> float:
        with self._lock:
            return time.time() - self._changed_at

    def set(self, state: State, label: str = "") -> None:
        with self._lock:
            if state != self._state:
                self._changed_at = time.time()
            self._state = state
            self._label = label
            if state in (State.IDLE, State.THINKING):
                self._amplitude = 0.0

    def set_amplitude(self, value: float) -> None:
        with self._lock:
            self._amplitude = max(0.0, min(1.0, float(value)))

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def running(self) -> bool:
        return not self.stop_event.is_set()
