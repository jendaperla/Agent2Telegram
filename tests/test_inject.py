"""`agent2telegram inject`: deliver a text into the attached pane and wait for the
UserPromptSubmit hook to acknowledge it. Exit codes 0 ack / 1 pane not the agent /
2 no ack in time / 3 tmux or config error; one journal line per attempt."""
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent2telegram import inject, prompt_hook
from agent2telegram.session import SessionError


class FakeSession:
    """Stands in for TmuxSession; `on_inject` simulates what the TUI + hook do."""

    def __init__(self, on_inject=None, error=None):
        self.on_inject, self.error, self.sent = on_inject, error, []

    def inject(self, text):
        if self.error:
            raise self.error
        self.sent.append(text)
        if self.on_inject:
            self.on_inject(text)


class InjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfgdir = root / "config"
        self.cfgdir.mkdir()
        self.signal = root / "state" / "answer.txt"
        self.signal.parent.mkdir()
        (self.cfgdir / "inari.json").write_text(json.dumps({
            "agent": "claude-code", "token": "1:x", "allowed_user_ids": [1], "mode": "attach",
            "origin_prefix": "[TG] ", "tmux_session": "inari", "signal_file": str(self.signal),
            "session_cwd": "/home/u/inari"}), "utf-8")
        self.env = mock.patch.dict(os.environ, {"AGENT2TELEGRAM_CONFIG": str(self.cfgdir / "inari.json")})
        self.env.start()
        self.journal = mock.patch.object(inject, "_journal")
        self.lines = self.journal.start()
        self.cfg = SimpleNamespace(tmux_session="inari", signal_file=str(self.signal))

    def tearDown(self):
        self.journal.stop()
        self.env.stop()
        self.tmp.cleanup()

    def hook(self, prompt, cwd="/home/u/inari"):
        payload = {"transcript_path": "/t.jsonl", "session_id": "s", "cwd": cwd, "prompt": prompt}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            prompt_hook.main()

    def rc_line(self):
        self.assertEqual(self.lines.call_count, 1, "exactly one journal line per attempt")
        return self.lines.call_args[0][0]

    def test_ack_from_hook_returns_0(self):
        # the TUI submits the text as one line (send-keys joins lines)
        s = FakeSession(on_inject=lambda t: self.hook(" ".join(t.splitlines())))
        rc = inject.run(self.cfg, "Zacina cyklus.\nwork order je pripraveny", timeout=2, session=s, poll=0.01)
        self.assertEqual(rc, 0)
        self.assertTrue(self.rc_line().startswith("rc=0"))
        self.assertEqual(list(inject.inject_dir(str(self.signal)).glob("*")), [], "req/ack cleaned up")
        self.assertFalse((self.signal.parent / prompt_hook.PIN_NAME).exists(), "inject never moves the pin")

    def test_no_ack_returns_2(self):
        s = FakeSession(on_inject=lambda t: self.hook("něco úplně jiného"))
        self.assertEqual(inject.run(self.cfg, "Zacina cyklus", timeout=0.2, session=s, poll=0.01), 2)
        self.assertTrue(self.rc_line().startswith("rc=2"))

    def test_pane_not_agent_returns_1(self):
        s = FakeSession(error=SessionError("refusing to inject into tmux session 'inari': tmux pane is at a shell prompt (bash)"))
        self.assertEqual(inject.run(self.cfg, "x", timeout=1, session=s), 1)
        self.assertIn("shell prompt", self.rc_line())

    def test_tmux_errors_return_3(self):
        for err in (SessionError("agent session 'inari' is gone"),
                    subprocess.TimeoutExpired(["tmux"], 10),
                    subprocess.CalledProcessError(1, ["tmux"])):
            self.lines.reset_mock()
            self.assertEqual(inject.run(self.cfg, "x", timeout=1, session=FakeSession(error=err)), 3)
            self.assertTrue(self.rc_line().startswith("rc=3"))

    def test_bad_config_returns_3(self):
        self.assertEqual(inject.run(SimpleNamespace(tmux_session="", signal_file=""), "x", session=FakeSession()), 3)

    def test_hook_ack_matches_normalized_text_and_ignores_stale(self):
        d = inject.inject_dir(str(self.signal))
        d.mkdir()
        (d / "a.req").write_text(json.dumps({"digest": inject.digest("a\n  b"), "ts": time.time()}))
        (d / "old.req").write_text(json.dumps({"digest": inject.digest("a b"), "ts": time.time() - 2 * inject.REQ_TTL}))
        self.assertEqual(inject.acknowledge(str(self.signal), "  a b "), ["a"])
        self.assertTrue((d / "a.ack").exists())
        self.assertFalse((d / "old.ack").exists())

    def test_ack_sees_through_pasted_content_wrapper(self):
        # Claude Code wraps a longer bracketed paste (1213 B yes, 612 B no) before the hook sees it
        d = inject.inject_dir(str(self.signal))
        d.mkdir()
        text = "Zacina cyklus.\nwork order je pripraveny"
        (d / "n.req").write_text(json.dumps({"digest": inject.digest(text), "ts": time.time()}))
        self.assertEqual(inject.acknowledge(str(self.signal), "<pasted_content id=\"x1\">\nnoise\n</pasted_content id=\"x1\">"), [])
        self.assertEqual(inject.acknowledge(str(self.signal), "<pasted_content id=\"x1\">\nnoise\n</pasted_content id=\"x1\">\n" + text), [],
                         "a wrapper that does not enclose the whole prompt is not unwrapped")
        self.assertEqual(inject.acknowledge(str(self.signal), "<pasted_content id=\"a\">\n" + text + "\n</pasted_content id=\"b\">"), [],
                         "mismatched ids are not a wrapper")
        self.assertEqual(inject.acknowledge(str(self.signal), "<pasted_content id=\"k7Qz\">\n" + text + "\n</pasted_content id=\"k7Qz\">"), ["n"])
        self.assertTrue((d / "n.ack").exists())

    def test_ack_of_text_that_is_itself_a_wrapper(self):
        d = inject.inject_dir(str(self.signal))
        d.mkdir()
        text = "<pasted_content id=\"i\">ahoj</pasted_content id=\"i\">"
        (d / "n.req").write_text(json.dumps({"digest": inject.digest(text), "ts": time.time()}))
        self.assertEqual(inject.acknowledge(str(self.signal), "<pasted_content id=\"o\">" + text + "</pasted_content id=\"o\">"), ["n"])

    def test_long_injected_text_acks_through_wrapper_returns_0(self):
        text = "Zacina cyklus. " * 90  # ~1.3 kB, over the size where the TUI wraps the paste
        s = FakeSession(on_inject=lambda t: self.hook(f'<pasted_content id="p9">\n{t}\n</pasted_content id="p9">'))
        self.assertEqual(inject.run(self.cfg, text, timeout=2, session=s, poll=0.01), 0)
        self.assertTrue(self.rc_line().startswith("rc=0"))

    def test_hook_respects_session_cwd(self):
        d = inject.inject_dir(str(self.signal))
        d.mkdir()
        (d / "n.req").write_text(json.dumps({"digest": inject.digest("ahoj"), "ts": time.time()}))
        self.hook("ahoj", cwd="/home/u/jina-session")
        self.assertFalse((d / "n.ack").exists(), "a prompt in another session must not acknowledge")
        self.hook("ahoj")
        self.assertTrue((d / "n.ack").exists())

    def test_cli_wiring(self):
        from agent2telegram import __main__ as cli
        with mock.patch.object(inject, "run", return_value=2) as run:
            rc = cli.main(["inject", "--config", str(self.cfgdir / "inari.json"), "--timeout", "5", "ahoj"])
        self.assertEqual(rc, 2)
        cfg, text = run.call_args[0]
        self.assertEqual((cfg.tmux_session, text, run.call_args[1]["timeout"]), ("inari", "ahoj", 5.0))


if __name__ == "__main__":
    unittest.main()
