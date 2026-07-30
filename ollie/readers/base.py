"""The reader contract.

A reader is the *only* component that knows where output comes from. It
promises one thing: "give me the new messages", returned as uniform Chunks.
Everything downstream — filter, TTS, orb — is source-agnostic.
"""

from __future__ import annotations

from ..message import Chunk


class Reader:
    name: str = "base"

    def start(self) -> None:
        """Attach to the source. Called once before the first poll()."""

    def poll(self) -> list[Chunk]:
        """Return chunks that have appeared since the last call. Never blocks long."""
        return []

    def stop(self) -> None:
        """Release any resources."""

    def describe(self) -> str:
        return self.name
