import unittest
from unittest.mock import Mock
from agent2telegram.config import Config, ConfigError
from agent2telegram.mentions import accepts_message
from agent2telegram.attach import AttachBridge
from agent2telegram.bridge import Bridge


class MentionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(agent="codex", token="fixture:only", allowed_user_ids=[11],
                          bot_username="example_bot", require_mention=True)
        self.msg = {"from": {"id": 11}, "chat": {"id": -99, "type": "supergroup"}, "text": "Ahoj"}

    def test_default_and_private(self):
        self.assertFalse(Config(agent="codex", token="fixture:only").require_mention)
        self.assertTrue(accepts_message(self.cfg, {**self.msg, "chat": {"type": "private"}}))

    def test_group_mentions(self):
        self.assertFalse(accepts_message(self.cfg, self.msg))
        for text in ("@example_bot Ahoj", "Ahoj @EXAMPLE_BOT", "/start@example_bot"):
            self.assertTrue(accepts_message(self.cfg, {**self.msg, "text": text}))
        for text in ("@example_bot_fake", "@@example_bot", "@otherbot"):
            self.assertFalse(accepts_message(self.cfg, {**self.msg, "text": text}))

    def test_reply_only_to_same_bot(self):
        reply = {"from": {"is_bot": True, "username": "example_bot"}}
        self.assertTrue(accepts_message(self.cfg, {**self.msg, "reply_to_message": reply}))
        reply["from"]["username"] = "anotherbot"
        self.assertFalse(accepts_message(self.cfg, {**self.msg, "reply_to_message": reply}))

    def test_both_live_dispatch_paths_drop_without_injecting(self):
        for cls, method in ((AttachBridge, "_handle"), (Bridge, "_dispatch")):
            b = object.__new__(cls)
            b.cfg, b._allowed, b.tg = self.cfg, {11}, Mock()
            b._inject = Mock(side_effect=AssertionError("must not inject"))
            getattr(b, method)({"message": self.msg})
            b.tg.send_message.assert_not_called()

    def test_invalid_boolean(self):
        self.cfg.require_mention = "false"
        with self.assertRaises(ConfigError):
            self.cfg.validate()

    def test_bare_commands_and_queue_ack_are_silent_without_mention(self):
        for cls, method in ((AttachBridge, "_handle"), (Bridge, "_dispatch")):
            b = object.__new__(cls)
            b.cfg, b._allowed, b.tg = self.cfg, {11}, Mock()
            b._handle_command = Mock(side_effect=AssertionError("unmentioned command"))
            getattr(b, method)({"message": {**self.msg, "text": "/status"}})
            b.tg.send_message.assert_not_called()
        b = object.__new__(AttachBridge)
        b.cfg, b._allowed, b.tg = self.cfg, {11}, Mock()
        b._turn_active = Mock()
        b._turn_active.is_set.return_value = True
        b._maybe_ack_queued({"message": self.msg})
        b.tg.send_message.assert_not_called()
