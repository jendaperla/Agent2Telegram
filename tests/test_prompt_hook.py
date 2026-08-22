"""Tests for the UserPromptSubmit hook — the pin must be written for exactly the right
prompts (bridge-injected, Claude, matching cwd) and for nothing else, and the hook must
stay silent and exit-0-safe no matter what it is fed (its stdout would be injected into
the model's context; its configs contain the bot token)."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram import prompt_hook


def _write_cfg(cfgdir: Path, name: str, **fields) -> Path:
    base = {"agent": "claude-code", "token": "1:x", "allowed_user_ids": [1],
            "mode": "attach", "origin_prefix": "[TG] "}
    base.update(fields)
    p = cfgdir / f"{name}.json"
    p.write_text(json.dumps(base), "utf-8")
    return p


class PromptHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfgdir = root / "config"
        self.cfgdir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.signal = self.state / "answer.txt"
        # config_path() honours AGENT2TELEGRAM_CONFIG; the hook scans its parent dir.
        self.env = mock.patch.dict(os.environ,
                                   {"AGENT2TELEGRAM_CONFIG": str(self.cfgdir / "config.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, payload) -> None:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        with mock.patch("sys.stdin", io.StringIO(raw)):
            prompt_hook.main()

    def _pin(self) -> dict | None:
        p = self.state / prompt_hook.PIN_NAME
        if not p.exists():
            return None
        return json.loads(p.read_text("utf-8"))

    def test_pins_bridge_prefixed_prompt(self):
        _write_cfg(self.cfgdir, "bridge", signal_file=str(self.signal))
        self._run({"transcript_path": "/home/u/.claude/projects/x/s.jsonl",
                   "session_id": "s", "cwd": "/home/u/brain", "prompt": "[TG] ahoj"})
        pin = self._pin()
        self.assertIsNotNone(pin, "prefixed prompt must be pinned")
        self.assertEqual(pin["transcript"], "/home/u/.claude/projects/x/s.jsonl")

    def test_ignores_unprefixed_and_task_notifications(self):
        """Terminal typing and background-task notifications fire this hook too (verified on
        2.1.238) — neither may move the pin."""
        _write_cfg(self.cfgdir, "bridge", signal_file=str(self.signal))
        self._run({"transcript_path": "/t.jsonl", "session_id": "s",
                   "prompt": "just typing in the terminal"})
        self._run({"transcript_path": "/t.jsonl", "session_id": "s",
                   "prompt": "<task-notification>\n<task-id>x</task-id>"})
        self.assertIsNone(self._pin())

    def test_ignores_codex_configs(self):
        """A Codex bridge has a signal_file too, but Codex has no hooks — a pin there would
        point it at a Claude transcript."""
        _write_cfg(self.cfgdir, "bridge", signal_file=str(self.signal), agent="codex")
        self._run({"transcript_path": "/t.jsonl", "session_id": "s", "prompt": "[TG] ahoj"})
        self.assertIsNone(self._pin())

    def test_scopes_by_session_cwd(self):
        """Two Claude bridges on one machine must not pin each other's transcript."""
        _write_cfg(self.cfgdir, "bridge", signal_file=str(self.signal),
                   session_cwd="/home/u/other-project")
        self._run({"transcript_path": "/t.jsonl", "session_id": "s",
                   "cwd": "/home/u/brain", "prompt": "[TG] ahoj"})
        self.assertIsNone(self._pin())

    def test_legacy_prefixes_accepted(self):
        _write_cfg(self.cfgdir, "bridge", signal_file=str(self.signal),
                   origin_prefix="Telegram:")
        self._run({"transcript_path": "/t.jsonl", "session_id": "s", "prompt": "[TG] ahoj"})
        self.assertIsNotNone(self._pin(), "legacy prefix set must keep working")

    def test_malformed_stdin_is_silent_and_harmless(self):
        """Whatever happens, the hook must print nothing (stdout goes into the model's
        context) and must not leak config contents (they hold the bot token)."""
        _write_cfg(self.cfgdir, "bridge", signal_file=str(self.signal))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self._run("this is not json")
            self._run({"prompt": "[TG] x"})           # no transcript_path
            self._run({"transcript_path": "/t"})       # no prompt
        self.assertEqual(out.getvalue(), "")
        self.assertIsNone(self._pin())


if __name__ == "__main__":
    unittest.main()
