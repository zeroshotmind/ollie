#!/bin/bash
# Ollie installer — https://zeroshotmind.github.io/ollie
#
# Builds Ollie on THIS Mac rather than shipping a prebuilt binary: the app
# bundle must be compiled and signed locally so macOS attributes the
# Accessibility / Input Monitoring / Microphone grants to Ollie itself
# (a downloaded unsigned binary would be blocked by Gatekeeper anyway).
#
# Environment overrides:
#   OLLIE_SRC_DIR      where the source lives   (default ~/.ollie/src)
#   OLLIE_APP_DEST     where Ollie.app goes     (default /Applications)
#   OLLIE_REF          git branch or tag        (default main)
#   OLLIE_KOKORO=1     also install the neural TTS engine
#   OLLIE_SKIP_LAUNCH=1  build but do not open the app

set -euo pipefail

REPO="zeroshotmind/ollie"
SRC_DIR="${OLLIE_SRC_DIR:-$HOME/.ollie/src}"
APP_DEST="${OLLIE_APP_DEST:-/Applications}"
REF="${OLLIE_REF:-main}"

step() { printf '\n\033[1;35m🔮 %s\033[0m\n' "$*"; }
note() { printf '\033[0;36m   %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m ! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m ✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "Ollie is macOS-only."
[ "$(uname -m)" = "arm64" ] || fail "Ollie needs Apple Silicon (this Mac is $(uname -m))."

# ---------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (manages Python and packages)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv installation failed — install it from https://docs.astral.sh/uv and rerun."
fi

# ---------------------------------------------------------------- source
step "Getting the Ollie source"
mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ]; then
  note "updating existing copy at $SRC_DIR"
  git -C "$SRC_DIR" fetch --quiet origin "$REF" && git -C "$SRC_DIR" checkout --quiet "$REF" && git -C "$SRC_DIR" pull --ff-only --quiet
elif command -v git >/dev/null 2>&1; then
  git clone --quiet --depth 1 -b "$REF" "https://github.com/$REPO.git" "$SRC_DIR"
  note "cloned to $SRC_DIR"
else
  tmp="$(mktemp -d)"
  curl -LsS "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" | tar xz -C "$tmp"
  rm -rf "$SRC_DIR"; mv "$tmp"/*/ "$SRC_DIR"; rm -rf "$tmp"
  note "downloaded to $SRC_DIR"
fi
cd "$SRC_DIR"

# ---------------------------------------------------------------- python env
step "Setting up the Python environment"
uv venv --python 3.12 .venv --quiet
if [ "${OLLIE_KOKORO:-0}" = "1" ]; then
  note "core + Kokoro neural TTS (this pulls MLX audio packages)"
  uv pip install --python .venv/bin/python --quiet -e '.[kokoro]'
else
  uv pip install --python .venv/bin/python --quiet -e .
  note "core installed — add the neural voice later with:"
  note "  OLLIE_KOKORO=1 bash <(curl -fsSL https://zeroshotmind.github.io/ollie/install.sh)"
fi

# ---------------------------------------------------------------- clang
if ! xcode-select -p >/dev/null 2>&1; then
  warn "Xcode Command Line Tools not found — Ollie's launcher needs clang so"
  warn "macOS shows permissions under Ollie's own name."
  warn "A dialog should appear now; rerun this installer after it finishes."
  xcode-select --install >/dev/null 2>&1 || true
  fail "Command Line Tools missing."
fi

# ---------------------------------------------------------------- ollama
step "Checking Ollama (powers the narration filter)"
if command -v ollama >/dev/null 2>&1; then
  if curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    if ollama list 2>/dev/null | grep -q "qwen2.5:3b-instruct"; then
      note "qwen2.5:3b-instruct already pulled"
    else
      note "pulling qwen2.5:3b-instruct (~1.9 GB, one time)…"
      ollama pull qwen2.5:3b-instruct
    fi
  else
    warn "Ollama is installed but not running. Start it, then run:"
    warn "  ollama pull qwen2.5:3b-instruct"
    warn "Ollie still works meanwhile, with simpler narration."
  fi
else
  warn "Ollama not found. Install it from https://ollama.com (or: brew install ollama),"
  warn "then run:  ollama pull qwen2.5:3b-instruct"
  warn "Ollie still works meanwhile, with simpler narration."
fi

# ---------------------------------------------------------------- app
step "Building Ollie.app"
.venv/bin/python scripts/make_app.py --dest "$APP_DEST" | sed 's/^/   /'

if [ "${OLLIE_SKIP_LAUNCH:-0}" != "1" ]; then
  open "$APP_DEST/Ollie.app"
fi

# ---------------------------------------------------------------- outro
step "Installed. One manual step remains: permissions."
cat <<'TXT'
   macOS asks for three permissions, each under System Settings →
   Privacy & Security. Ollie appears in each list by itself — flip the
   toggles (no need for the + button):

     1. Input Monitoring   — so the push-to-talk key is heard
     2. Accessibility      — so speech can be typed into your terminal
     3. Microphone         — so it can hear you at all

   Then right-click the orb → Quit, and open Ollie again once.

   Speak:   hold right Option (⌥), talk, release.
   Verify:  ~/.ollie/src/run.sh --doctor
   Docs:    https://zeroshotmind.github.io/ollie
TXT
