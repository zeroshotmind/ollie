"""POC: GUI grounding — "where is X on this screen?" — with local models.

Compares two backends on the same window screenshot:

  * ollama — qwen3-vl:30b (or any vision model) through the Ollama API
  * mlx    — UI-Venus-1.5-8B (mlx-community 6-bit) through mlx-vlm

For each --find instruction the model returns a click point; we draw a
crosshair on a copy of the screenshot and report latency, so quality can be
judged by eye and speed by numbers.

Run from the scratch venv (needs pillow + requests + pyobjc Quartz; mlx-vlm
only for --backend mlx):

  python scripts/poc_grounding.py --list
  python scripts/poc_grounding.py --app Safari --find "the address bar" \
      --find "the reload button" --backend ollama --backend mlx
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

OLLAMA_URL = "http://127.0.0.1:11434"
MAX_WIDTH = 1288          # downscale target: keeps vision tokens sane
MLX_MODEL = "mlx-community/UI-Venus-1.5-8B-6bit"


# ---------------------------------------------------------------- capture
def list_windows() -> list[dict]:
    import Quartz

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
    out = []
    for w in info or []:
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = w.get("kCGWindowBounds", {})
        if bounds.get("Width", 0) < 200 or bounds.get("Height", 0) < 150:
            continue
        out.append({
            "id": w["kCGWindowNumber"],
            "app": w.get("kCGWindowOwnerName", "?"),
            "title": w.get("kCGWindowName", "") or "(untitled)",
            "w": int(bounds.get("Width", 0)),
            "h": int(bounds.get("Height", 0)),
        })
    return out


def capture_window(window_id: int) -> Image.Image:
    tmp = tempfile.mktemp(suffix=".png")
    subprocess.run(["screencapture", "-x", "-o", "-l", str(window_id), tmp],
                   check=True)
    img = Image.open(tmp).convert("RGB")
    Path(tmp).unlink(missing_ok=True)
    return img


def prepare(img: Image.Image) -> Image.Image:
    if img.width > MAX_WIDTH:
        h = round(img.height * MAX_WIDTH / img.width)
        img = img.resize((MAX_WIDTH, h), Image.LANCZOS)
    return img


# ---------------------------------------------------------------- parsing
_NUMS = re.compile(r"-?\d+(?:\.\d+)?")


def parse_point(text: str, width: int, height: int):
    """Best-effort click point from model output.

    Handles click(x=..,y=..), (x,y), [x1,y1,x2,y2] (box -> centre), and both
    absolute-pixel and 0-1000-normalised conventions.
    """
    nums = [float(n) for n in _NUMS.findall(text)]
    if len(nums) >= 4 and ("[" in text or "box" in text.lower()):
        x, y = (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    elif len(nums) >= 2:
        x, y = nums[0], nums[1]
    else:
        return None
    # 0-1000 normalised coords are the giveaway when they exceed nothing
    if x <= 1000 and y <= 1000 and (width > 1100 or height > 1100):
        x, y = x / 1000 * width, y / 1000 * height
    if not (0 <= x <= width and 0 <= y <= height):
        return None
    return round(x), round(y)


# ---------------------------------------------------------------- backends
def ask_ollama(img: Image.Image, instruction: str, model: str) -> tuple[str, float]:
    import requests

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    prompt = (f"Locate this element on the screenshot: {instruction}. "
              f"The image is {img.width}x{img.height} pixels. Reply with only "
              f"the click point as (x, y) in pixels.")
    t0 = time.time()
    r = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(buf.getvalue()).decode()]}],
        "think": False,   # qwen3 family: reasoning otherwise eats num_predict
        "options": {"num_predict": 120, "temperature": 0},
    }, timeout=600)
    if r.status_code == 400:      # model doesn't take the think flag
        payload = r.request.body
        r = requests.post(f"{OLLAMA_URL}/api/chat", data=payload.replace(
            b'"think":false,', b""), timeout=600)
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), time.time() - t0


_MLX = {}


def ask_mlx(img: Image.Image, instruction: str, model: str) -> tuple[str, float]:
    from mlx_vlm import generate, load
    from mlx_vlm.prompt_utils import apply_chat_template

    if "m" not in _MLX:
        print(f"  loading {model} …", flush=True)
        _MLX["m"], _MLX["p"] = load(model)
        _MLX["c"] = _MLX["m"].config
    tmp = tempfile.mktemp(suffix=".png")
    img.save(tmp)
    # UI-Venus grounding prompt (from the model card)
    prompt = (f'Outline the position corresponding to the instruction: '
              f'{instruction}. The output should be only [x1,y1,x2,y2].')
    formatted = apply_chat_template(_MLX["p"], _MLX["c"], prompt, num_images=1)
    t0 = time.time()
    out = generate(_MLX["m"], _MLX["p"], formatted, image=[tmp],
                   max_tokens=120, temperature=0, verbose=False)
    Path(tmp).unlink(missing_ok=True)
    text = out.text if hasattr(out, "text") else str(out)
    return text.strip(), time.time() - t0


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list capturable windows")
    ap.add_argument("--window", type=int, help="CGWindow id to capture")
    ap.add_argument("--app", help="capture the first window of this app")
    ap.add_argument("--image", help="use an existing screenshot instead")
    ap.add_argument("--find", action="append", default=[],
                    help="element to locate (repeatable)")
    ap.add_argument("--backend", action="append", default=[],
                    choices=["ollama", "mlx"], help="backend(s) to test")
    ap.add_argument("--ollama-model", default="qwen3-vl:30b")
    ap.add_argument("--mlx-model", default=MLX_MODEL)
    ap.add_argument("--out", default="/tmp/ollie-grounding")
    args = ap.parse_args()

    if args.list:
        for w in list_windows():
            print(f"{w['id']:>6}  {w['app']:<24} {w['w']}x{w['h']:<10} {w['title'][:60]}")
        return

    if args.image:
        img = Image.open(args.image).convert("RGB")
        label = Path(args.image).stem
    else:
        wid = args.window
        if wid is None and args.app:
            match = [w for w in list_windows()
                     if args.app.lower() in w["app"].lower()]
            if not match:
                sys.exit(f"no on-screen window for app {args.app!r}")
            wid = match[0]["id"]
            print(f"capturing: {match[0]['app']} — {match[0]['title'][:60]}")
        if wid is None:
            sys.exit("need --window, --app or --image (see --list)")
        img = capture_window(wid)
        label = f"win{wid}"

    img = prepare(img)
    print(f"screenshot: {img.width}x{img.height}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    backends = args.backend or ["ollama"]
    finds = args.find or ["the close button of the window"]
    results = []
    for backend in backends:
        model = args.ollama_model if backend == "ollama" else args.mlx_model
        marked = img.copy()
        draw = ImageDraw.Draw(marked)
        print(f"\n=== {backend} ({model}) ===")
        for i, instruction in enumerate(finds):
            try:
                ask = ask_ollama if backend == "ollama" else ask_mlx
                raw, dt = ask(img, instruction, model)
            except Exception as e:
                print(f"  [{i}] {instruction!r}: FAILED — {e}")
                continue
            pt = parse_point(raw, img.width, img.height)
            print(f"  [{i}] {instruction!r}\n      -> {raw[:120]!r}"
                  f"\n      -> point={pt}  {dt:.2f}s")
            results.append({"backend": backend, "find": instruction,
                            "raw": raw, "point": pt, "seconds": round(dt, 2)})
            if pt:
                x, y = pt
                r = 14
                draw.ellipse([x - r, y - r, x + r, y + r], outline="red", width=4)
                draw.line([x - r - 8, y, x + r + 8, y], fill="red", width=2)
                draw.line([x, y - r - 8, x, y + r + 8], fill="red", width=2)
                draw.text((x + r + 4, y - r - 4), f"{i}", fill="red")
        path = out_dir / f"{label}-{backend}.png"
        marked.save(path)
        print(f"  marked image: {path}")

    (out_dir / f"{label}-results.json").write_text(json.dumps(results, indent=2))
    print(f"\nresults: {out_dir / (label + '-results.json')}")


if __name__ == "__main__":
    main()
