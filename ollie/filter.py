"""Filter / dedup stage.

A small local model (Ollama) decides what is genuinely new and worth speaking,
and condenses verbose output into one spoken line. It holds a running memory of
what has already been said so the narration never repeats itself.

If Ollama is unreachable the stage degrades to a deterministic local fallback
rather than going silent.
"""

from __future__ import annotations

import logging
import re
from collections import deque

import httpx

from .config import Config
from .message import Chunk

log = logging.getLogger("ollie.filter")

SYSTEM_PROMPT_BRIEF = """You are the narration filter for Ollie, a voice companion that reads a coding agent's activity aloud to a developer who is not looking at the screen.

You are given NEW events from the agent's session and the lines you have ALREADY SPOKEN.

Your job is to SUMMARISE, never to copy. Write one sentence a colleague would say out loud.

Rules:
- Never repeat an event verbatim. Rewrite it in your own words, as speech.
- Never output code, shell commands, flags, file paths with slashes, URLs, or symbols. Bare file names only.
- Say only what is genuinely new. If the events repeat or rephrase something in ALREADY SPOKEN, reply SKIP.
- Hard limit: {max_words} words. Shorter is better.
- Condense: a long diff becomes "edited the auth function, added error handling"; a wall of test output becomes "tests passed" or "three tests failed in test_auth".
- Routine, uninteresting steps (reading one file, listing a directory) are usually not worth speaking. Reply SKIP.
- If the agent asked the user a question or is waiting on them, always speak it — that is the most important thing to surface.
- Plain spoken English: no markdown, no bullets, no emoji, no quotes, no preamble, no labels.

Examples.

NEW EVENTS:
[tool_use] reading auth.py
[tool_use] editing auth.py
[assistant] I wrapped the token refresh in a try/except and added a regression test.
YOU SAY: Edited the auth module, added error handling around token refresh plus a test.

NEW EVENTS:
[tool_use] running a python snippet
[tool_use] running pytest tests
YOU SAY: Running the test suite.

NEW EVENTS:
[tool_use] listing files
[tool_use] reading config.py
YOU SAY: SKIP

NEW EVENTS:
[assistant] Should I drop the legacy v1 endpoint, or keep it behind a flag?
YOU SAY: It's asking whether to drop the legacy v1 endpoint or keep it behind a flag.

Reply with the sentence to speak and nothing else."""

SYSTEM_PROMPT_FULL = """You are the narration filter for Ollie, a voice companion that reads a coding agent's activity aloud to a developer who is not looking at the screen.

You are given NEW events from the agent's session and the lines you have ALREADY SPOKEN.

Your job is a LOSS-LESS retelling: convey every piece of information in the new events — every action taken, every file name, every result, every number, every question — rewritten as natural speech. Several sentences are fine.

Rules:
- Keep all facts. Compress wording, never content.
- Never output code, shell syntax, flags, or full paths. A path becomes its bare file name; code becomes a description of what it does.
- Reply SKIP only when the events contain nothing that has not already been spoken.
- If the agent asked the user a question or is waiting on them, say so first.
- Plain spoken English: no markdown, no bullets, no emoji, no quotes, no preamble, no labels.

Reply with the sentences to speak and nothing else."""

# A tone shapes delivery, never content. Facts stay identical across tones.
TONES = {
    "neutral": None,
    "warm": ("friendly and encouraging, like a supportive pair-programming buddy — "
             "it is fine to sound pleased when something works"),
    "snarky": ("dry wit and light sarcasm, like a colleague who has seen everything — "
               "wry about the code and the tools, never about the user"),
    "minimal": ("telegraphic — the fewest words that still carry every fact, "
                "no pleasantries, no filler"),
}

# Things a small model leaks when it is confused about the format.
_LABEL_LEAK = re.compile(
    r"^\s*(new events|already spoken|you say|events|response|output)\s*:?\s*",
    re.I,
)
_PATHY = re.compile(r"\S*/\S*")
_FLAGGY = re.compile(r"(?<!\w)--?[a-zA-Z][\w-]*")
_ASSIGNY = re.compile(r"\b[A-Z_]{3,}=\S*")

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MARKDOWN_CHARS = re.compile(r"[*_#>\[\]|]+")
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")

_STOPWORDS = {
    "the", "a", "an", "and", "to", "of", "in", "is", "it", "for", "on", "with",
    "that", "this", "i", "im", "its", "now", "then", "just",
}


def clean_for_speech(text: str, max_words: int) -> str:
    text = _THINK.sub(" ", text or "")
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _MARKDOWN_CHARS.sub(" ", text)
    # Anything that still looks like a command line is unspeakable.
    text = _ASSIGNY.sub(" ", text)
    text = _FLAGGY.sub(" ", text)
    text = _PATHY.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1] or " ", text)
    text = text.replace("—", ", ").replace("…", " ")
    text = _WS.sub(" ", text).strip().strip('"').strip()
    text = _LABEL_LEAK.sub("", text)
    for prefix in ("ollie:", "narration:", "speak:", "answer:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.lstrip("-•* ").strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(" ,;:.") + "."
    return text.strip()


def _token_list(text: str) -> list[str]:
    """Ordered content words — used for substring (echo) comparison."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS]


def _tokens(text: str) -> set[str]:
    return set(_token_list(text))


def clean_for_verbatim(text: str) -> str:
    """Light cleanup for verbatim mode: make it speakable, drop nothing else."""
    text = _THINK.sub(" ", text or "")
    text = _CODE_FENCE.sub(" — code block — ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MARKDOWN_CHARS.sub(" ", text)
    return _WS.sub(" ", text).strip()


def list_models(cfg: Config) -> list[str]:
    """Model names the local Ollama has pulled (for the orb's model menus)."""
    try:
        response = httpx.get(f"{cfg.ollama_url}/api/tags", timeout=3.0)
        response.raise_for_status()
        names = [m.get("name", "") for m in response.json().get("models", [])]
        return sorted(n for n in names if n)
    except Exception:
        return []


class OllamaFilter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Spoken-history memory is kept per source: what was said about one
        # window must not suppress (or contaminate the context of) another.
        # Switching sources stashes the current memory and restores the
        # target's, so coming back to a source picks up where it left off.
        self._histories: dict[str, deque[str]] = {}
        self.history: deque[str] = self._histories.setdefault(
            "default", deque(maxlen=cfg.history_window))
        self._client = httpx.Client(timeout=cfg.filter_timeout)
        self.degraded = False

    def set_context(self, key: str) -> None:
        """Switch to the spoken-history memory belonging to ``key``."""
        self.history = self._histories.setdefault(
            key, deque(maxlen=self.cfg.history_window))

    # ------------------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        """(ok, message) — is Ollama up and is the model pulled?"""
        try:
            response = self._client.get(f"{self.cfg.ollama_url}/api/tags", timeout=5.0)
            response.raise_for_status()
            names = [m.get("name", "") for m in response.json().get("models", [])]
        except Exception as exc:
            return False, f"Ollama unreachable at {self.cfg.ollama_url} ({exc})"
        wanted = self.cfg.ollama_model
        if wanted in names or any(n.split(":")[0] == wanted.split(":")[0] for n in names):
            return True, f"Ollama up, {wanted} available"
        return False, f"Ollama up but model {wanted} not pulled (run: ollama pull {wanted})"

    # ------------------------------------------------------------------
    @property
    def style(self) -> str:
        style = getattr(self.cfg, "style", "brief")
        return style if style in ("brief", "full", "verbatim") else "brief"

    def process(self, chunks: list[Chunk]) -> str | None:
        """Return what to speak for these events, or None. Style-dependent:

        brief    — one terse line; routine steps are skipped        (Ollama)
        full     — loss-less retelling; nothing dropped             (Ollama)
        verbatim — the agent's own words, cleaned for speech        (no model)
        """
        if not chunks:
            return None
        style = self.style

        if style == "verbatim":
            return self._verbatim(chunks)

        events = self._format_events(chunks)
        if not events:
            return None

        try:
            raw = self._ask(events, style)
            self.degraded = False
        except Exception as exc:
            if not self.degraded:
                log.warning("filter falling back to local condense: %s", exc)
            self.degraded = True
            raw = self._local_fallback(chunks)

        if not raw:
            return None
        if raw.strip().upper().startswith("SKIP"):
            return None

        cap = self.cfg.max_words if style == "brief" else max(120, self.cfg.max_words * 4)
        text = clean_for_speech(raw, cap)
        if len(text) < 2 or text.upper() == "SKIP":
            return None
        if style == "brief":
            # Only brief mode second-guesses the model; full mode is allowed to
            # restate tool events, that is the point of it.
            if self._is_echo(text, chunks):
                log.debug("dropped verbatim echo of a tool event: %s", text)
                return None
            if self._too_similar(text):
                return None

        self.history.append(text[:220])
        return text

    def _system_prompt(self, style: str) -> str:
        if style == "full":
            base = SYSTEM_PROMPT_FULL
        else:
            base = SYSTEM_PROMPT_BRIEF.format(max_words=self.cfg.max_words)
        tone = TONES.get(getattr(self.cfg, "tone", "neutral"))
        if tone:
            base += f"\n\nDelivery tone: {tone}"
        return base

    def _verbatim(self, chunks: list[Chunk]) -> str | None:
        parts = []
        for chunk in chunks:
            if chunk.role == "assistant":
                text = clean_for_verbatim(chunk.text)
            else:
                text = clean_for_verbatim(chunk.text)
                text = text[0].upper() + text[1:] if text else text
            if text:
                parts.append(text if text.endswith((".", "!", "?")) else text + ".")
        if not parts:
            return None
        spoken = " ".join(parts)
        self.history.append(spoken[:220])
        return spoken

    # ------------------------------------------------------------------
    def _format_events(self, chunks: list[Chunk]) -> str:
        lines: list[str] = []
        budget = 6000
        for chunk in chunks:
            rendered = chunk.render()
            if not rendered.strip():
                continue
            budget -= len(rendered)
            if budget <= 0:
                lines.append("[…more events omitted…]")
                break
            lines.append(rendered)
        return "\n".join(lines).strip()

    def _ask(self, events: str, style: str = "brief") -> str:
        spoken = "\n".join(f"- {line}" for line in self.history) or "(nothing yet)"
        user = f"ALREADY SPOKEN:\n{spoken}\n\nNEW EVENTS:\n{events}"
        system = self._system_prompt(style)
        predict = 400 if style == "full" else 90
        payload = {
            "model": self.cfg.ollama_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": predict, "num_ctx": 4096},
        }
        response = self._client.post(f"{self.cfg.ollama_url}/api/chat", json=payload)
        response.raise_for_status()
        return ((response.json() or {}).get("message") or {}).get("content", "") or ""

    def _local_fallback(self, chunks: list[Chunk]) -> str:
        """Deterministic condense used when the model is unavailable."""
        prose = [c.text for c in chunks if c.role == "assistant"]
        if prose:
            text = _CODE_FENCE.sub(" ", prose[-1])
            sentences = re.split(r"(?<=[.!?])\s+", _WS.sub(" ", text).strip())
            return " ".join(sentences[:2])
        tools = [c.text for c in chunks if c.role == "tool_use"]
        if tools:
            unique: list[str] = []
            for item in tools:
                head = item.split(":")[0]
                if head not in unique:
                    unique.append(head)
            return ", ".join(unique[:3])
        return ""

    @staticmethod
    def _is_echo(text: str, chunks: list[Chunk]) -> bool:
        """Reject output that is just a tool event copied back at us.

        Only tool events are checked: echoing the agent's own prose is often
        the right summary, but echoing "running pytest tests" adds nothing and
        is exactly what a 3B model does when it gives up.
        """
        candidate = " ".join(_token_list(text))
        if not candidate:
            return True
        for chunk in chunks:
            if chunk.role != "tool_use":
                continue
            other = " ".join(_token_list(chunk.text))
            if other and (candidate in other or other in candidate):
                return True
        return False

    def _too_similar(self, text: str) -> bool:
        candidate = _tokens(text)
        if not candidate:
            return True
        for previous in self.history:
            other = _tokens(previous)
            if not other:
                continue
            overlap = len(candidate & other) / max(1, len(candidate | other))
            if overlap > 0.72:
                return True
        return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
