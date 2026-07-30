"""The uniform shape every reader emits."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Chunk:
    """One new, clean piece of agent output.

    ``role`` is one of: assistant | tool_use | tool_result | user | system.
    ``key`` is a stable identity used for de-duplication at the reader level.
    """

    role: str
    text: str
    source: str = "claude-code"
    ts: float = field(default_factory=time.time)
    key: str = ""

    def render(self, limit: int = 1200) -> str:
        text = self.text.strip()
        if len(text) > limit:
            text = text[:limit] + " …(truncated)"
        return f"[{self.role}] {text}"
