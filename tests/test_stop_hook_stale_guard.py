"""A stale session guard must not leave a Claude turn open forever."""
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent2telegram import config, stop_hook


class StaleGuard(unittest.TestCase):
    def _run(self, session):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mine = {'agent': 'claude-code', 'mode': 'attach', 'tmux_session': 'Helper - Claude',
                    'claude_session_id': 'aaaa', 'signal_file': str(root/'mine'/'answer.txt')}
            other = {'agent': 'claude-code', 'mode': 'attach', 'tmux_session': 'Other - Claude',
                     'claude_session_id': 'bbbb', 'signal_file': str(root/'other'/'answer.txt')}
            (root/'helper.json').write_text(json.dumps(mine))
            (root/'other.json').write_text(json.dumps(other))
            payload = json.dumps({'transcript_path': '/x/cccc-new-conversation.jsonl'})
            with patch.object(config, 'config_path', return_value=root/'helper.json'), \
                 patch.object(stop_hook, '_own_tmux_session', return_value=session), \
                 patch('sys.stdin', io.StringIO(payload)):
                stop_hook.main()
            return (root/'mine'/'turn_end').exists(), (root/'other'/'turn_end').exists()

    def test_new_conversation_in_the_bridged_tmux_session_ends_its_turn(self):
        self.assertEqual(self._run('Helper - Claude'), (True, False))

    def test_unknown_session_marks_nothing(self):
        self.assertEqual(self._run('Somebody else'), (False, False))

    def test_pane_ancestry_finds_the_tmux_session(self):
        from subprocess import CompletedProcess
        listing = CompletedProcess([], 0, '100\tHelper - Claude\n200\tOther\n', '')
        chain = {50: 70, 70: 100}
        with patch.object(stop_hook.subprocess, 'run', return_value=listing), \
             patch.object(stop_hook.os, 'getppid', return_value=50), \
             patch.object(stop_hook, '_parent', side_effect=lambda pid: chain.get(pid, 1)):
            self.assertEqual(stop_hook._own_tmux_session(), 'Helper - Claude')


if __name__ == '__main__':
    unittest.main()
