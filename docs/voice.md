# Voice, tone and narration styles

[← back to the README](../README.md)

## Narration styles

**Style** is how much you hear.

| Style | What you hear | Model in the loop |
|---|---|---|
| `brief` (default) | One terse line; routine steps skipped | yes |
| `full` | Loss-less retelling — every action, file, number and question kept, several sentences allowed | yes |
| `verbatim` | The agent's own words, lightly cleaned for speech (markdown stripped, code fences elided); URLs and paths are read as written | no |

Switch with `--style full`, by right-clicking the orb, or by setting `style`
in `~/.ollie/config.json`. Changes made from the orb menu persist.

## Engine

**Engine** is what synthesises. Two options, switchable live from the orb's
**Engine** menu or `--engine`:

| Engine | Sound | Cost |
|---|---|---|
| `say` (default) | classic macOS voices | zero setup, instant |
| `kokoro` | neural (Kokoro-82M via MLX), noticeably more natural | install with `-e '.[kokoro]'`, one-time ~330 MB model download, ~2s warmup at launch |

Warm Kokoro synthesis runs ~50× real time on Apple Silicon — about 0.15s for
six seconds of speech — so it adds nothing perceptible to the narration cycle,
and it falls back to `say` per-utterance if anything goes wrong. Kokoro voices
(`af_heart`, `am_adam`, `bf_emma`, …) appear in the Voice menu when the engine
is active; `kokoro_speed` in the config trades pace for clarity.

## Voice

**Voice** is who speaks: any installed macOS voice. `./run.sh --list-voices`
shows what you have (System Settings → Accessibility → Spoken Content →
Manage Voices to download nicer ones — the Siri and Enhanced voices work).
Pick from the orb's right-click **Voice** menu (you hear a preview instantly),
or `--voice Daniel --rate 190`.

## Tone

**Tone** is how the narration is written, applied at the filter — facts stay
identical across tones:

| Tone | Flavour |
|---|---|
| `neutral` (default) | plain colleague-over-the-shoulder |
| `warm` | friendly, encouraging |
| `snarky` | dry wit — about the code, never about you |
| `minimal` | telegraphic, fewest words that carry every fact |

Orb menu → **Tone**, or `--tone snarky`. Verbatim style ignores tone, since
there the agent's own words are the whole point.
