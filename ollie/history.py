"""Bounded on-disk trajectory of everything that crosses the voice bridge.

One JSONL file, one event per line:

    {"ts": ..., "type": "narration", "text": "...", "source": "window:...",
     "source_label": "Terminal — ollie", ...}

Event types:
    narration   a line Ollie spoke (or showed in the caption while muted)
    speech      something the user said, and where it was injected
    autopilot   a turn autopilot authored and sent
    source      the narration source changed (window pinned, transcript, …)

The file is capped: once it grows past ~1.2× the configured maximum it is
rewritten keeping the newest events, so it never overflows no matter how
long Ollie runs. Small enough to grep, structured enough to replay.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from .config import STATE_DIR, Config

log = logging.getLogger("ollie.history")

HISTORY_PATH = STATE_DIR / "history.jsonl"


class History:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cap = max(100, int(getattr(cfg, "history_max_events", 2000)))
        self._lock = threading.Lock()
        self._count = self._initial_count()

    def _initial_count(self) -> int:
        try:
            with HISTORY_PATH.open("r", encoding="utf-8") as fh:
                return sum(1 for _ in fh)
        except FileNotFoundError:
            return 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    def record(self, event_type: str, text: str, **meta) -> None:
        """Append one event. Never raises — history must not break narration."""
        event = {"ts": round(time.time(), 3), "type": event_type,
                 "text": (text or "")[:600], **meta}
        try:
            with self._lock:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                with HISTORY_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._count += 1
                if self._count > self.cap * 1.2:
                    self._trim()
        except Exception:
            log.debug("could not record history event", exc_info=True)

    def _trim(self) -> None:
        """Rewrite the file keeping only the newest ``cap`` events."""
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        keep = lines[-self.cap:]
        tmp = HISTORY_PATH.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(HISTORY_PATH)
        self._count = len(keep)


# ----------------------------------------------------------------------
def tail(n: int = 50) -> list[dict]:
    """The newest ``n`` events, oldest first (for --history and debugging)."""
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _group(events: list[dict]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    groups: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    for e in events:
        key = e.get("source", "?")
        groups.setdefault(key, []).append(e)
        if e.get("source_label"):
            labels[key] = e["source_label"]
    return groups, labels


def render(events: list[dict]) -> str:
    """Human-readable trajectory, grouped per source conversation.

    Every source (the Claude Code session, each pinned window) is its own
    section in chronological order, so switching windows reads as separate
    conversations rather than one interleaved stream.
    """
    glyphs = {"narration": "🔊", "speech": "🎤", "autopilot": "🤖", "source": "▸"}
    groups, labels = _group(events)

    sections = []
    # order sections by their first event, so the trajectory reads forward
    for key, items in sorted(groups.items(), key=lambda kv: kv[1][0].get("ts", 0)):
        rows = [f"── {labels.get(key, key)} " + "─" * 20]
        for e in items:
            if e.get("type") == "source":
                continue                       # the header already says it
            when = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            glyph = glyphs.get(e.get("type", ""), "·")
            target = f" → {e['target']}" if e.get("target") else ""
            rows.append(f"  {when} {glyph} {e.get('text', '')}{target}")
        if len(rows) > 1:
            sections.append("\n".join(rows))
    return "\n\n".join(sections) or "(no history yet)"


# ----------------------------------------------------------------------
# browser view (orb menu → History…)
# ----------------------------------------------------------------------
def render_html(events: list[dict]) -> str:
    import html as _html

    groups, labels = _group(events)
    css_class = {"narration": "ollie", "speech": "user", "autopilot": "pilot"}
    side_name = {"narration": "Ollie", "speech": "You", "autopilot": "Autopilot"}

    nav_items, panes = [], []
    ordered = sorted(groups.items(), key=lambda kv: kv[1][-1].get("ts", 0), reverse=True)
    shown = 0
    for key, items in ordered:
        msgs = [e for e in items if e.get("type") != "source"]
        if not msgs:
            continue
        cid = f"conv{shown}"
        title = labels.get(key, key)
        # "window reader — Terminal — ollie" → "Terminal — ollie"
        short = title.split(" — ", 1)[1] if title.startswith("window reader — ") else title
        last = time.strftime("%b %d, %H:%M", time.localtime(msgs[-1].get("ts", 0)))
        nav_items.append(
            f"<div class='conv{' active' if shown == 0 else ''}' data-pane='{cid}'>"
            f"<div class='conv-name'>{_html.escape(short)}</div>"
            f"<div class='conv-sub'>{len(msgs)} events · {last}</div></div>")

        bubbles = []
        for e in msgs:
            kind = css_class.get(e.get("type", ""), "ollie")
            when = time.strftime("%b %d, %H:%M:%S", time.localtime(e.get("ts", 0)))
            target = f" → {_html.escape(e['target'])}" if e.get("target") else ""
            muted = " · muted" if e.get("muted") else ""
            bubbles.append(
                f"<div class='msg {kind}'><div class='bubble'>"
                f"{_html.escape(e.get('text', ''))}"
                f"<div class='meta'>{side_name.get(e.get('type'), '')} · {when}{target}{muted}</div>"
                f"</div></div>")
        panes.append(f"<div class='thread{' active' if shown == 0 else ''}' id='{cid}'>"
                     f"{''.join(bubbles)}</div>")
        shown += 1

    stamp = time.strftime("%Y-%m-%d %H:%M")
    nav = "".join(nav_items) or "<p class='empty'>No history yet.</p>"
    body = "".join(panes) or "<div class='thread active'><p class='empty'>No history yet — say something.</p></div>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Ollie history</title>
<style>
 * {{ box-sizing:border-box; }}
 body {{ background:#0d1117; color:#e6e8ee; font: 14px/1.5 -apple-system, sans-serif;
        margin:0; height:100vh; display:flex; flex-direction:column; }}
 header {{ padding:16px 24px 12px; border-bottom:1px solid #232a36; flex:none; }}
 h1 {{ font-size:19px; margin:0; }} h1 .orb {{ color:#8f7ae5; }}
 .stamp {{ color:#566; font-size:11.5px; margin-top:2px; }}
 main {{ flex:1; display:flex; min-height:0; }}
 nav {{ width:270px; flex:none; overflow-y:auto; border-right:1px solid #232a36;
       padding:10px; }}
 .conv {{ padding:10px 12px; border-radius:10px; cursor:pointer; margin-bottom:4px; }}
 .conv:hover {{ background:#161d2a; }}
 .conv.active {{ background:#1c2333; border:1px solid #2a3347; }}
 .conv-name {{ font-weight:600; color:#c9cede; overflow:hidden; text-overflow:ellipsis;
              white-space:nowrap; }}
 .conv-sub {{ color:#8b93a7; font-size:11.5px; }}
 .thread {{ display:none; flex:1; overflow-y:auto; padding:18px 24px;
           flex-direction:column; gap:8px; }}
 .thread.active {{ display:flex; }}
 .msg {{ display:flex; flex:none; }}
 .msg.user {{ justify-content:flex-end; }}
 .bubble {{ max-width:70%; padding:9px 13px; border-radius:14px; white-space:pre-wrap;
           overflow-wrap:break-word; }}
 .ollie .bubble {{ background:#1c2333; border:1px solid #2a3347;
                  border-bottom-left-radius:4px; }}
 .user .bubble {{ background:#3d3566; border:1px solid #55499a;
                 border-bottom-right-radius:4px; }}
 .pilot .bubble {{ background:#33271a; border:1px solid #5a4426;
                  border-bottom-left-radius:4px; }}
 .meta {{ color:#8b93a7; font-size:11px; margin-top:5px; }}
 .empty {{ color:#8b93a7; padding:12px; }}
</style></head><body>
<header>
 <h1><span class="orb">🔮</span> Ollie — conversation history</h1>
 <div class="stamp">Generated {stamp} · newest conversation first · reopen from the orb menu to refresh</div>
</header>
<main>
 <nav>{nav}</nav>
 {body}
</main>
<script>
 const scrollBottom = t => {{ if (t) t.scrollTop = t.scrollHeight; }};
 document.querySelectorAll('.conv').forEach(c => c.addEventListener('click', () => {{
   document.querySelectorAll('.conv').forEach(x => x.classList.remove('active'));
   document.querySelectorAll('.thread').forEach(x => x.classList.remove('active'));
   c.classList.add('active');
   const pane = document.getElementById(c.dataset.pane);
   pane.classList.add('active');
   scrollBottom(pane);
 }}));
 scrollBottom(document.querySelector('.thread.active'));
</script>
</body></html>"""


def open_page(n: int = 500) -> None:
    import subprocess

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "history.html"
    path.write_text(render_html(tail(n)), encoding="utf-8")
    subprocess.Popen(["open", str(path)])
