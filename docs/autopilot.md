# Autopilot

[← back to the README](../README.md)

Armed, Ollie stops being a narrator and becomes a driver: at the end of each
agent turn it hands your goal plus the agent's latest output to the local
model, which either declares the goal achieved or authors the next instruction
— typed into the terminal with Enter pressed.

```bash
./run.sh --autopilot --goal "make the failing tests in test_auth pass"
```

Or arm it live: right-click the orb → **Autopilot — arm**, then hold the talk
key and *speak* the goal. The orb wears a pulsing amber ring while armed.
**Autopilot — goal from file…** takes the goal from a markdown file instead,
which suits a long brief you don't want to dictate.

The first prompt sent is your goal, verbatim. After that the model reacts to
whatever the agent did: answers its questions decisively, tells it to fix its
errors, pushes it to the next step, and replies `DONE` when the output shows
the goal is met (spoken aloud: "Goal complete").

## Guardrails

Because autopilot types into your focused window:

- injects **only while a terminal app is frontmost** (list configurable via
  `autopilot_frontmost`); otherwise it waits, tells you, and gives up after 3
  minutes
- hard cap of `autopilot_max_turns` (default 15) per goal
- authoring the same instruction twice in a row = stalled → disarms and says so
- authored text that just echoes the agent's output is discarded, never typed
- disarm any time from the orb menu; speaking a new goal always wins

Turn ends are detected from the transcript's own end-of-turn records, with an
idle timer (`autopilot_idle`, 75s) as fallback. If Ollama is unreachable the
turn is skipped and retried — nothing is ever injected on a failure path.

## Computer use

Typing is enough for a terminal, but not for a window with buttons in it. With
**computer use** on (orb menu → *Computer use — let autopilot click*, or the
toggle under the orb), autopilot can click, scroll and press keys inside a
pinned window as well as type at it.

It only engages when all three are true: computer use is on, a
`grounding_model` is set, and you have actually pinned a window via the
**Watch** menu. On the Claude Code transcript it stays off — there is nothing
to click.

Two vision models are involved, and they do different jobs:

| Config | Default | Job |
|---|---|---|
| `autopilot_vision_model` | `qwen3-vl:30b-a3b-instruct` | *decides* the next action from a screenshot of the window |
| `grounding_model` | `ui-venus-8b` | turns "the Send button" into x,y coordinates so the click can land |

Leaving `grounding_model` empty disables clicking entirely. Leaving
`autopilot_vision_model` empty falls back to deciding from the window's screen
text alone, using the autopilot model.

Both are picked from the orb's **Models** submenu alongside the narration
model. Computer use needs the **Screen Recording** permission — see the
[permissions table](../README.md#permissions).

> The `-instruct` tag on the vision model matters: the default `qwen3-vl` tag
> is the thinking build, which ignores `"think": false` and returns empty
> answers when reasoning exhausts the token budget.
