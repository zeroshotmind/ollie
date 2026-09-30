"""Custom vocabulary unit tests. Run: .venv/bin/python -m pytest tests -q"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ollie.vocabulary import (
    Vocabulary,
    VocabularyFile,
    apply_replacements,
    build_prompt,
    parse,
)


def test_parse_terms_rules_and_comments():
    vocab = parse("# header\n\nKubernetes\n  C#  \ncloud code -> Claude Code\n -> nothing\n")
    assert vocab.terms == ["Kubernetes", "C#"]
    assert vocab.replacements == [("cloud code", "Claude Code")]


def test_prompt_includes_rule_targets_once():
    vocab = Vocabulary(terms=["Ollie", "claude code"],
                       replacements=[("cloud code", "Claude Code")])
    assert build_prompt(vocab) == "Glossary: Ollie, claude code."


def test_prompt_empty_is_none():
    assert build_prompt(Vocabulary()) is None


def test_prompt_respects_budget():
    vocab = Vocabulary(terms=[f"term{i}" for i in range(500)])
    prompt = build_prompt(vocab, count_tokens=lambda s: len(s.split()), budget=10)
    assert prompt == "Glossary: " + ", ".join(f"term{i}" for i in range(9)) + "."


def test_replacements_whole_phrase_case_insensitive():
    vocab = Vocabulary(replacements=[("cloud code", "Claude Code"), ("olly", "Ollie")])
    assert apply_replacements("Open Cloud Code, olly.", vocab) == "Open Claude Code, Ollie."
    assert apply_replacements("jollyrancher", vocab) == "jollyrancher"


def test_replacements_longest_first_and_punctuated_terms():
    vocab = Vocabulary(replacements=[("see", "C"), ("see plus plus", "C++"),
                                     ("C++", "cpp")])
    assert apply_replacements("I see plus plus", vocab) == "I C++"
    assert apply_replacements("write C++ now", vocab) == "write cpp now"


def test_replacement_to_empty_removes_phrase():
    vocab = Vocabulary(replacements=[("um", "")])
    assert apply_replacements("so um yes", vocab) == "so yes"


def test_file_reloads_on_change(tmp_path):
    path = tmp_path / "vocabulary.txt"
    vf = VocabularyFile(path)
    assert not vf.get()
    vf.ensure_exists()
    assert not vf.get()          # template is all comments
    path.write_text("Kubernetes\n")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert vf.get().terms == ["Kubernetes"]
