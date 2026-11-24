"""A turn that a message from ANOTHER session woke is local, not a Telegram turn.

Live incident (inari #498, 2026-11-17): the agent answered a Telegram message at 21:27 and the
turn ended. At 21:41 a teammate (`cyklus-2`) reported back. The agent wrote a digest WITHOUT the
`[TG]` marker because the owner sat in a meeting — and the bridge forwarded it to Telegram anyway.

Cause, read off the live transcript: Claude Code files the teammate message as a ``user`` record
with ``isMeta: true`` and ``turnOrigin: "peer"``. The reader dropped every ``isMeta`` record
without an event, so nothing told the bridge that a new turn had begun, and `_turn_from_tg` stayed
True from the Telegram turn 14 minutes earlier.

The fix: an ``isMeta`` record that OPENS a turn on someone else's behalf (``turnOrigin`` other than
``human``) is reported as ``meta``. Between turns ``meta`` clears the origin; mid-turn it does not.

Each test names the mutation that makes it red.
"""
import tempfile
import unittest

from agent2telegram import attach as attach_mod
from agent2telegram.readers import ClaudeCodeReader

try:                                  # `python3 -m unittest discover -s tests` loads top-level…
    from test_late_answer import _bridge
except ImportError:                   # …while `-m unittest tests.…` loads a package.
    from .test_late_answer import _bridge


def _peer_record(body="Cyklus 21:15 (večer) je hotový. Nic nehoří."):
    """Shaped like the live record of 2026-11-17 20:41:19Z (fields the reader may look at)."""
    text = ("Another Claude session sent a message:\n"
            f'<agent-message from="cyklus-2">\n{body}\n</agent-message>')
    return {"type": "user", "isMeta": True, "promptSource": "system", "turnOrigin": "peer",
            "origin": {"kind": "peer", "from": "cyklus-2", "name": "cyklus-2", "body": body},
            "turnPosition": {"promptIndex": 8, "turnIndex": 15},
            "message": {"role": "user", "content": text}}


def _companion_reminder():
    """The isMeta system reminder written in the same millisecond, just before the peer record."""
    return {"type": "user", "isMeta": True, "turnCompanion": True,
            "message": {"role": "user", "content":
                        "<system-reminder>\n<tools-used-before-summary>Grep"
                        "</tools-used-before-summary>\n</system-reminder>"}}


def _assistant(uuid, text, stop="end_turn"):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "stop_reason": stop,
                        "content": [{"type": "text", "text": text}]}}


def _feed(bridge, *records):
    reader = ClaudeCodeReader()
    for rec in records:
        for ev in reader.parse(rec):
            bridge._handle_event(ev)


def _after_a_telegram_turn(tmpdir):
    """A bridge whose Telegram turn has just ended: the flag is still True, the turn inactive."""
    b = _bridge(tmpdir)
    b._origins = ("[TG]",)
    b._last_stop_reason = ""
    b._turn_active.set()
    b._turn_from_tg = True
    _feed(b, _assistant("u-tg", "[TG] Hotovo: úkol je v Notionu."))
    b._turn_active.clear()
    b.tg.sent.clear()
    return b


class TheReaderReportsAPeerTurn(unittest.TestCase):
    """Mutation: drop the `_opens_foreign_turn` branch in `parse` → the record yields nothing."""

    def test_peer_record_yields_a_meta_event_with_its_text(self):
        evs = list(ClaudeCodeReader().parse(_peer_record()))
        self.assertEqual([e.kind for e in evs], ["meta"])
        self.assertIn("cyklus-2", evs[0].text)

    def test_scheduled_record_yields_a_meta_event(self):
        rec = {"type": "user", "isMeta": True, "promptSource": "system",
               "turnOrigin": "scheduled",
               "message": {"content": "pokračuj v ranním běhu: zpracuj výstup"}}
        self.assertEqual([e.kind for e in ClaudeCodeReader().parse(rec)], ["meta"])

    def test_meta_record_without_a_turn_origin_still_yields_nothing(self):
        """Mutation: report EVERY isMeta record → the companion reminder becomes an event too,
        and harness notes between turns would clear the origin far more often (the 23. 8. and
        2. 9. losses came from exactly that)."""
        self.assertEqual(list(ClaudeCodeReader().parse(_companion_reminder())), [])

    def test_human_turn_origin_on_a_meta_record_yields_nothing(self):
        """Mutation: treat ``turnOrigin: human`` as foreign → an isMeta note of the owner's own
        turn would clear the origin."""
        rec = {"type": "user", "isMeta": True, "turnOrigin": "human",
               "message": {"content": "Base directory for this skill: /home/agents/x"}}
        self.assertEqual(list(ClaudeCodeReader().parse(rec)), [])

    def test_auto_continuation_yields_nothing(self):
        """``auto_continuation`` resumes the turn a usage limit interrupted — possibly a Telegram
        turn. Mutation: key on "turnOrigin is not human" → the resumed Telegram turn loses its
        origin and its answer stays in the terminal."""
        rec = {"type": "user", "isMeta": True, "turnOrigin": "auto_continuation",
               "message": {"content": "Your claude.ai usage limit has reset. Continue"}}
        self.assertEqual(list(ClaudeCodeReader().parse(rec)), [])
        self.assertIsNone(ClaudeCodeReader().user_text(rec))

    def test_peer_record_counts_as_a_prompt_for_origin_recovery(self):
        """`user_text` drives origin recovery after a restart and the background `working` flag.
        Mutation: leave `user_text` returning None → after a restart the bridge recovers the
        Telegram origin from the older [TG] prompt and forwards the peer turn's text."""
        t = ClaudeCodeReader().user_text(_peer_record())
        self.assertTrue(t and not t.lstrip().startswith("[TG]"))
        self.assertIsNone(ClaudeCodeReader().user_text(_companion_reminder()))


class APeerTurnStaysLocal(unittest.TestCase):

    def test_unmarked_reply_to_a_teammate_is_not_forwarded(self):
        """The incident itself. Mutation: drop the fix in the reader → the digest is sent."""
        with tempfile.TemporaryDirectory() as d:
            b = _after_a_telegram_turn(d)
            with self.assertLogs(attach_mod.log, level="INFO") as cm:
                _feed(b, _companion_reminder(), _peer_record(),
                      _assistant("u-digest", "Večerní cyklus je hotový, Jenda je na schůzce."))
            self.assertEqual(b.tg.sent, [])
            self.assertFalse(b._turn_from_tg)
            self.assertTrue(any("NOT forwarded" in m for m in cm.output))

    def test_marked_reply_to_a_teammate_is_still_forwarded(self):
        """The agent keeps the way to reach the owner on purpose: `[TG]` on the first line."""
        with tempfile.TemporaryDirectory() as d:
            b = _after_a_telegram_turn(d)
            _feed(b, _peer_record(), _assistant("u-urgent", "[TG] Hoří produkce."))
            self.assertEqual(b.tg.sent, [(7, "Hoří produkce.")])

    def test_a_peer_record_mid_telegram_turn_does_not_mute_the_turn(self):
        """Mutation: let `meta` clear the flag while a turn is active → the rest of the
        Telegram turn (final answer included) is silently dropped, the 21. 8. loss."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._origins = ("[TG]",)
            b._last_stop_reason = ""
            b._turn_active.set()
            b._turn_from_tg = True
            _feed(b, _peer_record(), _assistant("u-final", "Odpověď pro Telegram."))
            self.assertTrue(b._turn_from_tg)
            self.assertEqual(b.tg.sent, [(7, "Odpověď pro Telegram.")])


if __name__ == "__main__":
    unittest.main()
