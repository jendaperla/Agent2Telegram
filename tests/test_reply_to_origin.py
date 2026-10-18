import unittest
from unittest.mock import Mock
from agent2telegram.config import Config, ConfigError
from agent2telegram.attach import AttachBridge


class ReplyToOriginTests(unittest.TestCase):
    def bridge(self, enabled):
        cfg = Config(agent="claude-code", token="fixture:only", allowed_user_ids=[11, 22],
                     reply_to_origin_chat=enabled)
        b = object.__new__(AttachBridge)
        b.cfg, b._allowed, b.tg = cfg, {11, 22}, Mock()
        b._owner_chat = 11
        b._inject = Mock()
        b._handle_command = Mock(return_value=False)
        return b

    def msg(self, user, chat, kind="supergroup"):
        return {"message": {"from": {"id": user}, "chat": {"id": chat, "type": kind},
                            "message_id": 5, "text": "Hello"}}

    def test_default_is_off_and_validated(self):
        self.assertFalse(Config(agent="codex", token="fixture:only").reply_to_origin_chat)
        cfg = Config(agent="codex", token="fixture:only", reply_to_origin_chat="yes")
        with self.assertRaises(ConfigError):
            cfg.validate()

    def test_off_keeps_owner_chat(self):
        b = self.bridge(False)
        try: b._handle(self.msg(22, -500))
        except Exception: pass
        self.assertEqual(b._owner_chat, 11)

    def test_on_follows_last_authorized_chat(self):
        b = self.bridge(True)
        try: b._handle(self.msg(22, -500))
        except Exception: pass
        self.assertEqual(b._owner_chat, -500)
        try: b._handle(self.msg(11, 11, "private"))
        except Exception: pass
        self.assertEqual(b._owner_chat, 11)

    def test_unauthorized_never_redirects(self):
        b = self.bridge(True)
        b._handle(self.msg(99, -777))
        self.assertEqual(b._owner_chat, 11)


if __name__ == "__main__":
    unittest.main()


class GroupMarkerTests(unittest.TestCase):
    def test_marker_routes_to_first_allowed_group_and_is_stripped(self):
        cfg = Config(agent="claude-code", token="fixture:only", allowed_user_ids=[11],
                     reply_to_origin_chat=True, allowed_group_ids=[-500], group_marker="[team]")
        b = object.__new__(AttachBridge)
        b.cfg, b._owner_chat = cfg, 11
        self.assertEqual(b._route_group_marker("[team] Hello everyone"), ("Hello everyone", -500))
        # Only this reply goes to the group; the turn's recipient stays the owner's DM.
        self.assertEqual(b._owner_chat, 11)

    def test_no_marker_or_no_group_keeps_chat(self):
        cfg = Config(agent="claude-code", token="fixture:only", allowed_user_ids=[11], group_marker="[team]")
        b = object.__new__(AttachBridge)
        b.cfg, b._owner_chat = cfg, 11
        self.assertEqual(b._route_group_marker("[team] x"), ("[team] x", 11))
        self.assertEqual(b._route_group_marker("Hello"), ("Hello", 11))
        self.assertEqual(b._owner_chat, 11)
