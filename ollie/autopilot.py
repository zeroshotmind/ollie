"""Autopilot: drive the coding agent toward a goal without a human in the loop.

When armed, Ollie watches for the end of each agent turn, hands the goal plus
the agent's latest output to the local model, and either declares the goal done
or authors the next instruction and types it into the terminal — with Enter.

Because injection lands in whatever window is focused, this is fenced in:

* prompts are only injected while a terminal app is frontmost,
* a hard turn cap stops runaway loops,
* an identical prompt authored twice in a row stops the run ("stalled"),
* toggling autopilot off, or speaking a new goal, always wins.

The human can watch the orb: an amber outer ring means autopilot is armed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Callable

import httpx

from .config import Config
from .message import Chunk

log = logging.getLogger("ollie.autopilot")

SYSTEM_PROMPT = """You drive a coding agent (Claude Code) toward a goal by writing its next instruction. You are given the GOAL, the instructions ALREADY SENT, and the agent's LATEST OUTPUT.

Reply with exactly one line, nothing else:
DONE: <one short sentence of evidence>          — only if the latest output shows the goal is fully achieved.
PROMPT: <the next instruction for the agent>    — otherwise.

Rules for PROMPT:
- One concrete step, moving toward the goal. Plain imperative text, no markdown, no quotes.
- If the agent asked a question, answer it decisively, in service of the goal.
- If the agent reported an error, instruct it to diagnose and fix that error.
- If the agent seems finished with a subtask, direct it to the next one, or to verify its work.
- Never repeat an instruction that already appears in ALREADY SENT.
- Write an instruction, not narration: never describe what the agent is doing, and never copy lines from LATEST OUTPUT.
- Under 60 words.

Examples.

GOAL: make the test suite pass
LATEST OUTPUT:
agent said: Two tests still fail in test_auth — the token refresh path raises KeyError.
YOU REPLY: PROMPT: Fix the KeyError in the token refresh path, then rerun the failing test_auth tests.

GOAL: add a --json flag to the CLI
LATEST OUTPUT:
agent said: Done. The --json flag is implemented and documented, and all 31 tests pass.
YOU REPLY: DONE: the flag is implemented and the full suite passes.

GOAL: profile and speed up the import step
LATEST OUTPUT:
agent said: Should I keep backwards compatibility with the pickle cache, or drop it?
YOU REPLY: PROMPT: Drop the pickle cache compatibility, since the goal is speed; proceed with the faster loader."""

_DONE = re.compile(r"^\s*DONE\s*:?\s*(.*)", re.I)
_PROMPT = re.compile(r"^\s*PROMPT\s*:?\s*(.*)", re.I | re.S)


def parse_reply(raw: str) -> tuple[str, str]:
    """Return ("done"|"prompt"|"invalid", payload)."""
    text = (raw or "").strip().strip('"')
    match = _DONE.match(text)
    if match:
        return "done", match.group(1).strip()
    match = _PROMPT.match(text)
    if match:
        payload = " ".join(match.group(1).split())
        return ("prompt", payload[:500]) if payload else ("invalid", "")
    return "invalid", text[:120]


class Autopilot:
    def __init__(
        self,
        cfg: Config,
        inject: Callable[[str], bool],
        speak: Callable[[str], None],
        frontmost: Callable[[], str],
    ) -> None:
        self.cfg = cfg
        self._inject = inject
        self._speak = speak
        self._frontmost = frontmost
        self._client = httpx.Client(timeout=cfg.filter_timeout)

        self.enabled = False
        self.awaiting_goal = False
        self.goal = ""
        self.turns = 0
        self.sent: list[str] = []
        self._output: deque[str] = deque(maxlen=60)
        self._fresh_output = False
        self._last_activity = time.time()
        self._advancing = threading.Lock()
        self._idle_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # arming
    # ------------------------------------------------------------------
    def arm(self, goal: str = "") -> None:
        goal = (goal or "").strip()
        if not goal:
            self.enabled = True
            self.awaiting_goal = True
            log.info("autopilot armed, waiting for a goal")
            self._speak("Autopilot armed. Hold the key and tell me the goal.")
            return
        self.enabled = True
        self.set_goal(goal)

    def set_goal(self, goal: str) -> None:
        self.goal = goal.strip()
        self.awaiting_goal = False
        self.turns = 0
        self.sent = []
        self._fresh_output = False
        log.info("autopilot goal: %s", self.goal)
        self._speak("Goal set. Starting.")
        self._start_idle_watch()
        # The opening move is the goal itself, verbatim — the model only takes
        # over once there is output to react to.
        threading.Thread(target=self._send, args=(self.goal,), daemon=True).start()

    def disarm(self, reason: str = "", speak: bool = True) -> None:
        was_enabled = self.enabled
        self.enabled = False
        self.awaiting_goal = False
        if was_enabled:
            log.info("autopilot off%s", f" — {reason}" if reason else "")
            if speak:
                self._speak(f"Autopilot off. {reason}".strip())

    def toggle(self) -> bool:
        if self.enabled:
            self.disarm()
        else:
            self.arm(self.cfg.autopilot_goal)
        return self.enabled

    # ------------------------------------------------------------------
    # observation
    # ------------------------------------------------------------------
    def observe(self, chunk: Chunk) -> None:
        self._last_activity = time.time()
        if chunk.role == "turn_end":
            if self.enabled and not self.awaiting_goal:
                threading.Thread(target=self._advance, daemon=True).start()
            return
        text = chunk.text.strip()
        if text:
            self._output.append((chunk.role, text[:400]))
            self._fresh_output = True

    def _start_idle_watch(self) -> None:
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return

        def watch() -> None:
            # Fallback for turns whose end marker we never see: if the session
            # has produced output and then gone quiet, treat that as a turn end.
            while self.enabled:
                time.sleep(5.0)
                idle = time.time() - self._last_activity
                if (self.enabled and not self.awaiting_goal and self._fresh_output
                        and idle > self.cfg.autopilot_idle):
                    log.info("session idle %.0fs — treating as end of turn", idle)
                    self._advance()

        self._idle_thread = threading.Thread(target=watch, name="autopilot-idle", daemon=True)
        self._idle_thread.start()

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def _advance(self) -> None:
        if not self._advancing.acquire(blocking=False):
            return                                    # a turn is already being handled
        try:
            time.sleep(self.cfg.autopilot_settle)     # let trailing records land
            if not self.enabled or self.awaiting_goal or not self._fresh_output:
                return
            if self.turns >= self.cfg.autopilot_max_turns:
                self.disarm(f"Reached the limit of {self.cfg.autopilot_max_turns} turns.")
                return

            try:
                raw = self._ask()
            except Exception as exc:
                log.error("autopilot cannot reach the model: %s — will retry next turn", exc)
                return

            verdict, payload = parse_reply(raw)
            if verdict == "prompt":
                payload = self._sanitize(payload)
                if not payload:
                    verdict = "invalid"
            log.info("autopilot verdict: %s %s", verdict, payload[:120])
            if not self.enabled:                      # disarmed while we were thinking
                return

            if verdict == "done":
                self.disarm(speak=False)
                self._speak(f"Goal complete. {payload}")
                return
            if verdict == "invalid":
                log.warning("unusable model reply, skipping this turn: %r", payload)
                return
            if self.sent and payload.lower() == self.sent[-1].lower():
                self.disarm("I keep authoring the same instruction, so I have stalled.")
                return
            self._send(payload)
        finally:
            self._advancing.release()

    def _send(self, prompt: str) -> None:
        waited = 0.0
        warned = False
        while self.enabled:
            app = self._frontmost()
            if self._is_terminal(app):
                if self._inject(prompt):
                    self.turns += 1
                    self.sent.append(prompt)
                    self._fresh_output = False
                    log.info("autopilot turn %d/%d -> %s",
                             self.turns, self.cfg.autopilot_max_turns, prompt)
                else:
                    log.error("autopilot injection failed")
                return
            if not warned:
                log.warning("waiting to inject — frontmost app is %r, not a terminal", app)
                self._speak("I have the next step, but the terminal isn't focused.")
                warned = True
            time.sleep(3.0)
            waited += 3.0
            if waited > 180:
                self.disarm("The terminal never came back into focus.")
                return

    def _sanitize(self, payload: str) -> str:
        """Reject authored 'prompts' that are really echoes of the output."""
        cleaned = re.sub(r"\[[a-z_ ]+\]", " ", payload, flags=re.I)
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            return ""
        low = cleaned.lower()
        if low.startswith(("agent said", "agent did", "result:")):
            return ""
        for _role, text in self._output:
            if low in text.lower() and len(low) > 20:
                return ""
        return cleaned

    def _is_terminal(self, app: str) -> bool:
        wanted = [t.strip().lower() for t in self.cfg.autopilot_frontmost.split(",") if t.strip()]
        return any(t in (app or "").lower() for t in wanted)

    def _ask(self) -> str:
        model = self.cfg.autopilot_model or self.cfg.ollama_model
        sent = "\n".join(f"- {p}" for p in self.sent[-8:]) or "(nothing yet)"
        verbs = {"assistant": "agent said", "tool_use": "agent did", "tool_result": "result"}
        output = "\n".join(
            f"{verbs.get(role, role)}: {text}" for role, text in self._output
        ) or "(no output yet)"
        user = f"GOAL:\n{self.goal}\n\nALREADY SENT:\n{sent}\n\nLATEST OUTPUT:\n{output[-5000:]}"
        response = self._client.post(
            f"{self.cfg.ollama_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 160, "num_ctx": 8192},
            },
        )
        response.raise_for_status()
        return ((response.json() or {}).get("message") or {}).get("content", "") or ""

    def close(self) -> None:
        self.enabled = False
        try:
            self._client.close()
        except Exception:
            pass
