"""Reader for Claude Code.

Claude Code writes every session to a JSONL transcript under
``~/.claude/projects/<slugified-cwd>/<session-id>.jsonl``. That file is
structured, append-only, and free of ANSI escapes and terminal redraws — which
is why we tail it instead of scraping the terminal.

Relevant record shapes (Claude Code 2.x)::

    {"type": "assistant", "uuid": ..., "isSidechain": false,
     "message": {"role": "assistant",
                 "content": [{"type": "thinking", ...},
                             {"type": "text", "text": "..."},
                             {"type": "tool_use", "id": ..., "name": "Bash",
                              "input": {"command": "..."}}]}}

    {"type": "user", "message": {"role": "user",
     "content": [{"type": "tool_result", "tool_use_id": ..., "content": ...}]}}

We emit assistant prose and (optionally) a one-line description of each tool
call. Thinking blocks are never spoken.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path

from ..config import SESSION_HINT_PATH, Config
from ..message import Chunk
from .base import Reader

log = logging.getLogger("ollie.reader")

# A transcript that was created this recently is treated as a fresh session, so
# we read it from the top. Anything older we join at the tail.
FRESH_SESSION_SECONDS = 120.0
SEEN_LIMIT = 4000


def _basename(path: str | None) -> str:
    if not path:
        return "a file"
    return Path(str(path)).name


def _first_line(text: str | None, limit: int = 180) -> str:
    if not text:
        return ""
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line[:limit]


_ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_NOISE_WORDS = ("nohup", "sudo", "time", "exec", "command")
# Programs whose subcommand is the interesting part.
_SUBCOMMAND = {"git", "npm", "pnpm", "yarn", "uv", "pip", "cargo", "docker", "make", "brew"}
_INSPECT = {"cat", "head", "tail", "less", "more", "bat"}


def summarize_command(command: str | None) -> str:
    """Turn a shell command into something a person would say out loud.

    Small models parrot whatever you hand them, so the raw command never
    reaches the filter: ``cd /repo && KMP_DUPLICATE_LIB_OK=TRUE .venv/bin/python
    -c "import torch; ..."`` becomes ``running a python snippet``.
    """
    line = _first_line(command, 400)
    if not line:
        return "running a command"

    segments = [s.strip() for s in re.split(r"&&|\|\||;", line) if s.strip()]
    meaningful = [s for s in segments if not s.startswith(("cd ", "cd\t"))]
    segment = (meaningful or segments or [line])[0]
    segment = _ENV_PREFIX.sub("", segment).strip()

    try:
        import shlex

        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    while tokens and tokens[0] in _NOISE_WORDS:
        tokens.pop(0)
    if not tokens:
        return "running a command"

    program = Path(tokens[0]).name
    args = tokens[1:]

    if program.startswith("python"):
        if "-c" in args:
            return "running a python snippet"
        if "-m" in args:
            index = args.index("-m")
            if index + 1 < len(args):
                return f"running python module {args[index + 1]}"
        script = next((a for a in args if a.endswith(".py")), None)
        return f"running {_basename(script)}" if script else "running python"

    if program in _INSPECT:
        target = next((a for a in args if not a.startswith("-")), None)
        return f"checking {_basename(target)}" if target else f"running {program}"

    if program == "ls":
        return "listing files"

    if program in _SUBCOMMAND:
        positionals = [a for a in args if not a.startswith("-")]
        # "npm run build" needs two words; "git commit -m msg" needs one.
        take = 2 if positionals[:1] == ["run"] else 1
        return " ".join(["running", program, *positionals[:take]]).strip()

    positional = next((a for a in args if not a.startswith("-")), "")
    if positional and len(positional) < 40:
        return f"running {program} {_basename(positional)}"
    return f"running {program}"


def describe_tool(block: dict) -> str:
    """Turn a tool_use block into a short, speakable phrase.

    Deliberately factual and terse — the Ollama filter downstream decides
    whether it is worth saying at all and may condense it further.
    """
    name = block.get("name") or "a tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}

    if name == "Bash":
        return summarize_command(inp.get("command"))
    if name == "Read":
        return f"reading {_basename(inp.get('file_path'))}"
    if name == "Write":
        return f"writing {_basename(inp.get('file_path'))}"
    if name in ("Edit", "MultiEdit"):
        return f"editing {_basename(inp.get('file_path'))}"
    if name == "NotebookEdit":
        return f"editing notebook {_basename(inp.get('notebook_path'))}"
    if name == "Glob":
        return f"looking for files matching {str(inp.get('pattern', ''))[:40]}"
    if name == "Grep":
        return f"searching the code for {str(inp.get('pattern', ''))[:40]}"
    if name in ("Task", "Agent"):
        detail = inp.get("description") or _first_line(inp.get("prompt"), 120)
        return f"delegating a subtask: {detail}"
    if name == "WebSearch":
        return f"searching the web for {inp.get('query', '')}"
    if name == "WebFetch":
        return "fetching a web page"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "updating the task list"
    if name == "ExitPlanMode":
        return "presenting a plan for approval"
    if isinstance(name, str) and name.startswith("mcp__"):
        parts = [p for p in name.split("__") if p]
        server = parts[1] if len(parts) > 1 else "a service"
        action = parts[-1].replace("_", " ") if parts else "an action"
        return f"calling {server}: {action}"
    return f"using the {name} tool"


def _flatten_tool_result(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p).strip()
    return ""


class ClaudeCodeReader(Reader):
    name = "claude-code"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.projects_dir = Path(cfg.claude_projects_dir).expanduser()
        self.path: Path | None = None
        self.offset = 0
        self._buf = ""
        self._ident: tuple | None = None
        self._head = b""
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._last_scan = 0.0

    # ------------------------------------------------------------------
    # session discovery
    # ------------------------------------------------------------------
    def _hinted_session(self) -> Path | None:
        """Path written by the optional SessionStart hook (scripts/session_hook.py)."""
        try:
            data = json.loads(SESSION_HINT_PATH.read_text())
            path = Path(str(data["transcript_path"])).expanduser()
        except Exception:
            return None
        try:
            if path.is_file() and time.time() - path.stat().st_mtime < 12 * 3600:
                return path
        except OSError:
            pass
        return None

    def _newest_transcript(self) -> Path | None:
        if not self.projects_dir.is_dir():
            return None
        best: Path | None = None
        best_mtime = -1.0
        for path in self.projects_dir.glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        return best

    def pick_session(self) -> Path | None:
        if self.cfg.session_file:
            path = Path(self.cfg.session_file).expanduser()
            return path if path.is_file() else None
        return self._hinted_session() or self._newest_transcript()

    def list_sessions(self, limit: int = 15) -> list[tuple[Path, float]]:
        if not self.projects_dir.is_dir():
            return []
        found = []
        for path in self.projects_dir.glob("*/*.jsonl"):
            try:
                found.append((path, path.stat().st_mtime))
            except OSError:
                continue
        found.sort(key=lambda item: item[1], reverse=True)
        return found[:limit]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._attach(self.pick_session(), from_start=self.cfg.from_start)

    def _attach(self, path: Path | None, from_start: bool) -> None:
        self.path = path
        self._buf = ""
        self.offset = 0
        self._ident = None
        self._head = b""
        if path is None:
            return
        try:
            stat = path.stat()
        except OSError:
            self.path = None
            return
        self._ident = (stat.st_dev, stat.st_ino)
        self._head = self._read_head(path)
        self.offset = 0 if from_start else stat.st_size
        log.info("attached to %s (%s)", path, "from start" if from_start else "tailing")

    @staticmethod
    def _read_head(path: Path, size: int = 256) -> bytes:
        try:
            with path.open("rb") as handle:
                return handle.read(size)
        except OSError:
            return b""

    def _rewind(self) -> None:
        self.offset = 0
        self._buf = ""

    def _is_fresh(self, path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        created = getattr(stat, "st_birthtime", stat.st_ctime)
        return (time.time() - created) < FRESH_SESSION_SECONDS

    def describe(self) -> str:
        return f"claude-code:{self.path.name}" if self.path else "claude-code:(no session)"

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    def poll(self) -> list[Chunk]:
        now = time.time()
        if not self.cfg.session_file and now - self._last_scan > 2.0:
            self._last_scan = now
            candidate = self.pick_session()
            if candidate is not None and candidate != self.path:
                if self.path is None or self.cfg.follow_latest:
                    # A brand-new session gets replayed from the top so we do not
                    # miss the opening turn; an older one we simply join at the tail.
                    self._attach(candidate, from_start=self._is_fresh(candidate))

        if self.path is None:
            return []

        try:
            stat = self.path.stat()
        except OSError:
            self.path = None
            return []
        size = stat.st_size

        # Three ways a transcript can stop being the file we were reading:
        # replaced (new inode), truncated (smaller), or rewritten in place
        # (same size or larger but different opening bytes).
        ident = (stat.st_dev, stat.st_ino)
        head = self._read_head(self.path)
        overlap = min(len(head), len(self._head))
        replaced = self._ident is not None and ident != self._ident
        rewritten = bool(overlap) and head[:overlap] != self._head[:overlap]
        if replaced or rewritten or size < self.offset:
            log.info("transcript changed underneath us — rereading from the top")
            self._rewind()
        self._ident = ident
        if len(head) >= len(self._head) or replaced or rewritten:
            self._head = head

        if size == self.offset:
            return []

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                data = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []

        self._buf += data
        *lines, self._buf = self._buf.split("\n")

        chunks: list[Chunk] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                chunks.extend(self._parse(record))
        return chunks

    # ------------------------------------------------------------------
    def _parse(self, record: dict) -> list[Chunk]:
        kind = record.get("type")
        if record.get("isSidechain") and not self.cfg.speak_sidechains:
            return []
        if record.get("isMeta"):
            return []

        if kind == "assistant":
            message = record.get("message") or {}
            uuid = record.get("uuid") or message.get("id") or ""
            out = []
            content = message.get("content")
            if isinstance(content, str):
                chunk = self._make("assistant", content, f"{uuid}:0")
                return [chunk] if chunk else []
            for index, block in enumerate(content or []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    chunk = self._make("assistant", block.get("text") or "", f"{uuid}:{index}")
                elif block_type == "tool_use" and self.cfg.speak_tool_use:
                    key = block.get("id") or f"{uuid}:{index}"
                    chunk = self._make("tool_use", describe_tool(block), key)
                else:
                    chunk = None          # thinking blocks are never spoken
                if chunk:
                    out.append(chunk)
            return out

        if kind == "system" and record.get("subtype") == "turn_duration":
            # Claude Code writes this when a turn completes — the signal
            # autopilot uses to know the agent is waiting for input.
            chunk = self._make("turn_end", "turn ended", f"{record.get('uuid')}:turn")
            return [chunk] if chunk else []

        if kind == "user" and self.cfg.speak_tool_results:
            message = record.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                return []
            out = []
            for index, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _flatten_tool_result(block.get("content"))
                key = str(block.get("tool_use_id") or f"{record.get('uuid')}:{index}")
                chunk = self._make("tool_result", text[:2000], key)
                if chunk:
                    out.append(chunk)
            return out

        return []

    def _make(self, role: str, text: str, key: str) -> Chunk | None:
        text = (text or "").strip()
        if not text or not key:
            return None
        if key in self._seen:
            return None
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())
        return Chunk(role=role, text=text, source=self.name, key=key)
