import json
from pathlib import Path
from agent2telegram import readers
from agent2telegram.background import SubagentWatcher, _Agent

SECRET = "SyntheticCredentialNeverPublish123456789"

def samples():
    for key in ("api_key","apiKey","token","refresh_token","password","secret","authorization"):
        yield json.dumps({key: SECRET})
        yield "tool --" + key + " " + SECRET
    yield "X=" + SECRET + " command"
    yield "Authorization: Bearer " + SECRET
    yield "https://user:" + SECRET + "@example.org/?token=" + SECRET
    yield "https://api.example.org/bot123456789:" + SECRET + "/getMe"
    yield json.dumps(json.dumps({'api_key': SECRET}))
    yield 'echo ' + json.dumps(json.dumps({'password': SECRET}))

def test_redaction_before_truncation_and_all_summary_inputs(tmp_path):
    for sample in samples():
        for text in (sample, "prefix " * 6 + sample):
            for name, key in (("Bash","command"),("Bash","description"),("Grep","pattern"),("WebSearch","query"),("Agent","description")):
                rendered = readers._claude_tool_summary(name, {key:text})
                assert SECRET[:6] not in rendered
            assert SECRET[:6] not in readers._short(text, 1000)
            a = _Agent(path=tmp_path/"agent.jsonl", description="test", started=0)
            watcher = SubagentWatcher(None, None, transcript=lambda:None, state_path=None)
            watcher._apply(a, {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":text}}]}})
            assert SECRET[:6] not in a.last_tool
    assert SECRET[:6] not in readers._claude_tool_summary("WebFetch", {"url":"https://user:"+SECRET+"@example.org"})

def test_description_redacted_before_shortening(tmp_path):
    p=tmp_path/"agent-1.jsonl"
    p.with_suffix(".meta.json").write_text(json.dumps({"description":"token="+SECRET}))
    assert SECRET[:6] not in SubagentWatcher._description(p)

def test_ordinary_summary_preserved():
    assert readers._claude_tool_summary("Bash",{"command":"git status --short"})=="🛠️ git status --short"
