"""Transcript resolution must never leave the driven session's own project directory.

Live loss on 2026-09-03 (agents@air-claw, kindle-notion-sync bot): the bot forwarded a message
written by the GRAIN bot's Claude session. Its own tmux session had been recreated at 22:03:21,
so at 22:01:28 no session by that name existed; ``tmux display-message -t <gone>`` answers with
an EMPTY stdout and exit code 0 -- no exception, so ``_session_cwd`` returned None -- and
``_newest_claude`` then scanned ALL of ``~/.claude/projects`` and picked whichever bot's
transcript happened to be newest. The config knew the answer the whole time: ``session_cwd``
was set to /home/agents/kindle-notion-sync.

Each test names the mutation that makes it red.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config


def _bridge(home: Path, *, session_cwd: str, tmux_cwd: str | None):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7],
                   tmux_session="mine", transcript_path="auto", session_cwd=session_cwd)
    b._session_cwd = lambda: tmux_cwd
    return b


def _project(home: Path, cwd: str, name: str, mtime: float) -> Path:
    d = home / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("{}\n", "utf-8")
    import os
    os.utime(p, (mtime, mtime))
    return p


class TranscriptStaysInOwnProject(unittest.TestCase):

    def test_missing_tmux_session_does_not_reach_another_bots_transcript(self):
        """The exact 2026-09-03 loss: tmux session gone, another bot's log is newer.

        Mutation: drop the ``cfg.session_cwd`` fallback from ``_newest_claude`` -> the global
        scan returns the foreign transcript and the test goes red."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            mine = _project(home, "/home/agents/kindle-notion-sync", "mine.jsonl", 1000.0)
            _project(home, "/home/agents/grain-mesh-sync", "foreign.jsonl", 9000.0)
            b = _bridge(home, session_cwd="/home/agents/kindle-notion-sync", tmux_cwd=None)
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertEqual(b._newest_claude(), mine)

    def test_tmux_cwd_still_wins_when_the_session_is_alive(self):
        """The tmux answer is the live truth and must keep priority over the stored config.

        Mutation: read cfg.session_cwd first -> a session that moved is tailed in the wrong
        place and this goes red."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _project(home, "/home/agents/stale", "stale.jsonl", 9000.0)
            live = _project(home, "/home/agents/live", "live.jsonl", 1000.0)
            b = _bridge(home, session_cwd="/home/agents/stale", tmux_cwd="/home/agents/live")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertEqual(b._newest_claude(), live)

    def test_known_cwd_with_no_project_dir_yields_nothing_rather_than_a_stranger(self):
        """A configured session that has not written a transcript yet must tail NOTHING.

        Forwarding someone else's output is worse than forwarding nothing.

        Mutation: fall back to the global scan when the project dir is missing -> red."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude" / "projects").mkdir(parents=True)
            _project(home, "/home/agents/grain-mesh-sync", "foreign.jsonl", 9000.0)
            b = _bridge(home, session_cwd="/home/agents/kindle-notion-sync", tmux_cwd=None)
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertIsNone(b._newest_claude())

    def test_no_cwd_from_either_source_keeps_the_old_global_scan(self):
        """Installs that never recorded a cwd (single bot, pre-session_cwd configs) must keep
        working exactly as before -- the narrowing must not break them.

        Mutation: return None whenever tmux is silent -> single-bot installs stop tailing."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            _project(home, "/home/agents/whatever", "only.jsonl", 1000.0)
            newest = _project(home, "/home/agents/other", "newest.jsonl", 9000.0)
            b = _bridge(home, session_cwd="", tmux_cwd=None)
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertEqual(b._newest_claude(), newest)


class ScopingCwdIsOneQuestion(unittest.TestCase):
    """Both callers (``_newest_claude`` and the mid-turn switch guard in ``_maybe_reresolve``)
    must decide "is the cwd known?" the same way. When they disagree, the guard treats a
    configured session as unscoped and blocks its own switch mid-turn — the ~90s first-message
    lag that the guard's comment was written to prevent."""

    def test_config_cwd_counts_as_known(self):
        """Mutation: make ``_scoping_cwd`` return only the tmux answer -> red."""
        b = _bridge(Path("/nonexistent"), session_cwd="/home/agents/mine", tmux_cwd=None)
        self.assertEqual(b._scoping_cwd(), "/home/agents/mine")

    def test_tmux_answer_wins_over_config(self):
        """Mutation: read the config first -> red."""
        b = _bridge(Path("/nonexistent"), session_cwd="/home/agents/stale", tmux_cwd="/home/agents/live")
        self.assertEqual(b._scoping_cwd(), "/home/agents/live")

    def test_neither_source_means_genuinely_unknown(self):
        """Mutation: return "" instead of None -> the guards stop firing and this goes red."""
        b = _bridge(Path("/nonexistent"), session_cwd="", tmux_cwd=None)
        self.assertIsNone(b._scoping_cwd())


if __name__ == "__main__":
    unittest.main()
