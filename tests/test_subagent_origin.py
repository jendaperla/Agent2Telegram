"""A background job a Telegram turn dispatched must report back to Telegram.

Measured on a live bridge: a run of texts was dropped, every one of them written in a turn that
a ``<task-notification>`` had woken. The bridge classified those turns as terminal-originated, so
the text was not forwarded and the turn-end backstop — which guards only Telegram turns — never
ran either. From the owner's side the agent simply went quiet while it reported progress into the
tmux panel.

The fix decides ownership per JOB: the ``tool_use`` id of the dispatch is remembered while the
turn is Telegram-originated, and the ``<tool-use-id>`` inside the notification matches it back.
Verified on live transcripts: every notification checked carried an id equal to a ``tool_use.id``
in the same file.

Each test names the mutation that makes it red (rodný list červenosti).
"""
import tempfile
import threading
import unittest

from agent2telegram.attach import AttachBridge, OWED_JOBS_MAX
from agent2telegram.readers import ClaudeCodeReader, Ev

try:                                  # `python3 -m unittest discover -s tests` (README) loads
    from test_late_answer import _bridge          # these as top-level modules…
except ImportError:                               # …while `-m unittest tests.…` loads a package.
    from .test_late_answer import _bridge


def _notification(tool_use_id: str = "toolu_01dispatch", task_id: str = "af14b3a31feca80fb") -> str:
    """Shaped exactly like the live record (Claude Code 2.1.267)."""
    return (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
            f"<tool-use-id>{tool_use_id}</tool-use-id>\n<status>completed</status>\n"
            "</task-notification>")


def _owning_bridge(tmpdir, *, dispatch_id="toolu_01dispatch", stop_reason="end_turn"):
    """A bridge that has just finished a Telegram turn which dispatched one background job."""
    b = _bridge(tmpdir)
    b._origins = ("[TG]",)              # the stub predates the origin tuple
    b._last_stop_reason = ""            # _owed_jobs is created lazily, exactly as in production
    b._turn_active.set()
    b._turn_from_tg = True
    b._handle_event(Ev("tool", text="🤖 index the sources", key=dispatch_id,
                       stop_reason="tool_use"))
    b._handle_event(Ev("text", text="Spouštím subagenta.", key="k-intro",
                       stop_reason=stop_reason))
    b._turn_active.clear()              # the Stop hook ends the turn while the job still runs
    return b


class TheReaderCarriesWhatTheBridgeNeeds(unittest.TestCase):
    """Mutation: `yield Ev("meta")` without the text → the bridge cannot tell a task-notification
    from a compaction summary, and ownership can never be matched."""

    def test_meta_event_carries_the_notification_text(self):
        rec = {"type": "user", "message": {"content": _notification()}}
        evs = list(ClaudeCodeReader().parse(rec))
        self.assertEqual([e.kind for e in evs], ["meta"])
        self.assertIn("toolu_01dispatch", evs[0].text)

    def test_assistant_events_carry_stop_reason(self):
        """Mutation: drop `stop_reason=stop` from the yields → the mid-terminal-turn guard in
        `_claims_owed_job` can never see a turn end, and no owed job is ever honoured."""
        rec = {"type": "assistant", "uuid": "u1", "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"},
                        {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {}}]}}
        evs = list(ClaudeCodeReader().parse(rec))
        self.assertEqual([e.stop_reason for e in evs], ["end_turn", "end_turn"])

    def test_stop_reason_is_empty_when_the_transcript_omits_it(self):
        rec = {"type": "assistant", "uuid": "u2",
               "message": {"content": [{"type": "text", "text": "hi"}]}}
        self.assertEqual(list(ClaudeCodeReader().parse(rec))[0].stop_reason, "")


class AnOwedJobKeepsTheTelegramOrigin(unittest.TestCase):
    """Mutation: remove the `_claims_owed_job` call from the `meta` branch → the woken turn is
    classified as terminal-originated and every text of it is dropped (the live loss)."""

    def test_the_report_after_the_notification_reaches_telegram(self):
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification()}}):
                b._handle_event(ev)
            b._handle_event(Ev("text", text="Hotovo: 12 zdrojů zaindexováno.", key="k-report",
                               stop_reason="end_turn"))
            self.assertEqual([t for _, t in b.tg.sent],
                             ["Spouštím subagenta.", "Hotovo: 12 zdrojů zaindexováno."])

    def test_every_progress_note_of_the_woken_turn_is_forwarded_not_only_the_first(self):
        """The lost texts were mostly running commentary, not one final report — so the fix has
        to survive past the first message of the woken turn."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification()}}):
                b._handle_event(ev)
            for i, note in enumerate(["first pass done", "second pass done",
                                      "lint is clean", "all four passes done"]):
                b._handle_event(Ev("text", text=note, key=f"k{i}", stop_reason="end_turn"))
            self.assertEqual([t for _, t in b.tg.sent][1:],
                             ["first pass done", "second pass done",
                              "lint is clean", "all four passes done"])

    def test_a_chain_of_jobs_keeps_flowing(self):
        """A job dispatched inside the woken turn is owed to Telegram as well, so the chain does
        not break after the first link."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification()}}):
                b._handle_event(ev)
            b._handle_event(Ev("tool", text="🤖 second pass", key="toolu_02second",
                               stop_reason="tool_use"))
            b._handle_event(Ev("text", text="Pouštím druhý běh.", key="k-2nd",
                               stop_reason="end_turn"))
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification("toolu_02second")}}):
                b._handle_event(ev)
            b._handle_event(Ev("text", text="Druhý běh hotový.", key="k-2nd-done",
                               stop_reason="end_turn"))
            self.assertIn("Druhý běh hotový.", [t for _, t in b.tg.sent])

    def test_the_id_is_not_consumed_so_a_repeated_notification_still_counts(self):
        """The harness announces the same job again if someone resumes it. A consumed id would
        silently restore the drop. Mutation: `self._owed_jobs.remove(...)` on a match → red."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            notif = {"type": "user", "message": {"content": _notification()}}
            for ev in ClaudeCodeReader().parse(notif):
                b._handle_event(ev)
            b._turn_from_tg = False                    # the woken turn ended; origin reset
            for ev in ClaudeCodeReader().parse(notif):  # …and the job reports a second time
                b._handle_event(ev)
            b._handle_event(Ev("text", text="Po resume hotovo.", key="k-again",
                               stop_reason="end_turn"))
            self.assertIn("Po resume hotovo.", [t for _, t in b.tg.sent])

    def test_a_stray_harness_note_between_the_turn_and_the_notification_cannot_kill_it(self):
        """The whole bug class is "a stray record silently reclassified the turn" (23. 8., 2. 9.).
        So the rescue must SET the origin, not merely decline to clear it.

        Mutation: replace `self._turn_from_tg = True` with a bare `return` → red."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            b._handle_event(Ev("meta", text="<system-reminder>disk is filling up</system-reminder>"))
            self.assertFalse(b._turn_from_tg, "precondition: the stray note cleared the origin")
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification()}}):
                b._handle_event(ev)
            b._handle_event(Ev("text", text="Hotovo i po systémové poznámce.", key="k-late",
                               stop_reason="end_turn"))
            self.assertIn("Hotovo i po systémové poznámce.", [t for _, t in b.tg.sent])


class WhatMustStillStayLocal(unittest.TestCase):
    """The fix must not become a new leak. Each of these keeps today's behaviour."""

    def test_a_job_nobody_dispatched_from_telegram_stays_local(self):
        """A night cycle from a timer owes nothing — its notification clears the origin."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification("toolu_99stranger")}}):
                b._handle_event(ev)
            b._handle_event(Ev("text", text="Noční cyklus hotov.", key="k-night",
                               stop_reason="end_turn"))
            self.assertNotIn("Noční cyklus hotov.", [t for _, t in b.tg.sent])

    def test_a_notification_mid_terminal_turn_does_not_hijack_it(self):
        """A terminal turn never sets `_turn_active`, so that flag cannot tell "between turns"
        from "inside a private turn". `stop_reason` can. Mutation: drop the `end_turn` check in
        `_claims_owed_job` → the rest of the terminal turn is pushed to Telegram."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            # The owner now types in the TERMINAL. The user branch clears the origin, and the
            # terminal turn is under way — a terminal turn never sets `_turn_active`.
            b._handle_event(Ev("user", text="ukaz mi diff"))
            self.assertFalse(b._turn_from_tg)
            b._handle_event(Ev("text", text="Otevírám diff…", key="k-open",
                               stop_reason="tool_use"))   # the turn has NOT ended
            for ev in ClaudeCodeReader().parse(
                    {"type": "user", "message": {"content": _notification()}}):
                b._handle_event(ev)
            self.assertFalse(b._turn_from_tg,
                             "an owed job hijacked a terminal turn that was still running")
            b._handle_event(Ev("text", text="Soukromá poznámka z terminálu.", key="k-priv",
                               stop_reason="end_turn"))
            self.assertNotIn("Soukromá poznámka z terminálu.", [t for _, t in b.tg.sent])

    def test_a_compaction_summary_still_clears_the_origin(self):
        """Only a task-notification may claim ownership. Mutation: drop the prefix check → any
        harness record would keep the origin alive."""
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            b._handle_event(Ev("meta", text="This session is being continued from a previous "
                                            "conversation <tool-use-id>toolu_01dispatch</tool-use-id>"))
            self.assertFalse(b._turn_from_tg)

    def test_a_tool_call_in_a_terminal_turn_is_never_owed(self):
        """`_handle_event` reaches the tool branch only inside a Telegram turn. If that ever
        changes, a terminal dispatch must still not be recorded."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._last_stop_reason = ""
            b._turn_from_tg = False
            b._handle_event(Ev("tool", text="🤖 local work", key="toolu_local",
                               stop_reason="tool_use"))
            self.assertNotIn("toolu_local", getattr(b, "_owed_jobs", ()))


class TheOwedSetIsBounded(unittest.TestCase):
    """Ids are kept, never consumed — so the only thing standing between this and an unbounded
    set is the deque's maxlen. Mutation: `deque()` without maxlen → red."""

    def test_it_forgets_the_oldest_instead_of_growing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _owning_bridge(d)
            b._turn_from_tg = True
            for i in range(OWED_JOBS_MAX + 50):
                b._handle_event(Ev("tool", text="🛠️", key=f"toolu_{i:05d}",
                                   stop_reason="tool_use"))
            self.assertEqual(b._owed_jobs.maxlen, OWED_JOBS_MAX)
            self.assertEqual(len(b._owed_jobs), OWED_JOBS_MAX)
            self.assertNotIn("toolu_00000", b._owed_jobs)
            self.assertIn(f"toolu_{OWED_JOBS_MAX + 49:05d}", b._owed_jobs)


class TheHelperIsDefensive(unittest.TestCase):
    """Focused tests build the bridge without __init__ (see tests/test_late_answer.py). The
    helper must not explode on an object that predates these fields."""

    def test_missing_fields_mean_no_claim_rather_than_an_attribute_error(self):
        b = object.__new__(AttachBridge)
        b._turn_active = threading.Event()
        self.assertFalse(AttachBridge._claims_owed_job(b, _notification()))


if __name__ == "__main__":
    unittest.main()
