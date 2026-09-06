# Reference — tuning, layout, diagnostics

[← back to the README](../README.md)

## Tuning

Flags override `~/.ollie/config.json`, which overrides the defaults in
`ollie/config.py`. Every field also reads from `OLLIE_<FIELD>` in the
environment.

| What | How |
|---|---|
| Quieter narration | `--no-tools` (prose only) |
| Noisier narration | `--tool-results` (also reads command output) |
| Different voice | `--voice Daniel --rate 190` (`say -v '?'` to list) |
| Different filter model | `--model llama3.2:3b` |
| Different hotkey | `--hotkey "caps lock"`, `--hotkey f13`, `--hotkey-mode toggle` |
| Find a working key | `./run.sh --test-hotkey` prints every key you press |
| Different microphone | `--input-device "MacBook Pro Microphone"` (or the orb's **Microphone** submenu) |
| List microphones | `./run.sh --list-input-devices` |
| Type instead of paste | `OLLIE_INJECT_MODE=type` |
| Better transcription | `OLLIE_WHISPER_REPO=mlx-community/whisper-small.en-mlx` |

Hotkeys can be named the way they are printed on your keyboard — `"right
option"`, `"option"`, `"right command"`, `"caps lock"`, `f13` — or by the
internal names (`alt_r`, `cmd_r`) if you prefer.

If push-to-talk transcribes nothing and `~/.ollie/ollie.log` shows `cannot
open microphone: ... PaErrorCode -9986`, the system default input is likely a
Bluetooth device (AirPods, etc.) mid-profile-switch — PortAudio's CoreAudio
backend frequently fails to open those. Point `input_device` at a wired mic
instead of leaving it on the system default.

Useful knobs that have no flag: `batch_debounce` (how long events are gathered
before one narration pass — raise it for fewer, denser sentences),
`history_window` (how many spoken lines the filter remembers), `max_words`.

### Models

Three models are configurable independently, all from the orb's **Models**
submenu or the config:

| Field | Default | Job |
|---|---|---|
| `ollama_model` | `qwen2.5:3b-instruct` | narration — dedup, condense, tone |
| `autopilot_model` | *(empty — same as narration)* | authors the next instruction |
| `grounding_model` | `ui-venus-8b` | finds click coordinates for [computer use](autopilot.md#computer-use) |

**Models → Check & download missing…** pulls whatever is missing.

## Run from a terminal

Handy while developing, since you see the logs live:

```bash
./run.sh                 # narrator + floating orb
./run.sh --no-orb        # headless, logs to stderr and ~/.ollie/ollie.log
./run.sh --list-sessions # which transcripts it can see; * is the active one
./run.sh --say "hello"   # TTS smoke test
```

This needs your *terminal* to hold the Accessibility grant, which is why the
[app bundle](design-notes.md#why-the-app-bundle-is-a-c-program) is the
recommended way to run it.

```bash
python scripts/make_app.py --dest /Applications   # build somewhere else
```

## Pin the session with a hook (optional)

Instead of guessing the newest transcript, let Claude Code tell Ollie which
session started:

```bash
.venv/bin/python scripts/install_hook.py     # --uninstall to undo
```

This registers a `SessionStart` hook that records the session id and transcript
path in `~/.ollie/current_session.json`. Your existing `~/.claude/settings.json`
is backed up first.

## Settings & dependency report

`./run.sh --settings` prints, and orb menu → **Settings & dependencies…**
opens as a page, a live inventory of everything Ollie stands on: which models
are in use and their on-disk size (Whisper, the Ollama filter/autopilot
models, Kokoro), which macOS APIs are involved and whether each permission is
currently granted for that process, where the transcripts, config and logs
live, and the versions of every library in the stack. All of it is gathered
at render time — it is a health check, not a brochure. Everything listed runs
on this Mac; no cloud API appears anywhere in it.

Logs: the full log is `~/.ollie/ollie.log`; `~/.ollie/app.log` catches raw
stdout and stderr, including crashes. Both are reachable from the orb menu.

## Tests

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest tests -q
```

`scripts/render_orb.py` renders the four orb states to the README's animated
GIF offscreen — handy for tweaking the visuals without launching the app.

## Layout

```
ollie/
  readers/base.py         the Reader contract — the swap point
  readers/claude_code.py  tail + parse the Claude Code session JSONL
  readers/window.py       read any window over the Accessibility API
  filter.py               Ollama dedup/condense, spoken-history memory
  tts.py                  `say` -> WAV -> playback with amplitude
  stt.py                  mlx-whisper, push-to-talk capture
  injector.py             pasteboard + synthesised ⌘V (or unicode typing)
  hotkey.py               global push-to-talk listener
  orb.py                  always-on-top transparent Cocoa window
  core.py                 the shared source-agnostic core loop
  lookup_ai.py            starts/stops the lookup-ai companion subprocess
scripts/
  make_app.py             build Ollie.app (bundle, icon, ad-hoc signature)
  launcher/main.c         the native launcher that gives the app its own TCC identity
  install_hook.py         register the Claude Code SessionStart hook
  session_hook.py         the hook itself
  render_orb.py           offscreen orb preview
lookup-ai/                Electron app: select text, ask an AI — see docs/lookup-ai.md
```
