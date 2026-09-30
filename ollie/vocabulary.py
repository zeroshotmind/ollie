"""User vocabulary for speech-to-text.

One file, one entry per line:

    Kubernetes                  (a word Whisper should know)
    cloud code -> Claude Code   (a mishearing to fix after transcription)

Plain entries (and the right-hand side of every rule) go into Whisper's
`initial_prompt`, which biases the decoder toward those spellings. That is a
nudge, not a guarantee, so `wrong -> right` rules are applied to the text
afterwards for the cases that still slip through.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("ollie.vocabulary")

# Whisper keeps only the last n_text_ctx // 2 - 1 (= 223) prompt tokens and
# drops the rest from the front, silently. Stay under that with some slack.
PROMPT_TOKEN_BUDGET = 200

TEMPLATE = """\
# Ollie custom vocabulary — words Whisper should recognise.
#
# One entry per line. Lines starting with # are ignored.
#
#   Kubernetes                  a word to recognise, spelled the way you want it
#   cloud code -> Claude Code   a mishearing to replace after transcription
#
# Plain words bias the model toward them; "->" rules always apply.
# Saved changes take effect on your next push-to-talk, no restart needed.

"""


@dataclass
class Vocabulary:
    terms: list[str] = field(default_factory=list)
    replacements: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.terms or self.replacements)

    def prompt_terms(self) -> list[str]:
        """Every spelling Whisper should be nudged toward, deduplicated."""
        seen: set[str] = set()
        out: list[str] = []
        for term in self.terms + [right for _, right in self.replacements]:
            key = term.lower()
            if term and key not in seen:
                seen.add(key)
                out.append(term)
        return out


def parse(text: str) -> Vocabulary:
    vocab = Vocabulary()
    for raw in text.splitlines():
        # Full-line comments only: "#" inside an entry is a word (C#, F#).
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "->" in line:
            wrong, _, right = line.partition("->")
            wrong, right = wrong.strip(), right.strip()
            if wrong:
                vocab.replacements.append((wrong, right))
            else:
                log.warning("vocabulary: ignoring rule with nothing to replace: %r", raw)
        else:
            vocab.terms.append(line)
    return vocab


def estimate_tokens(text: str) -> int:
    """Rough GPT-2-BPE count; errs high so the budget holds without a tokenizer."""
    return sum(len(word) // 3 + 1 for word in text.split()) + text.count(",")


def build_prompt(
    vocab: Vocabulary,
    count_tokens: Callable[[str], int] = estimate_tokens,
    budget: int = PROMPT_TOKEN_BUDGET,
) -> str | None:
    """A glossary-style prompt that fits Whisper's prompt window, or None."""
    terms = vocab.prompt_terms()
    if not terms:
        return None
    kept: list[str] = []
    for i, term in enumerate(terms):
        candidate = "Glossary: " + ", ".join(kept + [term]) + "."
        if count_tokens(candidate) > budget:
            log.warning(
                "vocabulary: prompt full at %d terms — not biasing toward %s",
                len(kept), ", ".join(terms[i:]),
            )
            break
        kept.append(term)
    return ("Glossary: " + ", ".join(kept) + ".") if kept else None


def apply_replacements(text: str, vocab: Vocabulary) -> str:
    """Case-insensitive whole-phrase substitution, longest phrase first.

    Lookarounds rather than \\b so terms that start or end with punctuation
    (C++, .NET) still match at their edges.
    """
    if not vocab.replacements:
        return text
    # One pass over one alternation, so a rule's output is never rewritten
    # by another rule. Later lines win when the same phrase appears twice.
    table = {wrong.lower(): right for wrong, right in vocab.replacements}
    alternation = "|".join(re.escape(w) for w in sorted(table, key=len, reverse=True))
    pattern = re.compile(r"(?<!\w)(?:" + alternation + r")(?!\w)", re.IGNORECASE)
    text = pattern.sub(lambda m: table.get(m.group(0).lower(), m.group(0)), text)
    return re.sub(r"\s{2,}", " ", text).strip()


class VocabularyFile:
    """Reads the vocabulary file, re-reading only when it changes on disk."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._mtime: float | None = None
        self._vocab = Vocabulary()

    def get(self) -> Vocabulary:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self._vocab = None, Vocabulary()
            return self._vocab
        except OSError as exc:
            log.warning("vocabulary: cannot stat %s: %s", self.path, exc)
            return self._vocab
        if mtime != self._mtime:
            try:
                self._vocab = parse(self.path.read_text())
                log.info("vocabulary: %d terms, %d replacements from %s",
                         len(self._vocab.terms), len(self._vocab.replacements), self.path)
            except Exception as exc:
                log.error("vocabulary: cannot read %s: %s", self.path, exc)
                self._vocab = Vocabulary()
            self._mtime = mtime
        return self._vocab

    def ensure_exists(self) -> Path:
        """Create the file from the commented template if it is missing."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(TEMPLATE)
        return self.path
