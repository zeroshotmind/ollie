"""Companion process manager for lookup-ai.

lookup-ai is a separate Electron app (select text anywhere, hit a shortcut,
ask an AI about it) — a different runtime entirely, so it is not imported
into this process. Instead Ollie just spawns and supervises it as a plain
subprocess: start it when Ollie starts, stop it when Ollie quits, and pass
the configured shortcut through as an environment override so the two apps'
settings stay in one place (Ollie's config) without lookup-ai needing to know
Ollie exists.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from pathlib import Path

from .config import Config, STATE_DIR

log = logging.getLogger("ollie.lookup_ai")


class LookupAICompanion:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen | None = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_running():
            return
        if not self.cfg.lookup_ai_enabled:
            return

        project_dir = Path(self.cfg.lookup_ai_path).expanduser()
        # The native Electron.app binary, not node_modules/.bin/electron: that's a
        # `#!/usr/bin/env node` shebang script, and a GUI-launched process (like
        # this one, started by macOS rather than an interactive shell) gets a bare
        # PATH with no node on it, so the shebang exec fails before Electron ever
        # gets a chance to run.
        electron_bin = (
            project_dir / "node_modules" / "electron" / "dist" / "Electron.app"
            / "Contents" / "MacOS" / "Electron"
        )
        if not electron_bin.exists():
            log.warning(
                "lookup-ai not found at %s (run `npm install` there first) — skipping",
                project_dir,
            )
            return

        env = os.environ.copy()
        if self.cfg.lookup_ai_shortcut:
            env["LOOKUP_AI_SHORTCUT"] = self.cfg.lookup_ai_shortcut

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "lookup-ai.log"
        try:
            log_file = open(log_path, "a")
            self._proc = subprocess.Popen(
                [str(electron_bin), "."],
                cwd=str(project_dir),
                env=env,
                stdout=log_file,
                stderr=log_file,
            )
            log.info("lookup-ai started (pid %d), logging to %s", self._proc.pid, log_path)
        except Exception:
            log.exception("failed to start lookup-ai")
            self._proc = None

    def stop(self) -> None:
        if not self.is_running():
            return
        try:
            self._proc.send_signal(signal.SIGTERM)
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)
        except Exception:
            log.exception("failed to stop lookup-ai cleanly")
        finally:
            log.info("lookup-ai stopped")
            self._proc = None

    def restart(self) -> None:
        self.stop()
        self.start()
