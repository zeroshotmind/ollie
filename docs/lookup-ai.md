# Ask AI on selection (lookup-ai)

[← back to the README](../README.md)

A separate, small Electron app that lives at `lookup-ai/` in this repo, with
two features: select text anywhere and ask an AI about it, and dim everything
but one part of the screen to focus on it. Neither has anything to do with
Ollie's voice loop — Ollie just starts the app, stops it, and lets you
configure it from one place instead of running two apps by hand.

## Enable / disable

On by default. Toggle it live from the orb menu → **Ask AI on selection**, or
set `lookup_ai_enabled: false` in `~/.ollie/config.json`. Toggling persists;
starting/stopping happens immediately, no restart needed.

## Shortcut

Default is **⌘E**. Set it from lookup-ai's own Settings window — that's the
one place that actually matters. `lookup_ai_shortcut` in
`~/.ollie/config.json` only seeds the accelerator on a brand-new install
(passed through as `LOOKUP_AI_SHORTCUT`); once lookup-ai has ever saved a
shortcut of its own, that saved value wins on every future launch, even
though Ollie keeps passing its own config's value as the env var every time
it starts the companion. Ollie's copy is otherwise just a label — it's shown
in the orb menu's "Ask AI on selection (...)" text, so update it there too
if you want that label accurate, but it's cosmetic only.

## Focus on screen area

Default shortcut is **⌘⇧F** (change it in the same Settings window as the Ask
AI shortcut). Drag a rectangle like the macOS screenshot tool; everything
outside it dims. Click outside the rectangle, or press Esc, to dismiss —
clicks inside it are swallowed, not passed through, so you can't accidentally
interact with whatever's underneath while it's up.

Once locked, a **Darkness** slider appears at the bottom of the screen —
drag it live to adjust how dark the dimmed area is; your last value is
remembered as the default next time (`focusDarkness` in the config, 0.2–0.98).

## Multi-turn conversation

Follow-up questions work: the response appears above the input box, which
then stays put below it for the next question, and each answer builds on
the prior turns in that same popup session. Every backend call is stateless
under the hood — lookup-ai replays the conversation as plain text ahead of
each new question — so this works uniformly across CLI, Ollama, and
OpenRouter backends without needing backend-specific session support.
Closing the popup (Esc, clicking outside, or hitting the shortcut again to
toggle it closed) ends the session; reopening it starts a fresh one.

## History

The last 30 sessions persist across popup closes — the clock icon next to
the close button opens a panel listing them (title, relative time, backend
used); clicking one reloads its full conversation and lets you keep going
where you left off. A session is saved after its first successful answer,
not only on close, so closing the popup by accident never loses one that
already got a reply. **Clear** in the history panel wipes all of it.
Sessions are stored in the same config file as everything else
(`~/Library/Application Support/lookup-ai/config.json`, key `history`).

## Backends

Configured per-backend in lookup-ai's own Settings window (right-click the
lookup-ai tray icon → Settings), stored separately from Ollie's config since
it's a different process:

| Backend | Needs | Model picker | Effort picker |
|---|---|---|---|
| Claude CLI | `claude` on `PATH` | yes (`sonnet`/`opus`/`haiku`) | no |
| Codex CLI | `codex` on `PATH` | yes | yes (`minimal`/`low`/`medium`/`high`, via `-c model_reasoning_effort`) |
| Ollama | a running `ollama` server | yes | no |
| OpenRouter | an API key (set in Settings) | yes | yes (`low`/`medium`/`high`, via the `reasoning.effort` field) |

The popup only shows the model/effort dropdowns a backend actually defines.
Your last choice per backend is remembered across popup opens. Edit the
model/effort lists, or a CLI backend's flags, as JSON fields in Settings.

## Troubleshooting

**A shortcut you saved in Settings doesn't do anything, and pressing it
instead seems to trigger something in whatever app is frontmost** (e.g. a
browser's own keybinding): that means the accelerator was never actually
registered by lookup-ai, so macOS delivered the keypress straight through to
whatever app was in front. The most likely cause is another app (or macOS
itself) already owns that exact key combination — Settings now shows an
error naming which accelerator failed and which one is still active when
this happens; try a different combination. If Settings doesn't show an
error but the shortcut still silently doesn't fire, check the value in
`~/Library/Application Support/lookup-ai/config.json` matches what you meant
to save.

**A CLI backend (Claude/Codex) fails with `spawn <command> ENOENT`** even
though it works fine in your terminal: lookup-ai resolves CLI binaries via
`$SHELL -ilc 'command -v <command>'` at call time, since a GUI-launched
process (this one, started by Ollie or Finder/LaunchServices) gets a bare
`PATH` with none of your shell customizations. This only works if the binary
is actually on `PATH` inside an *interactive* shell — if you added it via
something unusual (a `.bashrc` guarded by `[ -n "$PS1" ]`, a tool that only
patches a non-login, non-interactive shell config), it may still not resolve.
Confirm with `zsh -ilc 'command -v claude'` (or your shell) from a plain
terminal; whatever that prints is what lookup-ai will try to run.

**The popup doesn't appear on ⌘E**: check that lookup-ai's tray icon
("AI" in the menu bar) is present — if Ollie's log
(`~/.ollie/ollie.log`, filtered to `ollie.lookup_ai`) shows nothing, the
companion never started, most likely because `lookup-ai/node_modules` isn't
installed (see [Install](../README.md#install)). If it's present but the
shortcut is unresponsive, another app may already be bound to that
accelerator, or a stale lookup-ai process from a previous run is still
holding it — quitting Ollie doesn't always reach a process that was force-
killed rather than quit cleanly; check for and kill any leftover
`.../lookup-ai/node_modules/electron/dist/Electron.app/.../Electron` process.

**Selected text doesn't appear in the popup**: lookup-ai grabs it by
simulating ⌘C and reading the clipboard, which needs Accessibility
permission granted to the Electron process itself (a separate grant from
Ollie's own, since it's a different app) — System Settings → Privacy &
Security → Accessibility.
