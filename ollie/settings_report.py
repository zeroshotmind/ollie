"""The Ollie settings & dependency report.

One honest page answering: what is Ollie built on, which models does it run,
what permissions does it hold, and is each piece currently healthy. Everything
is gathered live — nothing here is hardcoded status.

Rendered two ways: plain text (`ollie --settings`) and a small self-contained
HTML page (orb menu → Settings), written to ~/.ollie/settings.html.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from importlib import metadata
from pathlib import Path

import httpx

from . import __version__
from .config import CONFIG_PATH, LOG_PATH, STATE_DIR, Config

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except Exception:
        return "not installed"


def _hf_cache_size(repo: str) -> str:
    """Is this Hugging Face model downloaded, and how big is it?"""
    slug = "models--" + repo.replace("/", "--")
    for base in (Path.home() / ".cache" / "huggingface" / "hub",):
        path = base / slug
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            return f"{total / 1e6:.0f} MB on disk"
    return ""


def _ollama_models(cfg: Config) -> tuple[bool, dict[str, str]]:
    try:
        response = httpx.get(f"{cfg.ollama_url}/api/tags", timeout=3.0)
        response.raise_for_status()
        models = {
            m.get("name", ""): f"{(m.get('size') or 0) / 1e9:.1f} GB"
            for m in response.json().get("models", [])
        }
        return True, models
    except Exception:
        return False, {}


def _row(name, detail, status, note=""):
    return {"name": name, "detail": detail, "status": status, "note": note}


def gather(cfg: Config) -> list[dict]:
    from .injector import accessibility_trusted
    from .permissions import AUTHORIZED, input_monitoring_status, microphone_status
    from .readers.claude_code import ClaudeCodeReader

    sections: list[dict] = []

    # ---- models ------------------------------------------------------
    up, models = _ollama_models(cfg)
    filter_model = cfg.ollama_model
    pilot_model = cfg.autopilot_model or cfg.ollama_model

    whisper_cache = _hf_cache_size(cfg.whisper_repo)
    rows = [
        _row("Speech-to-text", f"mlx-whisper · {cfg.whisper_repo}",
             OK if whisper_cache else WARN,
             whisper_cache or "model not downloaded yet (fetched on first run)"),
        _row("Narration filter", f"Ollama · {filter_model}",
             OK if up and filter_model in models else (WARN if up else BAD),
             (models.get(filter_model, "model not pulled") if up
              else f"Ollama unreachable at {cfg.ollama_url}")),
        _row("Autopilot author", f"Ollama · {pilot_model}",
             OK if up and pilot_model in models else (WARN if up else BAD),
             models.get(pilot_model, "model not pulled" if up else "needs Ollama")),
    ]
    if cfg.tts_engine == "kokoro":
        kokoro_cache = _hf_cache_size(cfg.kokoro_model)
        rows.append(_row("Text-to-speech", f"Kokoro (mlx-audio) · {cfg.kokoro_model}",
                         OK if kokoro_cache else WARN,
                         f"voice {cfg.kokoro_voice} · {kokoro_cache or 'model not downloaded yet'}"))
    else:
        rows.append(_row("Text-to-speech", "macOS `say` (built in)",
                         OK if shutil.which("say") else BAD,
                         f"voice {cfg.voice} at {cfg.rate} wpm"))
    sections.append({"title": "Models — all local, nothing leaves this machine",
                     "rows": rows})

    # ---- macOS integration -------------------------------------------
    listen = input_monitoring_status()
    mic = microphone_status()
    ax = accessibility_trusted()
    # TCC grants are per-process: run from a terminal this shows the
    # terminal's grants; opened from the orb it shows Ollie.app's.
    sections.append({"title": "macOS APIs & permissions (for this process)", "rows": [
        _row("Input Monitoring", "Quartz event tap — hears the push-to-talk key",
             OK if listen == AUTHORIZED else BAD, listen),
        _row("Accessibility", "CGEvent + pasteboard — types text into the terminal",
             OK if ax else BAD, "granted" if ax else "not granted"),
        _row("Microphone", "AVFoundation capture while the talk key is held",
             OK if mic == AUTHORIZED else BAD, mic),
        _row("Orb window", "AppKit non-activating panel, never takes focus", INFO, ""),
    ]})

    # ---- sources ------------------------------------------------------
    reader = ClaudeCodeReader(cfg)
    active = reader.pick_session()
    count = len(reader.list_sessions(limit=200))
    sections.append({"title": "Sources", "rows": [
        _row("Claude Code transcripts", str(reader.projects_dir),
             OK if active else WARN,
             f"{count} session files · active: {active.name if active else 'none found'}"),
    ]})

    # ---- current settings --------------------------------------------
    sections.append({"title": "Current settings", "rows": [
        _row("Narration style", cfg.style, INFO, "orb menu, or --style"),
        _row("Tone", cfg.tone, INFO, "orb menu, or --tone"),
        _row("Push-to-talk", f"{cfg.hotkey} ({cfg.hotkey_mode})", INFO, "--hotkey / --hotkey-mode"),
        _row("Autopilot", f"cap {cfg.autopilot_max_turns} turns · idle {cfg.autopilot_idle:.0f}s",
             INFO, "orb menu, or --autopilot --goal"),
        _row("Config file", str(CONFIG_PATH), INFO, "flags > config.json > defaults"),
        _row("Logs", str(LOG_PATH), INFO, ""),
    ]})

    # ---- runtime ------------------------------------------------------
    sections.append({"title": "Runtime", "rows": [
        _row("Ollie", __version__, INFO, ""),
        _row("Python", platform.python_version(), INFO, ""),
        _row("mlx-whisper", _version("mlx-whisper"), INFO, ""),
        _row("mlx-audio", _version("mlx-audio"), INFO, "kokoro engine"),
        _row("httpx", _version("httpx"), INFO, "talks to Ollama"),
        _row("pyobjc-core", _version("pyobjc-core"), INFO, "macOS bridge"),
        _row("sounddevice", _version("sounddevice"), INFO, "audio I/O"),
    ]})
    return sections


# ----------------------------------------------------------------------
_MARK = {OK: "✓", WARN: "!", BAD: "✗", INFO: "·"}


def render_text(sections: list[dict]) -> str:
    out = ["Ollie — settings & dependencies\n"]
    for section in sections:
        out.append(section["title"])
        for r in section["rows"]:
            note = f"  — {r['note']}" if r["note"] else ""
            out.append(f"  {_MARK[r['status']]}  {r['name']:22} {r['detail']}{note}")
        out.append("")
    return "\n".join(out)


_COLORS = {OK: "#3ddc84", WARN: "#f5b942", BAD: "#ff5d5d", INFO: "#8b93a7"}


def render_html(sections: list[dict]) -> str:
    body = []
    for section in sections:
        rows = "".join(
            f"<tr><td class='dot' style='color:{_COLORS[r['status']]}'>●</td>"
            f"<td class='name'>{r['name']}</td><td>{r['detail']}"
            + (f"<div class='note'>{r['note']}</div>" if r["note"] else "")
            + "</td></tr>"
            for r in section["rows"]
        )
        body.append(f"<h2>{section['title']}</h2><table>{rows}</table>")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Ollie settings</title>
<style>
 body {{ background:#0d1117; color:#e6e8ee; font: 14px/1.5 -apple-system, sans-serif;
        max-width: 760px; margin: 40px auto; padding: 0 20px; }}
 h1 {{ font-size: 22px; }} h1 .orb {{ color:#8f7ae5; }}
 h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
      color:#8b93a7; margin-top: 28px; border-bottom: 1px solid #232a36;
      padding-bottom: 6px; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td {{ padding: 6px 10px 6px 0; vertical-align: top; }}
 td.dot {{ width: 18px; }} td.name {{ width: 190px; color:#c9cede; font-weight:600; }}
 .note {{ color:#8b93a7; font-size: 12.5px; }}
 .stamp {{ color:#566; font-size: 12px; margin-top: 30px; }}
</style></head><body>
<h1><span class="orb">🔮</span> Ollie — settings &amp; dependencies</h1>
{''.join(body)}
<p class="stamp">Generated {stamp} · refresh from the orb menu · everything above runs on this Mac</p>
</body></html>"""


def write_html(cfg: Config) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "settings.html"
    path.write_text(render_html(gather(cfg)))
    return path


def open_report(cfg: Config) -> None:
    subprocess.Popen(["open", str(write_html(cfg))])
