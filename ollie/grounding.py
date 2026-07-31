"""Visual grounding: "where is X in this window?" -> screen coordinates.

This is what lets autopilot *click* things instead of only typing. The pinned
window is screenshotted, handed to a local vision model together with a plain
description of the target ("the Send button"), and the model answers with a
point. UI-Venus (imported into Ollama from the original safetensors — the
community GGUFs crash the runner) and qwen3-vl both work; both answer in
0-1000-normalised coordinates.

Screenshots need the Screen Recording permission for whichever app runs
Ollie, granted the same way as Accessibility.

Proven in scripts/poc_grounding.py: ~0.3s per call for ui-venus-8b once warm.
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("ollie.grounding")

MAX_WIDTH = 1288      # downscale target: keeps vision-token prefill fast
_NUMS = re.compile(r"-?\d+(?:\.\d+)?")


def _window_id(pid: int, frame: tuple[float, float, float, float]) -> int | None:
    """CGWindow id of the pid's on-screen window closest to the AX frame.

    AX and CGWindowList number windows differently, so the frame — same
    top-left-origin coordinate space in both APIs — is the join key.
    """
    import Quartz

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    x, y, w, h = frame
    best, best_err = None, 40.0
    for win in info or []:
        if win.get("kCGWindowOwnerPID") != pid or win.get("kCGWindowLayer", 0) != 0:
            continue
        b = win.get("kCGWindowBounds", {})
        err = (abs(b.get("X", 1e9) - x) + abs(b.get("Y", 1e9) - y)
               + abs(b.get("Width", 1e9) - w) + abs(b.get("Height", 1e9) - h))
        if err < best_err:
            best, best_err = win["kCGWindowNumber"], err
    return best


def parse_point(text: str, width: int, height: int) -> tuple[int, int] | None:
    """Point in image pixels from a model reply.

    Accepts (x, y), click(x=..,y=..) and [x1,y1,x2,y2] boxes (centre taken);
    0-1000-normalised answers — what UI-Venus and qwen3-vl produce — are
    detected and scaled.
    """
    nums = [float(n) for n in _NUMS.findall(text or "")]
    if len(nums) >= 4 and ("[" in text or "box" in text.lower()):
        x, y = (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    elif len(nums) >= 2:
        x, y = nums[0], nums[1]
    else:
        return None
    if x <= 1000 and y <= 1000 and (width > 1100 or height > 1100):
        x, y = x / 1000 * width, y / 1000 * height
    if not (0 <= x <= width and 0 <= y <= height):
        return None
    return round(x), round(y)


class Grounder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = httpx.Client(timeout=cfg.filter_timeout)

    def locate(self, description: str, pid: int,
               frame: tuple[float, float, float, float]) -> tuple[int, int] | None:
        """Global screen coordinates of the described element, or None."""
        shot = self._capture(pid, frame)
        if shot is None:
            return None
        png, img_w, img_h = shot
        try:
            raw, took = self._ask(png, description, img_w, img_h)
        except Exception as exc:
            log.error("grounding model failed: %s", exc)
            return None
        point = parse_point(raw, img_w, img_h)
        log.info("ground %r -> %r -> %s (%.2fs)", description, raw[:80], point, took)
        if point is None:
            return None
        # image pixels -> window fraction -> global screen point
        fx, fy, fw, fh = frame
        return (round(fx + point[0] / img_w * fw),
                round(fy + point[1] / img_h * fh))

    def screenshot(self, pid: int,
                   frame: tuple[float, float, float, float]) -> tuple[bytes, int, int] | None:
        """Downscaled PNG of the window plus its pixel size — the same capture
        the grounding call uses, exposed so autopilot can *see* the window
        when deciding its next action, not only when clicking."""
        return self._capture(pid, frame)

    # ------------------------------------------------------------------
    def _capture(self, pid: int, frame) -> tuple[bytes, int, int] | None:
        """PNG of the window plus its pixel dimensions, downscaled via sips
        (a macOS builtin — no imaging dependency)."""
        wid = _window_id(pid, frame)
        tmp = tempfile.mktemp(suffix=".png")
        try:
            if wid is not None:
                cmd = ["screencapture", "-x", "-o", "-l", str(wid), tmp]
            else:
                x, y, w, h = frame        # occlusion-prone fallback
                cmd = ["screencapture", "-x", f"-R{x:.0f},{y:.0f},{w:.0f},{h:.0f}", tmp]
            subprocess.run(cmd, check=True, capture_output=True)
            subprocess.run(["sips", "--resampleWidth", str(MAX_WIDTH), tmp],
                           check=True, capture_output=True)
            probe = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", tmp],
                check=True, capture_output=True, text=True).stdout
            dims = {k.strip(): int(v) for k, v in
                    (line.split(":") for line in probe.splitlines() if ":" in line)}
            return (Path(tmp).read_bytes(),
                    dims["pixelWidth"], dims["pixelHeight"])
        except Exception as exc:
            log.error("window capture failed (Screen Recording permission?): %s", exc)
            return None
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _ask(self, png: bytes, description: str,
             width: int, height: int) -> tuple[str, float]:
        model = self.cfg.grounding_model or self.cfg.ollama_model
        prompt = (f"Locate this element on the screenshot: {description}. "
                  f"The image is {width}x{height} pixels. Reply with only "
                  f"the click point as (x, y).")
        body = {
            "model": model, "stream": False,
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(png).decode()]}],
            "think": False,
            "options": {"num_predict": 80, "temperature": 0},
        }
        start = time.time()
        response = self._client.post(f"{self.cfg.ollama_url}/api/chat", json=body)
        if response.status_code == 400:      # model rejects the think flag
            body.pop("think", None)
            response = self._client.post(f"{self.cfg.ollama_url}/api/chat", json=body)
        response.raise_for_status()
        content = ((response.json() or {}).get("message") or {}).get("content", "") or ""
        content = re.sub(r"<think>.*?(</think>|$)", "", content, flags=re.S).strip()
        if not content:
            # UI-Venus sometimes answers a free-form question with nothing;
            # its training-native grounding format is far more reliable
            body["messages"][0]["content"] = (
                f"Outline the position corresponding to the instruction: "
                f"{description}. The output should be only [x1,y1,x2,y2].")
            response = self._client.post(f"{self.cfg.ollama_url}/api/chat", json=body)
            response.raise_for_status()
            content = ((response.json() or {}).get("message") or {}).get("content", "") or ""
            content = re.sub(r"<think>.*?(</think>|$)", "", content, flags=re.S).strip()
        return content, time.time() - start

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
