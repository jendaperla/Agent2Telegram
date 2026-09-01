"""Config-defined shell commands answered at bridge level (no agent turn)."""
import unittest

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


def _bridge(shell_commands):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7],
                   tmux_session="a2t", shell_commands=shell_commands)
    b.tg = _FakeClient()
    return b


class ShellCommandTests(unittest.TestCase):
    def test_runs_and_replies_with_output(self):
        b = _bridge({"hello": "echo world"})
        self.assertTrue(b._handle_command("/hello", 7))
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("world", b.tg.sent[0][1])

    def test_nonzero_exit_is_visible(self):
        b = _bridge({"boom": "echo oops >&2; exit 3"})
        self.assertTrue(b._handle_command("/boom", 7))
        msg = b.tg.sent[0][1]
        self.assertIn("exit 3", msg)
        self.assertIn("oops", msg)

    def test_unknown_command_falls_through_to_agent(self):
        b = _bridge({"hello": "echo world"})
        self.assertFalse(b._handle_command("/unrelated", 7))
        self.assertEqual(b.tg.sent, [])

    def test_output_truncated(self):
        b = _bridge({"big": "python3 -c \"print('x' * 10000)\""})
        self.assertTrue(b._handle_command("/big", 7))
        msg = b.tg.sent[0][1]
        self.assertLess(len(msg), 4000)
        self.assertIn("truncated", msg)

    def test_builtin_commands_win_over_config(self):
        # /status z configu se nikdy nespustí — vestavěné příkazy mají přednost
        b = _bridge({"status": "echo hijacked"})
        b.cfg.elevenlabs_api_key = ""
        b._voice_on = False
        self.assertTrue(b._handle_command("/status", 7))
        self.assertNotIn("hijacked", b.tg.sent[0][1])


if __name__ == "__main__":
    unittest.main()
