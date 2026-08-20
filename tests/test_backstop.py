"""Tests for the turn-end backstop — the safety net must never deliver a reply twice.

Regression cover for upstream issue #3: the backstop called `_send_final()` without a dedup key,
so it bypassed the `attach_sent.txt` ledger and could re-send a reply the normal path had already
forwarded (or, when the new answer wasn't in the transcript yet, the *previous* turn's reply).
"""
import time
import unittest

from agent2telegram.attach import AttachBridge


class _FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat, text):
        self.sent.append((chat, text))


def _bridge(last_text, last_key, already_sent=()):
    """A bare bridge with just what the backstop path touches (no tmux, no transcript)."""
    b = object.__new__(AttachBridge)
    b._marker = "[TG]"
    b.tg = _FakeTelegram()
    b._owner_chat = 42
    b._turn_from_tg = True
    b._turn_text_sent = False          # the turn flag says "nothing forwarded" ...
    b._sent_keys = set(already_sent)   # ... but the ledger may know better
    b._pending_send = False
    b._turn_seq = 7
    b._backstop_seq = 7
    b._backstop_due = time.monotonic() - 1.0   # due a second ago → fires on this call
    b._last_assistant_text = lambda: (last_text, last_key)
    b._mark_sent = lambda key: b._sent_keys.add(key)
    b._enqueue = lambda text, key: None
    return b


class BackstopDedupTests(unittest.TestCase):
    def test_does_not_resend_reply_already_in_ledger(self):
        """The normal path forwarded this reply (its uuid is in the ledger) but `_turn_text_sent`
        is still False — the race from issue #3. The backstop must stay silent."""
        b = _bridge("Hotovo.", "uuid-1", already_sent={"uuid-1"})
        b._check_backstop()
        self.assertEqual(b.tg.sent, [], "backstop re-delivered an already-forwarded reply")

    def test_does_not_resend_previous_turns_reply(self):
        """The new answer isn't written to the transcript yet, so the tail scan returns the
        *previous* turn's reply — whose uuid is in the ledger. Must not go out again."""
        b = _bridge("Odpoved z minuleho tahu.", "uuid-old", already_sent={"uuid-old", "uuid-older"})
        b._check_backstop()
        self.assertEqual(b.tg.sent, [], "backstop re-sent a stale reply from an earlier turn")

    def test_still_delivers_when_nothing_was_sent(self):
        """The backstop must keep doing its job: an unanswered turn still gets its reply."""
        b = _bridge("Skutecne nedorucena odpoved.", "uuid-new")
        b._check_backstop()
        self.assertEqual(len(b.tg.sent), 1, "backstop failed to deliver an unanswered turn")
        self.assertEqual(b.tg.sent[0][1], "Skutecne nedorucena odpoved.")

    def test_marks_the_ledger_so_it_cannot_fire_twice(self):
        """After the backstop delivers, the key must be recorded — a second call stays silent."""
        b = _bridge("Jedina odpoved.", "uuid-new")
        b._check_backstop()
        b._backstop_due = time.monotonic() - 1.0      # arm it again
        b._turn_text_sent = False
        b._check_backstop()
        self.assertEqual(len(b.tg.sent), 1, "backstop fired twice for the same reply")

    def test_strips_the_routing_marker(self):
        b = _bridge("[TG] S markerem.", "uuid-new")
        b._check_backstop()
        self.assertEqual(b.tg.sent[0][1], "S markerem.")


if __name__ == "__main__":
    unittest.main()
