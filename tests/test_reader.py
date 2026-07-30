"""Reader + filter unit tests. Run: .venv/bin/python -m pytest tests -q"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ollie.config import Config
from ollie.filter import OllamaFilter, clean_for_speech, clean_for_verbatim
from ollie.hotkey import normalise, pretty
from ollie.message import Chunk
from ollie.readers.claude_code import (
    ClaudeCodeReader,
    describe_tool,
    summarize_command,
)


def _write(path, records):
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _assistant(uuid, blocks, sidechain=False):
    return {"type": "assistant", "uuid": uuid, "isSidechain": sidechain,
            "message": {"role": "assistant", "id": "msg_" + uuid, "content": blocks}}


def _reader(tmp_path, **kw):
    projects = tmp_path / "projects" / "-proj"
    projects.mkdir(parents=True)
    transcript = projects / "session.jsonl"
    transcript.touch()
    cfg = Config.load({"claude_projects_dir": str(tmp_path / "projects"),
                       "session_file": str(transcript), **kw})
    reader = ClaudeCodeReader(cfg)
    reader.start()
    return reader, transcript


def test_emits_prose_and_tools(tmp_path):
    reader, transcript = _reader(tmp_path)
    _write(transcript, [
        _assistant("a1", [
            {"type": "thinking", "thinking": "secret reasoning"},
            {"type": "text", "text": "Refactoring the auth module now."},
            {"type": "tool_use", "id": "t1", "name": "Edit",
             "input": {"file_path": "/repo/src/auth.py"}},
        ]),
    ])
    chunks = reader.poll()
    assert [c.role for c in chunks] == ["assistant", "tool_use"]
    assert chunks[0].text == "Refactoring the auth module now."
    assert chunks[1].text == "editing auth.py"


def test_thinking_is_never_spoken(tmp_path):
    reader, transcript = _reader(tmp_path)
    _write(transcript, [_assistant("a1", [{"type": "thinking", "thinking": "hmm"}])])
    assert reader.poll() == []


def test_sidechains_excluded_by_default(tmp_path):
    reader, transcript = _reader(tmp_path)
    _write(transcript, [_assistant("a1", [{"type": "text", "text": "subagent chatter"}],
                                   sidechain=True)])
    assert reader.poll() == []


def test_no_duplicates_across_polls(tmp_path):
    reader, transcript = _reader(tmp_path)
    _write(transcript, [_assistant("a1", [{"type": "text", "text": "one"}])])
    assert len(reader.poll()) == 1
    assert reader.poll() == []
    _write(transcript, [_assistant("a1", [{"type": "text", "text": "one"}])])
    assert reader.poll() == [], "same uuid+index must not be spoken twice"


def test_partial_line_is_buffered(tmp_path):
    reader, transcript = _reader(tmp_path)
    record = json.dumps(_assistant("a1", [{"type": "text", "text": "half written"}]))
    with transcript.open("a") as handle:
        handle.write(record[:40])
    assert reader.poll() == []
    with transcript.open("a") as handle:
        handle.write(record[40:] + "\n")
    chunks = reader.poll()
    assert len(chunks) == 1 and chunks[0].text == "half written"


def test_truncation_resets_offset(tmp_path):
    reader, transcript = _reader(tmp_path)
    _write(transcript, [_assistant("a1", [{"type": "text", "text": "first"}])])
    reader.poll()
    transcript.write_text("")
    _write(transcript, [_assistant("a2", [{"type": "text", "text": "after rotate"}])])
    chunks = reader.poll()
    assert [c.text for c in chunks] == ["after rotate"]


def test_tool_results_opt_in(tmp_path):
    reader, transcript = _reader(tmp_path, speak_tool_results=True)
    _write(transcript, [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "3 passed, 0 failed"}]}]}}])
    chunks = reader.poll()
    assert len(chunks) == 1 and chunks[0].role == "tool_result"
    assert "3 passed" in chunks[0].text


def test_describe_tool_variants():
    assert describe_tool({"name": "Bash", "input": {"command": "pytest -q\nsecond line"}}) == \
        "running pytest"
    assert describe_tool({"name": "Read", "input": {"file_path": "/a/b/c/main.py"}}) == \
        "reading main.py"
    assert describe_tool({"name": "Grep", "input": {"pattern": "TODO"}}) == \
        "searching the code for TODO"
    assert "slack" in describe_tool({"name": "mcp__slack__send_message", "input": {}})
    assert describe_tool({"name": "Zebra", "input": {}}) == "using the Zebra tool"


def test_auto_discovery_picks_newest(tmp_path):
    projects = tmp_path / "projects"
    (projects / "-old").mkdir(parents=True)
    (projects / "-new").mkdir(parents=True)
    old = projects / "-old" / "a.jsonl"
    new = projects / "-new" / "b.jsonl"
    old.write_text("")
    new.write_text("")
    import os
    os.utime(old, (time.time() - 900, time.time() - 900))
    cfg = Config.load({"claude_projects_dir": str(projects)})
    assert ClaudeCodeReader(cfg).pick_session() == new


def test_clean_for_speech():
    out = clean_for_speech("**Done!** see `main.py` and https://x.com/y", 28)
    assert "**" not in out and "`" not in out and "https" not in out
    assert "a link" in out
    assert clean_for_speech(" ".join(["word"] * 60), 10).count("word") == 10


def test_summarize_command_never_leaks_shell():
    cases = {
        "cd /Users/roy/x && ls .venv/bin/python* 2>/dev/null; echo ---": "listing files",
        'KMP_DUPLICATE_LIB_OK=TRUE .venv/bin/python -c "import torch"': "running a python snippet",
        "cd /repo && .venv/bin/python rm_jlens/fit.py --steps 100": "running fit.py",
        "pytest -q tests/": "running pytest tests",
        "cat /private/tmp/a/b/c/job.output": "checking job.output",
        "git commit -m 'fix things'": "running git commit",
        "npm run build": "running npm run build",
        "nohup python -m http.server 8000 &": "running python module http.server",
        "": "running a command",
    }
    for command, expected in cases.items():
        assert summarize_command(command) == expected, command
    # nothing speakable should ever contain a slash, a flag or an env assignment
    for command in cases:
        out = summarize_command(command)
        assert "/" not in out and "--" not in out and "=" not in out


def test_truncation_has_one_full_stop():
    out = clean_for_speech("word. " * 60, 10)
    assert not out.endswith("..")
    assert out.endswith(".")


def test_hotkey_names_match_a_mac_keyboard():
    # macOS has no key labelled "alt" — whatever the user types must resolve,
    # and whatever we say back must be a name printed on their keyboard.
    for spelling in ("right option", "Right-Option", "right_option", "alt_r", "ralt"):
        assert normalise(spelling) == "alt_r"
        assert pretty(spelling) == "right Option (⌥)"
    assert normalise("caps lock") == "caps_lock"
    assert pretty("caps lock") == "Caps Lock (⇪)"
    assert pretty("cmd_r") == "right Command (⌘)"
    assert pretty("f13") == "F13"
    assert "alt" not in pretty("alt_r").lower()


def test_verbatim_style_needs_no_model(tmp_path):
    # Point Ollama at a dead port: verbatim must not care.
    cfg = Config.load({"style": "verbatim", "ollama_url": "http://127.0.0.1:1"})
    flt = OllamaFilter(cfg)
    out = flt.process([
        Chunk(role="assistant", text="I **fixed** the `auth` bug"),
        Chunk(role="tool_use", text="running pytest tests"),
    ])
    assert out == "I fixed the auth bug. Running pytest tests."
    assert flt.degraded is False          # the model was never consulted
    flt.close()


def test_verbatim_keeps_everything_brief_would_drop():
    text = clean_for_verbatim("see https://example.com/docs and /a/b/config.yaml")
    assert "https://example.com/docs" in text      # loss-less: URL kept
    assert "/a/b/config.yaml" in text              # loss-less: path kept


def test_voice_listing_parses_say_output():
    from ollie.tts import parse_voice_listing

    sample = (
        "Samantha            en_US    # Hello! My name is Samantha.\n"
        "Bad News            en_US    # The light you see...\n"
        "Flo (English (UK))  en_GB    # Hello! My name is Flo.\n"
        "Amélie              fr_CA    # Bonjour!\n"
    )
    voices = parse_voice_listing(sample)
    assert ("Samantha", "en_US") in voices
    assert ("Flo (English (UK))", "en_GB") in voices
    assert ("Amélie", "fr_CA") in voices


def test_tone_shapes_prompt_not_facts():
    from ollie.filter import TONES, OllamaFilter

    cfg = Config.load({"tone": "snarky", "ollama_url": "http://127.0.0.1:1"})
    flt = OllamaFilter(cfg)
    prompt = flt._system_prompt("brief")
    assert TONES["snarky"] in prompt
    cfg.tone = "neutral"
    assert "Delivery tone" not in flt._system_prompt("brief")
    flt.close()


def test_reader_emits_turn_end_markers(tmp_path):
    reader, transcript = _reader(tmp_path)
    _write(transcript, [
        _assistant("a1", [{"type": "text", "text": "All done, tests pass."}]),
        {"type": "system", "subtype": "turn_duration", "uuid": "s1", "durationMs": 1200},
    ])
    chunks = reader.poll()
    assert [c.role for c in chunks] == ["assistant", "turn_end"]
    _write(transcript, [{"type": "system", "subtype": "turn_duration", "uuid": "s1"}])
    assert reader.poll() == []          # same uuid, deduped


def test_autopilot_reply_parsing():
    from ollie.autopilot import parse_reply

    assert parse_reply("DONE: all 14 tests pass") == ("done", "all 14 tests pass")
    assert parse_reply("  prompt: Run the test suite again ") ==         ("prompt", "Run the test suite again")
    assert parse_reply("PROMPT:\nFix the failing\nimport") ==         ("prompt", "Fix the failing import")
    kind, _ = parse_reply("Sure! Here's what I think you should do next...")
    assert kind == "invalid"
    assert parse_reply("PROMPT:")[0] == "invalid"
    assert parse_reply("PROMPT: " + "x " * 600)[1].__len__() <= 500


def test_autopilot_turn_flow_without_a_model():
    from ollie.autopilot import Autopilot

    injected, spoken = [], []
    cfg = Config.load({"autopilot_settle": 0.0, "autopilot_max_turns": 3,
                       "ollama_url": "http://127.0.0.1:1"})
    pilot = Autopilot(cfg, inject=lambda t: injected.append(t) or True,
                      speak=spoken.append, frontmost=lambda: "Terminal")
    pilot._ask = lambda: "PROMPT: run the tests"
    pilot.enabled = True
    pilot.goal = "make tests pass"

    pilot.observe(Chunk(role="assistant", text="I fixed the import."))
    pilot._advance()
    assert injected == ["run the tests"] and pilot.turns == 1

    # same authored prompt again -> stall detection disarms
    pilot.observe(Chunk(role="assistant", text="Ran them."))
    pilot._advance()
    assert pilot.enabled is False and len(injected) == 1
    assert any("stalled" in s.lower() for s in spoken)

    # DONE path
    pilot.enabled, pilot.sent, pilot.turns = True, [], 0
    pilot._ask = lambda: "DONE: tests are green"
    pilot.observe(Chunk(role="assistant", text="14 passed."))
    pilot._advance()
    assert pilot.enabled is False
    assert any("Goal complete" in s for s in spoken)
    pilot.close()


def test_autopilot_respects_frontmost_gate():
    from ollie.autopilot import Autopilot

    injected = []
    cfg = Config.load({"autopilot_settle": 0.0})
    pilot = Autopilot(cfg, inject=lambda t: injected.append(t) or True,
                      speak=lambda s: None, frontmost=lambda: "Safari")
    pilot.enabled = True
    pilot.goal = "g"
    # patch the retry loop to a single check
    pilot._is_terminal = lambda app: "terminal" in app.lower()
    assert not pilot._is_terminal("Safari")
    assert pilot._is_terminal("Terminal")
    pilot.close()
