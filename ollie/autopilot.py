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

# Used instead of SYSTEM_PROMPT when driving a pinned window with clicking
# available: an ordinary GUI takes keystrokes and clicks, not instructions.
WINDOW_SYSTEM_PROMPT = """You operate the {app} application window toward a goal, one action per turn. You are given the GOAL, the actions ALREADY SENT, a SCREENSHOT of the window as it looks right now, and any new TEXT ON SCREEN since your last action. The screenshot is the authoritative current state: read it before deciding — what is focused, what your last action changed, what is now visible.

Reply with exactly one line, nothing else:
DONE: <one short sentence of evidence>           — only if the screen shows the goal is achieved.
CLICK: <short visual description of one element> — press a button, link, tab, menu item — or focus a text box before typing.
DOUBLECLICK: <short visual description>          — open an item, or select a word.
PROMPT: <text to type>                           — literal keystrokes typed into the focused text field, then Enter.
KEY: <one keystroke>                             — e.g. KEY: escape, KEY: tab, KEY: backspace, KEY: down, KEY: cmd-a.
CLEAR:                                           — empty the focused text field (select all + delete).
SCROLL: up|down                                  — scroll the window to reveal more of the page.
WAIT:                                            — the screen is still loading or mid-animation; look again shortly.

Rules:
- {app} is an ordinary application, not an assistant. PROMPT is what a human would type, never an instruction about what to do ("Search for jazz" is wrong; CLICK the search box, then PROMPT: jazz).
- Typed text lands only in a focused text field: unless your previous action clicked a text box, CLICK it first. If the field already contains text, CLEAR before typing.
- If the goal's target is not visible in the screenshot, SCROLL toward it before clicking blindly.
- Use KEY: escape to dismiss popups or menus that block the page.
- One action per turn. CLICK descriptions name one visible element, under 15 words. PROMPT text under 30 words.
- Never repeat an action already in ALREADY SENT unless the screen shows it had no effect.

Example, step by step.

GOAL: play some jazz on YouTube
ALREADY SENT: (nothing yet)
YOU REPLY: CLICK: the search box at the top of the page

GOAL: play some jazz on YouTube
ALREADY SENT:
- CLICK the search box at the top of the page
YOU REPLY: PROMPT: jazz music

GOAL: play some jazz on YouTube
ALREADY SENT:
- CLICK the search box at the top of the page
- jazz music
TEXT ON SCREEN: Jazz Music — Smooth Jazz Radio 24/7 …
YOU REPLY: CLICK: the first search result thumbnail"""

_DONE = re.compile(r"^\s*DONE\s*:?\s*(.*)", re.I)
_PROMPT = re.compile(r"^\s*PROMPT\s*:?\s*(.*)", re.I | re.S)
_CLICK = re.compile(r"^\s*CLICK\s*:?\s*(.*)", re.I)
_DBLCLICK = re.compile(r"^\s*DOUBLE\s*-?\s*CLICK\s*:?\s*(.*)", re.I)
_KEY = re.compile(r"^\s*KEY\b\s*:?\s*(.*)", re.I)
_CLEAR = re.compile(r"^\s*CLEAR\b\s*:?\s*$", re.I)
_SCROLL = re.compile(r"^\s*SCROLL\b\s*:?\s*(.*)", re.I)
_WAIT = re.compile(r"^\s*WAIT\b\s*:?", re.I)


def parse_reply(raw: str) -> tuple[str, str]:
    """Return ("done"|"prompt"|"invalid", payload)."""
    text = (raw or "").strip().strip('"')
    match = _DONE.match(text)
    if match:
        return "done", match.group(1).strip()
    match = _DBLCLICK.match(text)
    if match:
        payload = " ".join(match.group(1).split())
        return ("doubleclick", payload[:120]) if payload else ("invalid", "")
    match = _CLICK.match(text)
    if match:
        payload = " ".join(match.group(1).split())
        return ("click", payload[:120]) if payload else ("invalid", "")
    if _CLEAR.match(text):
        return "clear", ""
    if _WAIT.match(text):
        return "wait", ""
    match = _KEY.match(text)
    if match:
        payload = " ".join(match.group(1).split())
        return ("key", payload[:40]) if payload else ("invalid", "")
    match = _SCROLL.match(text)
    if match:
        direction = match.group(1).strip().lower()
        direction = "up" if direction.startswith("up") else "down"
        return "scroll", direction
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
        self.extra_app = ""    # app of a pinned window — also a legal target
        self.prepare = None    # callable that focuses the pinned window first
        self.click = None      # callable(description, double=False), window mode
        self.key = None        # callable("escape"/"cmd-a"/…) -> bool
        self.clear = None      # callable() -> bool: empty the focused field
        self.scroll = None     # callable("up"|"down") -> bool
        self.status = lambda text: None   # live "what am I doing" for the bubble
        self.window_shot = None  # callable -> (png, w, h) | None: current window
        self._failures = 0       # consecutive turns that produced no action
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
        self._failures = 0
        # A new goal starts from a clean slate: judging it against output
        # observed under the *previous* goal declares instant false victory.
        self._output.clear()
        log.info("autopilot goal: %s (target: %s)", self.goal, self.extra_app or "terminal")
        self._speak(f"Goal set. Driving {self.extra_app}." if self.extra_app
                    else "Goal set. Starting.")
        self._start_idle_watch()
        if self.extra_app:
            # Driving a pinned window (a chat, another tool): the spoken goal
            # is an instruction *about* the conversation, not its first line —
            # let the model author the opening instead of sending it verbatim.
            self._fresh_output = True
            threading.Thread(target=self._advance, daemon=True).start()
        else:
            # A coding agent takes the goal itself as the first prompt.
            self._fresh_output = False
            threading.Thread(target=self._send, args=(self.goal,), daemon=True).start()

    def reset_observations(self) -> None:
        """Forget observed output — the narration source changed underneath us."""
        self._output.clear()
        self._fresh_output = False

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
        text = self._strip_own_echo(chunk.text.strip())
        if text:
            self._output.append((chunk.role, text[:400]))
            self._fresh_output = True

    def _strip_own_echo(self, text: str) -> str:
        """Drop lines that are our own sent messages coming back off the
        screen. In a chat window everything we type reappears as 'output';
        without this the model converses with itself and loops."""
        if not text or not self.sent:
            return text
        recent = [s.lower() for s in self.sent[-5:]]
        kept = []
        for line in text.splitlines():
            low = " ".join(line.lower().split())
            if any(low == s or (len(low) > 12 and low in s)
                   or (len(low) > 20 and s in low) for s in recent):
                continue
            kept.append(line)
        return "\n".join(kept).strip()

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

            self.status("Autopilot: reading the screen, deciding the next step…")
            try:
                raw = self._ask()
            except Exception as exc:
                log.error("autopilot cannot reach the model: %s — will retry next turn", exc)
                self.status("")
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
                self._note_failure()
                return
            if verdict in ("click", "doubleclick"):
                if self.click is None:
                    log.warning("model asked to click but clicking is unavailable")
                    return
                self._do_click(payload, double=(verdict == "doubleclick"))
                return
            if verdict == "wait":
                log.info("autopilot waiting — screen not settled")
                self._fresh_output = True
                threading.Timer(4.0, self._advance).start()
                return
            if verdict in ("key", "clear", "scroll"):
                self._do_simple(verdict, payload)
                return
            if self.sent and payload.lower() == self.sent[-1].lower():
                self.disarm("I keep authoring the same instruction, so I have stalled.")
                return
            self._send(payload)
        finally:
            self.status("")
            self._advancing.release()

    def _note_failure(self) -> None:
        """A turn produced no action. In window mode the loop only breathes
        through action-scheduled timers, so a dropped turn must reschedule
        itself — and give up cleanly if it keeps happening."""
        self._failures += 1
        if self._failures >= 5:
            self.disarm("I failed to act five times in a row.")
            return
        if self.extra_app:
            threading.Timer(2.5, self._advance).start()

    def _do_simple(self, kind: str, payload: str) -> None:
        """KEY / CLEAR / SCROLL: focus the pinned window, fire the injector
        primitive, and keep the loop breathing like every other action."""
        action = {"key": self.key, "clear": self.clear, "scroll": self.scroll}[kind]
        if action is None:
            log.warning("model asked for %s but it is unavailable", kind)
            self._note_failure()
            return
        marker = f"{kind.upper()} {payload}".strip()
        if self.sent and self.sent[-1].lower() == marker.lower():
            self.disarm(f"I keep repeating the same {kind} action, so I have stalled.")
            return
        self.status(f"Autopilot: {kind} {payload}".strip() + "…")
        if self.prepare is not None:
            try:
                self.prepare()
                time.sleep(0.35)
            except Exception:
                log.exception("prepare-focus failed")
        ok = action(payload) if payload else action()
        if ok:
            self._failures = 0
            self.turns += 1
            self.sent.append(marker)
            log.info("autopilot turn %d/%d -> %s",
                     self.turns, self.cfg.autopilot_max_turns, marker)
            self._fresh_output = True
            threading.Timer(3.0, self._advance).start()
        else:
            log.error("autopilot %s action failed", kind)
            self._note_failure()

    def _do_click(self, description: str, double: bool = False) -> None:
        marker = f"{'DOUBLECLICK' if double else 'CLICK'} {description}"
        if self.sent and self.sent[-1].lower() == marker.lower():
            self.disarm("I keep clicking the same element, so I have stalled.")
            return
        self.status(f"Autopilot: looking for {description}…")
        if self.prepare is not None:
            try:
                self.prepare()
                time.sleep(0.35)
            except Exception:
                log.exception("prepare-focus failed")
        if self.click(description, double=double):
            self._failures = 0
            self.turns += 1
            self.sent.append(marker)
            log.info("autopilot turn %d/%d -> click %s",
                     self.turns, self.cfg.autopilot_max_turns, description)
            # A click often changes nothing textual (focusing a text box), so
            # no turn_end will ever arrive — take the next turn on a timer.
            # The screen gets a moment to react first; anything it prints in
            # the meantime is observed as usual and simply enriches that turn.
            self._fresh_output = True
            threading.Timer(3.0, self._advance).start()
        else:
            log.error("autopilot could not find %r to click", description)
            self._note_failure()

    def _send(self, prompt: str) -> None:
        waited = 0.0
        warned = False
        while self.enabled:
            if self.prepare is not None:
                # narrating a pinned window: bring it to the front so the
                # injection lands there, not in whatever happens to be focused
                try:
                    self.prepare()
                    time.sleep(0.35)
                except Exception:
                    log.exception("prepare-focus failed")
            app = self._frontmost()
            if self._is_terminal(app):
                self.status(f"Autopilot: typing step {self.turns + 1}…")
                if self._inject(prompt):
                    self._failures = 0
                    self.turns += 1
                    self.sent.append(prompt)
                    log.info("autopilot turn %d/%d -> %s",
                             self.turns, self.cfg.autopilot_max_turns, prompt)
                    if self.extra_app:
                        # Driving a GUI: the app may react without printing
                        # anything the reader counts as new (our own typed
                        # text is echo-stripped; a search box just renders
                        # suggestions), and then no turn-end ever fires. Keep
                        # the observe-act loop breathing on a timer, exactly
                        # like clicks do. Stall/turn caps still apply.
                        self._fresh_output = True
                        threading.Timer(5.0, self._advance).start()
                    else:
                        self._fresh_output = False
                else:
                    log.error("autopilot injection failed")
                return
            if not warned:
                log.warning("waiting to inject — frontmost app is %r, not a terminal", app)
                self._speak("I have the next step, but the terminal isn't focused.")
                warned = True
            self.status("Autopilot: waiting for the target window to come into focus…")
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
        extra = (self.extra_app or "").strip().lower()
        if extra and extra in (app or "").lower():
            # narrating a pinned window: its own app is always a legal target
            return True
        return any(t in (app or "").lower() for t in wanted)

    def _ask(self) -> str:
        model = self.cfg.autopilot_model or self.cfg.ollama_model
        sent = "\n".join(f"- {p}" for p in self.sent[-8:]) or "(nothing yet)"
        verbs = {"assistant": "agent said", "tool_use": "agent did", "tool_result": "result"}
        output = "\n".join(
            f"{verbs.get(role, role)}: {text}" for role, text in self._output
        ) or "(no output yet)"
        images = None
        if self.click is not None and self.extra_app:
            system = WINDOW_SYSTEM_PROMPT.format(app=self.extra_app)
            user = (f"GOAL:\n{self.goal}\n\nALREADY SENT:\n{sent}"
                    f"\n\nTEXT ON SCREEN:\n{output[-5000:]}")
            # decide from what the window actually looks like, not text deltas
            shot = self.window_shot() if self.window_shot is not None else None
            if shot is not None:
                import base64

                png, w, h = shot
                images = [base64.b64encode(png).decode()]
                user += f"\n\n(the attached screenshot is {w}x{h} pixels)"
                if self.cfg.autopilot_vision_model:
                    model = self.cfg.autopilot_vision_model
        else:
            system = SYSTEM_PROMPT
            user = (f"GOAL:\n{self.goal}\n\nALREADY SENT:\n{sent}"
                    f"\n\nLATEST OUTPUT:\n{output[-5000:]}")
        user_msg = {"role": "user", "content": user}
        if images:
            user_msg["images"] = images
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                user_msg,
            ],
            "stream": False,
            # Thinking models (qwen3 family) otherwise spend the whole token
            # budget on reasoning and return a truncated instruction.
            "think": False,
            "options": {"temperature": 0.3, "num_predict": 220, "num_ctx": 8192},
        }
        response = self._client.post(f"{self.cfg.ollama_url}/api/chat", json=body)
        if response.status_code == 400:
            # some models reject the think flag — retry without it
            body.pop("think", None)
            response = self._client.post(f"{self.cfg.ollama_url}/api/chat", json=body)
        response.raise_for_status()
        message = (response.json() or {}).get("message") or {}
        content = message.get("content", "") or ""
        content = re.sub(r"<think>.*?(</think>|$)", "", content, flags=re.S).strip()
        if not content:
            # Some thinking models (qwen3-vl) ignore "think": false and burn
            # the whole budget reasoning, leaving content empty. The decision
            # is usually sitting fully-formed inside the reasoning — take the
            # last well-formed action line rather than dropping the turn.
            thinking = message.get("thinking", "") or ""
            lines = re.findall(r"(?:DONE|PROMPT|CLICK)\s*:\s*[^\n\"]+", thinking)
            if lines:
                content = lines[-1].strip()
                log.info("salvaged verdict from thinking stream: %r", content[:80])
        return content

    def close(self) -> None:
        self.enabled = False
        try:
            self._client.close()
        except Exception:
            pass
