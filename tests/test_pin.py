"""Tests for transcript pinning and synthetic-record handling in the attach bridge.

Regression cover for the 21. 8. 2026 loss: Claude Code ≥ 2.1.232 writes sibling transcripts
(background-agent sessions) into the same project dir, the "newest .jsonl" heuristic switched
away mid-turn and the turn's final answer was never forwarded. Two independent kill channels
are covered: (a) the mtime switch itself, (b) a `<task-notification>` user record flipping the
turn's origin and muting the rest of the turn."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram import readers
from agent2telegram.attach import AttachBridge
from agent2telegram.prompt_hook import PIN_NAME


class _FakeTG:
    def __init__(self):
        self.sent = []

    def send_message(self, chat, text):
        self.sent.append(text)

    def send_chat_action(self, *a, **k):
        pass

    def delete_message(self, *a, **k):
        pass


def _rec_user(text):
    return {"type": "user", "message": {"content": text}}


def _rec_asst(text, uuid):
    return {"type": "assistant", "uuid": uuid,
            "message": {"content": [{"type": "text", "text": text}]}}


def _append(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    os.utime(path)


class _Env:
    """A bare bridge over a temp HOME with a real project dir — no tmux, no network."""

    def __init__(self, session_cwd=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir(parents=True)
        self.proj = self.home / ".claude" / "projects" / str(self.work.resolve()).replace("/", "-")
        self.proj.mkdir(parents=True)
        self.state = self.home / "state"
        self.state.mkdir(parents=True)
        self._home_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._home_patch.start()

        b = object.__new__(AttachBridge)

        class _Cfg:
            pass

        b.cfg = _Cfg()
        b.cfg.agent = "claude-code"
        b.cfg.transcript_path = "auto"
        b.cfg.tmux_session = "t"
        b.cfg.session_cwd = str(self.work) if session_cwd is None else session_cwd
        b.cfg.file_marker = ""          # upstream _send_final reads it; "" skips extraction
        b.cfg.voice_replies = False
        b._reader = readers.ClaudeCodeReader()
        b.tg = _FakeTG()
        b._owner_chat = 42
        b._marker = "[TG]"
        b._origins = ("[TG]", "Telegram:")
        b._sent_keys = set()
        b._mark_sent = lambda k: b._sent_keys.add(k)
        b._pending_send = []
        b._enqueue = lambda t, k: None
        b._turn_from_tg = False
        b._turn_text_sent = False
        b._turn_active = threading.Event()
        b._turn_seq = 0
        b._last_activity = 0.0
        b._last_resolve = 0.0
        b._status = {"mid": None, "shown": ""}
        b._seen_tools = set()
        b._status_clear = lambda: None
        b._session_cwd = lambda: str(self.work)
        b._pin_path = self.state / PIN_NAME
        b._pinned = None
        b._pin_stamp = 0.0
        b._pin_accepted_seq = -1
        b._hold_logged_seq = -1
        b._transcript = None
        b._tpos = 0
        self.bridge = b

    def close(self):
        self._home_patch.stop()
        self.tmp.cleanup()

    def transcript(self, name):
        return self.proj / f"{name}.jsonl"

    def pin(self, path):
        self.bridge._pin_path.write_text(
            json.dumps({"transcript": str(path), "session_id": "s", "ts": time.time()}), "utf-8")

    def turn_starts(self):
        """What the inbound handler does when a Telegram message arrives (relevant subset)."""
        b = self.bridge
        b._turn_seq += 1
        b._turn_active.set()
        b._turn_text_sent = False

    def cycle(self):
        b = self.bridge
        b._last_resolve = 0.0           # collapse the 3 s re-resolve throttle
        b._maybe_reresolve()
        b._drain_transcript()


class PinTests(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.b = self.env.bridge

    def tearDown(self):
        self.env.close()

    def test_regression_sibling_touch_mid_turn(self):
        """The 21. 8. scenario: the answer keeps flowing even though a sibling transcript is
        touched (looks newest) mid-turn — the pin holds the bridge on the right file."""
        main = self.env.transcript("main")
        sibling = self.env.transcript("sibling")
        _append(sibling, _rec_asst("background noise", "noise-1"))
        self.env.turn_starts()
        _append(main, _rec_user("[TG] co dela /clear?"))
        self.env.pin(main)
        self.env.cycle()
        _append(main, _rec_asst("prvni cast", "u1"))
        self.env.cycle()
        _append(sibling, _rec_asst("more noise", "noise-2"))   # sibling becomes newest
        self.env.cycle()
        _append(main, _rec_asst("FINALNI ODPOVED", "u2"))
        self.env.cycle()
        self.assertIn("FINALNI ODPOVED", " ".join(self.b.tg.sent))
        self.assertNotIn("noise", " ".join(self.b.tg.sent))

    def test_task_notification_does_not_mute_turn(self):
        """Channel (b): a <task-notification> user record mid-turn must not flip the origin."""
        main = self.env.transcript("main")
        self.env.turn_starts()
        _append(main, _rec_user("[TG] otazka"))
        self.env.pin(main)
        self.env.cycle()
        _append(main, _rec_asst("pracuju", "u1"))
        _append(main, _rec_user("<task-notification>\n<task-id>x</task-id> done"))
        _append(main, _rec_asst("FINALNI ODPOVED", "u2"))
        self.env.cycle()
        self.assertIn("FINALNI ODPOVED", " ".join(self.b.tg.sent))

    def test_meta_between_turns_clears_origin(self):
        """A background result arriving BETWEEN turns must not inherit Telegram origin —
        the agent's reaction to it is not an answer to a Telegram message."""
        main = self.env.transcript("main")
        self.env.turn_starts()
        _append(main, _rec_user("[TG] otazka"))
        self.env.pin(main)
        self.env.cycle()
        _append(main, _rec_asst("odpoved", "u1"))
        self.env.cycle()
        self.b._turn_active.clear()          # turn over
        _append(main, _rec_user("<task-notification> late result"))
        _append(main, _rec_asst("unsolicited reaction", "u2"))
        self.env.cycle()
        self.assertNotIn("unsolicited reaction", " ".join(self.b.tg.sent))

    def test_pin_outside_project_dir_rejected(self):
        evil = Path(self.env.tmp.name) / "evil.jsonl"
        _append(evil, _rec_user("[TG] x"))
        _append(evil, _rec_asst("secret", "e1"))
        self.env.turn_starts()
        self.env.pin(evil)
        self.env.cycle()
        self.assertIsNone(self.b._pinned)
        self.assertEqual(self.b.tg.sent, [])

    def test_pin_traversal_rejected(self):
        """`..` inside an allowed-looking path must not escape the project dir."""
        evil = self.env.proj / ".." / ".." / ".." / "state" / "x.jsonl"
        self.env.turn_starts()
        self.env.pin(evil)
        self.env.cycle()
        self.assertIsNone(self.b._pinned)

    def test_first_pin_of_turn_wins_second_ignored(self):
        main = self.env.transcript("main")
        other = self.env.transcript("other")
        _append(main, _rec_user("[TG] otazka"))
        _append(other, _rec_user("some agent task"))
        self.env.turn_starts()
        self.env.pin(main)
        self.env.cycle()
        self.assertEqual(self.b._pinned, main)
        time.sleep(0.01)
        self.env.pin(other)                  # a later, different pin in the SAME turn
        self.env.cycle()
        self.assertEqual(self.b._pinned, main, "second pin of the turn must be ignored")

    def test_pin_accepted_while_turn_already_active(self):
        """The /clear case: inbound activates the turn BEFORE the hook can fire; the first pin
        must still be accepted or the post-/clear answer is lost — the original bug."""
        old = self.env.transcript("old")
        fresh = self.env.transcript("fresh")
        _append(old, _rec_user("[TG] yesterday"))
        self.b._transcript = old
        self.b._tpos = old.stat().st_size
        self.env.turn_starts()               # turn active before any pin exists
        _append(fresh, _rec_user("[TG] ranni zprava"))
        self.env.pin(fresh)
        self.env.cycle()
        _append(fresh, _rec_asst("ODPOVED PO CLEARU", "f1"))
        self.env.cycle()
        self.assertIn("ODPOVED PO CLEARU", " ".join(self.b.tg.sent))

    def test_pin_switch_does_not_replay_history(self):
        """Switching to an existing transcript must tail it from the current turn, not replay
        megabytes of history through the chat."""
        main = self.env.transcript("main")
        for i in range(50):
            _append(main, _rec_asst(f"old history {i}", f"old-{i}"))
        _append(main, _rec_user("[TG] nova otazka"))
        self.env.turn_starts()
        self.env.pin(main)
        self.env.cycle()
        _append(main, _rec_asst("NOVA ODPOVED", "n1"))
        self.env.cycle()
        joined = " ".join(self.b.tg.sent)
        self.assertIn("NOVA ODPOVED", joined)
        self.assertNotIn("old history", joined)

    def test_stale_pin_yields_back_to_heuristic(self):
        main = self.env.transcript("main")
        _append(main, _rec_user("hello"))
        self.env.pin(main)
        old = time.time() - 7200
        os.utime(self.b._pin_path, (old, old))
        self.assertFalse(self.b._drain_pin(), "a stale pin with no active turn must not latch")

    def test_subagent_transcripts_excluded_from_heuristic(self):
        sub = self.env.proj / "some-session" / "subagents"
        sub.mkdir(parents=True)
        _append(sub / "agent-xyz.jsonl", _rec_asst("subagent noise", "s1"))
        main = self.env.transcript("main")
        _append(main, _rec_user("hello"))
        os.utime(main, (time.time() - 60, time.time() - 60))   # subagent file is newer
        self.assertEqual(self.b._newest_claude(), main)


class StopHookPinTests(unittest.TestCase):
    """With a pin present, turn_end fires only for the pinned transcript — a background-agent
    session's Stop event must not end the bridge's turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        (self.state / PIN_NAME).write_text(
            json.dumps({"transcript": str(self.state / "pinned.jsonl"), "session_id": "s"}), "utf-8")
        (self.state / "pinned.jsonl").write_text("", "utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _fire(self, transcript_path):
        from agent2telegram import stop_hook
        cfg = {"signal_file": str(self.state / "answer.txt"), "claude_session_id": ""}
        with mock.patch.object(stop_hook, "_all_cfgs", return_value=[cfg]), \
             mock.patch("sys.stdin", __import__("io").StringIO(
                 json.dumps({"transcript_path": transcript_path}))):
            stop_hook.main()
        return (self.state / "turn_end").exists()

    def test_pinned_transcript_marks_turn_end(self):
        self.assertTrue(self._fire(str(self.state / "pinned.jsonl")))

    def test_other_session_does_not_mark(self):
        self.assertFalse(self._fire(str(self.state / "background-agent.jsonl")))


if __name__ == "__main__":
    unittest.main()
