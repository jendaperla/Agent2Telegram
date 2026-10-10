"""Security audit 2026-10-10 (bridge-reply-routing): a reply must reach the chat its turn came
from, never the chat of a message that arrived in between, and model text must not pick a
recipient on its own."""
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent2telegram import attach
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config
from agent2telegram.durable import DurableOutbox
from agent2telegram.redaction import RedactingFilter

OWNER, OTHER, GROUP = 11, 22, -500


def make_bridge(tmp):
    cfg = Config(agent="claude-code", token="fixture:only", allowed_user_ids=[OWNER, OTHER],
                 reply_to_origin_chat=True, allowed_group_ids=[GROUP], group_marker="[skupina]")
    b = object.__new__(AttachBridge)
    b.cfg, b._allowed, b.tg = cfg, {OWNER, OTHER}, Mock()
    b._owner_chat = OWNER
    b._use_durable_outbox = True
    b._outbox = DurableOutbox(Path(tmp) / "queue")
    b._pending_send, b._pending_files, b._sent_keys = [], [], set()
    b._mark_sent = Mock(return_value=True)
    b._voice_reply_on = Mock(return_value=False)
    b._turn_text_sent = False
    b._turn_active, b._stop = threading.Event(), threading.Event()
    b._inject = Mock(return_value=True)
    b._handle_command = Mock(return_value=False)
    b._begin_turn = Mock()
    return b


def msg(user, chat, kind):
    return {"message": {"from": {"id": user}, "chat": {"id": chat, "type": kind},
                        "message_id": 5, "text": "Ahoj"}}


class ReplyBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = make_bridge(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sent_to(self):
        return [c.args[0] for c in self.b.tg.send_message.call_args_list]

    def test_queued_private_reply_is_not_flushed_into_a_later_group(self):
        self.b.tg.send_message.side_effect = OSError("network down")
        self.b._send_final("soukromá odpověď")            # queued for the owner's DM
        self.b._flush_pending()
        self.b._owner_chat = GROUP                        # a group message switched the chat
        self.b.tg.send_message.reset_mock(side_effect=True)
        self.b._flush_pending()
        self.assertEqual(self.sent_to(), [OWNER])

    def test_group_message_waits_until_the_private_turn_ends(self):
        self.b._turn_active.set()
        orig = attach.CHAT_SWITCH_POLL
        attach.CHAT_SWITCH_POLL = 0.01
        try:
            t = threading.Thread(target=self.b._handle, args=(msg(OTHER, GROUP, "supergroup"),))
            t.start()
            time.sleep(0.1)
            self.assertEqual(self.b._owner_chat, OWNER)   # still answering the DM
            self.b._inject.assert_not_called()
            self.b._turn_active.clear()
            t.join(2)
        finally:
            attach.CHAT_SWITCH_POLL = orig
        self.assertEqual(self.b._owner_chat, GROUP)
        self.b._inject.assert_called_once()

    def test_same_chat_does_not_wait(self):
        self.b._turn_active.set()
        self.b._handle(msg(OWNER, OWNER, "private"))
        self.b._inject.assert_called_once()

    def test_marker_ignored_in_a_group_turn(self):
        self.b._owner_chat = GROUP
        self.assertEqual(self.b._route_group_marker("[skupina] x"), ("x", GROUP))

    def test_marker_ignored_in_another_users_dm(self):
        self.b._owner_chat = OTHER
        self.assertEqual(self.b._route_group_marker("[skupina] x"), ("x", OTHER))

    def test_marker_from_owner_dm_routes_one_reply_only(self):
        self.b._send_final("[skupina] do skupiny")
        self.b._send_final("zpátky soukromě")
        self.b._flush_pending()
        self.assertEqual(self.sent_to(), [GROUP, OWNER])
        self.assertEqual(self.b._owner_chat, OWNER)

    def test_outbox_record_keeps_its_chat(self):
        rid = self.b._outbox.enqueue(["a"], [], None, chat=GROUP)
        self.assertEqual(self.b._outbox.head().chat, GROUP)
        self.assertEqual(self.b._outbox.head().record_id, rid)


class LogRedactionTests(unittest.TestCase):
    def test_api_key_in_a_preview_is_masked(self):
        key = "AIza" + "Sy" + "A" * 33
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "IN  %r", (key[:40],), None)
        RedactingFilter().filter(rec)
        self.assertNotIn(key[:20], rec.getMessage())
        self.assertIn("[REDACTED]", rec.getMessage())


if __name__ == "__main__":
    unittest.main()
