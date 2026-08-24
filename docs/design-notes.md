# Design notes

[← back to the README](../README.md)

## Why the app bundle is a C program

macOS grants Accessibility and Microphone to an **application**, not to a
script. Run `python -m ollie` from a shell and the grant lands on your terminal
— so every script that terminal ever runs inherits the ability to read your
keystrokes and drive your machine. Inside the bundle the grant belongs to Ollie
alone, and the permission dialogs say "Ollie".

Getting that right takes more than a folder with an `Info.plist`. The obvious
wrapper — a shell script that `exec`s the interpreter — does **not** work:
`exec` replaces the process image, so macOS attributes the permission to the
Python binary and Accessibility lists a bare "python 3.12". So Ollie's
`Contents/MacOS/Ollie` is a small C program that links libpython and calls
`Py_BytesMain` directly. The process image never changes, the running process
really is the bundle, and TCC sees Ollie. `scripts/launcher/main.c` is about
sixty lines and `make_app.py` compiles it with clang.

The bundle stays a thin wrapper (~1.4 MB): it points at this project's
virtualenv, so edits to the Python take effect on the next launch with no
rebuild — and, importantly, without disturbing the permission.

## Why rebuilding revokes the grant

macOS keys Accessibility to the bundle's code signature, and re-signing
produces a new seal even when nothing changed. So `make_app.py` compares a
manifest of its inputs (launcher source, `orb.py`, paths, `Info.plist`) and
does nothing at all when they match. You only re-grant when the app genuinely
changed: a new launcher, a new icon, or a moved project.

Byte-comparing the built binary would not work — the linker stamps a fresh
LC_UUID on every build, so no two builds are ever identical.

A grant can also go stale in the other direction: the Settings checkbox shows
"on" while the grant is keyed to a signature this build no longer has. The orb
detects that (`AXIsProcessTrusted()` disagreeing with the checkbox) and offers
to reset and re-request it; by hand,
`tccutil reset Accessibility com.zeroshotmind.ollie` clears the row.

## Why the raw shell command never reaches the model

A 3B model handed
`cd /repo && KMP_DUPLICATE_LIB_OK=TRUE .venv/bin/python -c "import torch..."`
will simply read it back to you. `summarize_command()` in the reader turns it
into `running a python snippet` first. The filter is also given few-shot
examples, and any output that is just a tool event echoed back is dropped
(`OllamaFilter._is_echo`).

## Degradation

If Ollama is down the filter falls back to a deterministic local condense
rather than going silent. If `say` cannot synthesise to a file it falls back to
speaking directly, losing only the amplitude animation. If clang is missing,
`make_app.py` falls back to a shell launcher and says so — the app still runs,
it just loses its own permission identity.

## Logging goes to a duplicated file descriptor

PortAudio's CoreAudio backend points fd 2 at `/dev/null` while it initialises,
to hide its own chatter, and restores it afterwards. Anything logged in that
window vanishes — which quietly ate the startup lines until `_setup_logging`
started handing the console handler its own `os.dup` of stderr.

## The reader never replays

It tracks inode, size and the first 256 bytes of the transcript, so a rotated,
truncated or rewritten file is detected and reread instead of producing garbage
from a stale offset. Chunks are deduped by tool-use id and message uuid, so the
same line is never spoken twice.

Thinking blocks and subagent sidechains are never spoken.

## Why tail the JSONL instead of scraping the terminal

Tailing the session JSONL means no ANSI escapes, no redraw/spinner noise, and
clean role information for free.

The **reader** is the only component that knows where output comes from.
Everything downstream is source-agnostic. See `ollie/readers/base.py` for the
contract — `readers/claude_code.py` tails the transcript, `readers/window.py`
reads any window over the Accessibility API.

Latency is roughly 2–4 seconds end to end. Stages run sequentially on purpose;
optimise only if it starts to bother you.
