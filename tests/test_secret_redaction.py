import json
from pathlib import Path
import tempfile
import unittest
from tests.cases import cases, expand_cases
from agent2telegram import readers
from agent2telegram.background import SubagentWatcher, _Agent
from agent2telegram.redaction import redact

SECRET = "SyntheticCredentialNeverPublish123456789"


def samples():
    for key in ("api_key","apiKey","token","refresh_token","password","secret","authorization"):
        yield json.dumps({key: SECRET})
        yield "tool --" + key + " " + SECRET
    yield "X=sk-proj-" + SECRET + " command"
    yield "Authorization: Bearer " + SECRET
    yield "https://user:" + SECRET + "@example.org/?token=" + SECRET
    yield "https://api.example.org/bot123456789:" + SECRET + "/getMe"
    yield json.dumps(json.dumps({'api_key': SECRET}))
    yield 'echo ' + json.dumps(json.dumps({'password': SECRET}))


@expand_cases
class SecretRedactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_env_keeps_port_and_mode(self):
        text="PORT=8080\nTELEGRAM_BOT_TOKEN=123456789:"+SECRET+"\nMODE=prod"
        assert redact(text)=="PORT=8080\nTELEGRAM_BOT_TOKEN=[REDACTED]\nMODE=prod"

    def test_env_replacement_has_no_extra_bracket(self):
        assert redact("TELEGRAM_BOT_TOKEN="+SECRET)=="TELEGRAM_BOT_TOKEN=[REDACTED]"

    def test_empty_secret_does_not_consume_next_line(self):
        text="TOKEN=\nMODE=prod\nPORT=8080"
        assert redact(text)==text

    @cases(text=["TOKEN=synthetic-value", "TOKEN='synthetic value'", "TOKEN=[REDACTED]", "tool --token synthetic-value", "TOKEN=https://example.org/?token=synthetic-value"])
    def test_redaction_is_idempotent(self, text):
        once=redact(text)
        assert "[REDACTED]]" not in once
        assert redact(once)==once

    @cases(key=["KEY","SERVICE_KEY","AUTH","AUTH_HEADER","TOKEN_FILE","PASSWORD","SECRET_ACCESS_KEY"])
    def test_env_secret_keys(self, key):
        assert redact(key+"=synthetic-value")==key+"=[REDACTED]"

    def test_env_recognizes_token_value_under_neutral_key(self):
        assert redact("X=sk-proj-"+SECRET)=="X=[REDACTED]"

    def test_safe_text_and_selective_json_unchanged(self):
        text="PORT=8080\nMODE=prod\nMONKEY=capuchin\ngit status --short"
        assert redact(text)==text
        assert json.loads(redact(json.dumps({"PORT":8080,"MODE":"prod","token":SECRET})))=={"PORT":8080,"MODE":"prod","token":"[REDACTED]"}

    def test_redaction_before_truncation_and_all_summary_inputs(self):
        for sample in samples():
            for text in (sample, "prefix " * 6 + sample):
                for name, key in (("Bash","command"),("Bash","description"),("Grep","pattern"),("WebSearch","query"),("Agent","description")):
                    rendered = readers._claude_tool_summary(name, {key:text})
                    assert SECRET[:6] not in rendered
                assert SECRET[:6] not in readers._short(text, 1000)
                a = _Agent(path=self.root/"agent.jsonl", description="test", started=0)
                watcher = SubagentWatcher(None, None, transcript=lambda:None, state_path=None)
                watcher._apply(a, {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":text}}]}})
                assert SECRET[:6] not in a.last_tool
        assert SECRET[:6] not in readers._claude_tool_summary("WebFetch", {"url":"https://user:"+SECRET+"@example.org"})

    def test_description_redacted_before_shortening(self):
        p=self.root/"agent-1.jsonl"
        p.with_suffix(".meta.json").write_text(json.dumps({"description":"token="+SECRET}))
        assert SECRET[:6] not in SubagentWatcher._description(p)

    def test_ordinary_summary_preserved(self):
        assert readers._claude_tool_summary("Bash",{"command":"git status --short"})=="🛠️ git status --short"


if __name__ == "__main__":
    unittest.main()
