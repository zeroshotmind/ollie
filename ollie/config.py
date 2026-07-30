"""Configuration. Defaults -> ~/.ollie/config.json -> OLLIE_* env -> CLI flags."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".ollie"
CONFIG_PATH = STATE_DIR / "config.json"
SESSION_HINT_PATH = STATE_DIR / "current_session.json"
LOG_PATH = STATE_DIR / "ollie.log"
APP_LOG_PATH = STATE_DIR / "app.log"   # stdout/stderr when launched from Ollie.app

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _coerce(value, annotation: str):
    if isinstance(value, str):
        annotation = annotation.strip()
        if annotation == "bool":
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(f"cannot read {value!r} as a boolean")
        if annotation == "int":
            return int(value)
        if annotation == "float":
            return float(value)
    return value


@dataclass
class Config:
    # ---------- reader ----------
    claude_projects_dir: str = str(HOME / ".claude" / "projects")
    session_file: str = ""          # pin to one transcript; empty = auto-discover
    follow_latest: bool = True      # hop to a newer session when one appears
    from_start: bool = False        # replay the whole transcript on boot
    poll_interval: float = 0.4
    speak_tool_use: bool = True
    speak_tool_results: bool = False
    speak_sidechains: bool = False

    # ---------- narration style ----------
    # brief    — one terse line, routine steps skipped (default)
    # full     — loss-less: every fact kept, several sentences allowed
    # verbatim — the agent's own words, no model in the loop
    style: str = "brief"

    # How the narration is delivered (ignored by verbatim, which has no voice
    # of its own by definition): neutral | warm | snarky | minimal
    tone: str = "neutral"

    # ---------- filter / dedup ----------
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b-instruct"
    history_window: int = 14        # how many spoken lines the filter remembers
    filter_timeout: float = 45.0
    batch_debounce: float = 1.2     # gather this many seconds of events per pass
    max_words: int = 28

    # ---------- tts ----------
    voice: str = "Samantha"
    rate: int = 210

    # ---------- stt ----------
    whisper_repo: str = "mlx-community/whisper-base.en-mlx"
    sample_rate: int = 16000
    hotkey: str = "right option"    # or "caps lock", "f13", "cmd_r" … see ollie/hotkey.py
    hotkey_mode: str = "hold"       # hold | toggle
    max_record_seconds: float = 60.0

    # ---------- autopilot ----------
    autopilot: bool = False
    autopilot_goal: str = ""
    autopilot_max_turns: int = 15
    autopilot_idle: float = 75.0     # quiet seconds that count as end-of-turn
    autopilot_settle: float = 2.5    # wait after a turn ends before deciding
    autopilot_model: str = ""        # empty = same model as the filter
    autopilot_frontmost: str = ("Terminal,iTerm,WezTerm,kitty,Alacritty,"
                                "Ghostty,Warp,Visual Studio Code,Code,Cursor,Windsurf")

    # ---------- injection ----------
    inject_mode: str = "paste"      # paste | type
    press_enter: bool = False
    inject_delay: float = 0.10

    # ---------- orb ----------
    orb: bool = True
    orb_size: int = 130
    orb_margin: int = 28

    # ---------- misc ----------
    verbose: bool = False

    @classmethod
    def load(cls, overrides: dict | None = None) -> "Config":
        data: dict = {}

        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text())
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                pass

        for f in fields(cls):
            env = os.environ.get("OLLIE_" + f.name.upper())
            if env is not None:
                data[f.name] = env

        if overrides:
            data.update({k: v for k, v in overrides.items() if v is not None})

        known = {f.name: str(f.type) for f in fields(cls)}
        clean = {}
        for name, value in data.items():
            if name not in known:
                continue
            try:
                clean[name] = _coerce(value, known[name])
            except (TypeError, ValueError):
                pass
        return cls(**clean)

    def save(self) -> Path:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2) + "\n")
        return CONFIG_PATH
