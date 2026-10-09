import unittest
from unittest.mock import Mock
from agent2telegram.config import Config, ConfigError
from agent2telegram.attach import AttachBridge


class GroupOnlyUserTests(unittest.TestCase):
    def bridge(self, **kw):
        cfg = Config(agent="claude-code", token="fixture:only", allowed_user_ids=[11, 22], **kw)
        b = object.__new__(AttachBridge)
        b.cfg, b._allowed, b.tg = cfg, {11, 22}, Mock()
        b._owner_chat = 11
        b._inject = Mock()
        b._handle_command = Mock(return_value=False)
        return b

    def msg(self, user, chat, kind):
        return {"message": {"from": {"id": user}, "chat": {"id": chat, "type": kind},
                            "message_id": 5, "text": "Ahoj"}}

    def run_msg(self, b, *a):
        try: b._handle(self.msg(*a))
        except Exception: pass

    def test_group_only_user_rejected_in_private(self):
        b = self.bridge(group_only_user_ids=[22], reply_to_origin_chat=True)
        self.run_msg(b, 22, 22, "private")
        b._inject.assert_not_called()
        self.assertEqual(b._owner_chat, 11)
        b.tg.send_message.assert_called_once()

    def test_group_only_user_accepted_in_allowed_group(self):
        b = self.bridge(group_only_user_ids=[22], allowed_group_ids=[-500], reply_to_origin_chat=True)
        self.run_msg(b, 22, -500, "supergroup")
        self.assertEqual(b._owner_chat, -500)

    def test_foreign_group_rejected_for_everyone(self):
        b = self.bridge(group_only_user_ids=[22], allowed_group_ids=[-500], reply_to_origin_chat=True)
        for user in (11, 22):
            self.run_msg(b, user, -999, "supergroup")
        b._inject.assert_not_called()
        self.assertEqual(b._owner_chat, 11)

    def test_owner_private_still_works(self):
        b = self.bridge(group_only_user_ids=[22], allowed_group_ids=[-500], reply_to_origin_chat=True)
        self.run_msg(b, 11, -500, "supergroup")
        self.run_msg(b, 11, 11, "private")
        self.assertEqual(b._owner_chat, 11)

    def test_validation(self):
        for kw in ({"group_only_user_ids": [11]}, {"group_only_user_ids": ["x"]},
                   {"allowed_group_ids": [5]}):
            with self.assertRaises(ConfigError):
                Config(agent="codex", token="fixture:only", allowed_user_ids=[11, 22], **kw).validate()


if __name__ == "__main__":
    unittest.main()
