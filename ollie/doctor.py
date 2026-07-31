"""Dependency doctor: make sure every configured model actually exists.

Ollie leans on several Ollama models (narration filter, autopilot backbone,
and — when computer use is on — a vision planner and a grounding specialist).
Nothing guarantees they are present on a fresh machine, and the failure mode
without this module is an error mid-goal instead of a download up front.

Two acquisition paths:

* ordinary models — ``ollama pull`` streamed, with progress reported back
* ui-venus-8b — not in the Ollama library at all; it is imported from the
  original safetensors (the community GGUFs crash Ollama's runner) and given
  the qwen3-vl-instruct renderer, exactly the recipe proven by hand first
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import httpx

from .config import Config

log = logging.getLogger("ollie.doctor")

UI_VENUS_HF_REPO = "inclusionAI/UI-Venus-1.5-8B"


def installed_models(cfg: Config) -> set[str]:
    try:
        response = httpx.get(f"{cfg.ollama_url}/api/tags", timeout=5.0)
        response.raise_for_status()
        return {m.get("name", "") for m in response.json().get("models", [])}
    except Exception as exc:
        log.warning("cannot list Ollama models: %s", exc)
        return set()


def required_models(cfg: Config) -> list[tuple[str, str]]:
    """(model, what it is for) — deduplicated, order = user-facing priority."""
    wanted: list[tuple[str, str]] = [(cfg.ollama_model, "narration filter")]
    if cfg.autopilot_model:
        wanted.append((cfg.autopilot_model, "autopilot"))
    if cfg.computer_use and cfg.grounding_model:
        wanted.append((cfg.grounding_model, "click grounding"))
        if cfg.autopilot_vision_model:
            wanted.append((cfg.autopilot_vision_model, "computer-use vision"))
    seen: set[str] = set()
    out = []
    for name, why in wanted:
        if name and name not in seen:
            seen.add(name)
            out.append((name, why))
    return out


def missing_models(cfg: Config) -> list[tuple[str, str]]:
    have = installed_models(cfg)
    bare = {name.split(":")[0] for name in have}

    def present(name: str) -> bool:
        return name in have or (":" not in name and name in bare)

    return [(name, why) for name, why in required_models(cfg) if not present(name)]


def pull_model(cfg: Config, name: str, status: Callable[[str], None]) -> bool:
    """ollama pull with streamed progress -> status callback."""
    try:
        last_pct = -1
        with httpx.stream("POST", f"{cfg.ollama_url}/api/pull",
                          json={"model": name}, timeout=None) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if event.get("error"):
                    log.error("pull %s: %s", name, event["error"])
                    return False
                total, done = event.get("total"), event.get("completed")
                if total and done:
                    pct = int(done * 100 / total)
                    if pct != last_pct and pct % 5 == 0:
                        last_pct = pct
                        status(f"Downloading {name}: {pct}% of "
                               f"{total / 1e9:.1f} GB")
        return name in installed_models(cfg) or True
    except Exception as exc:
        log.error("pull %s failed: %s", name, exc)
        return False


def import_ui_venus(cfg: Config, name: str, status: Callable[[str], None]) -> bool:
    """Build ui-venus-8b locally: HF safetensors -> ollama create -q q4_K_M.

    Slow (a ~16 GB download plus quantisation) but hands-free; the caller
    streams the phases to the user.
    """
    try:
        status("Downloading UI-Venus weights from Hugging Face (~16 GB)…")
        from huggingface_hub import snapshot_download

        snapshot = snapshot_download(UI_VENUS_HF_REPO)

        # ollama refuses the HF cache's symlinked layout — dereference first
        status("Preparing UI-Venus for import…")
        staging = Path(tempfile.mkdtemp(prefix="ollie-uivenus-")) / "model"
        shutil.copytree(snapshot, staging, symlinks=False)
        modelfile = staging.parent / "Modelfile"
        modelfile.write_text(
            f"FROM {staging}\n"
            "TEMPLATE {{ .Prompt }}\n"
            # the safetensors import gets a bare template; the built-in
            # renderer is what makes vision input actually work
            "RENDERER qwen3-vl-instruct\n"
            "PARSER qwen3-vl-instruct\n"
            "PARAMETER temperature 0\n"
        )
        status("Quantising UI-Venus (q4_K_M) — a few minutes…")
        result = subprocess.run(
            ["ollama", "create", name.split(":")[0], "--quantize", "q4_K_M",
             "-f", str(modelfile)],
            capture_output=True, text=True, timeout=3600,
        )
        shutil.rmtree(staging.parent, ignore_errors=True)
        if result.returncode != 0:
            log.error("ollama create failed: %s", result.stderr[-400:])
            return False
        return True
    except Exception as exc:
        log.error("ui-venus import failed: %s", exc)
        return False


# models that need a bespoke acquisition path instead of `ollama pull`
IMPORTERS: dict[str, Callable[[Config, str, Callable[[str], None]], bool]] = {
    "ui-venus-8b": import_ui_venus,
}


def acquire(cfg: Config, name: str, status: Callable[[str], None]) -> bool:
    importer = IMPORTERS.get(name.split(":")[0])
    if importer is not None:
        return importer(cfg, name, status)
    return pull_model(cfg, name, status)
