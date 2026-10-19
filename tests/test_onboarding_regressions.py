"""Launcher-only installs, fully masked secrets, and provider-safe selection."""
import io
import json
import subprocess
import traceback
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, MagicMock, patch

from agent2telegram import adapters, config, service, telegram, updater, wizard


class Onboarding(unittest.TestCase):
    def test_missing_config_recommends_launcher(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(config.ConfigError) as error:
                config.load(Path(tmp)/'missing.json')
        self.assertIn("Run 'agent2telegram setup'", str(error.exception))
        with patch.object(service.sys, 'platform', 'other'), \
             patch.object(service.shutil, 'which', return_value=None), \
             patch('sys.stderr', new_callable=io.StringIO) as output, redirect_stdout(io.StringIO()):
            service.print_instructions()
        self.assertNotIn('python -m agent2telegram', output.getvalue())

    def test_update_without_config_recommends_setup(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as output:
            root = Path(tmp)
            (root/'.git').mkdir()
            with patch.object(updater, '_src', return_value=root), \
                 patch.object(updater.agent2telegram, '__file__', str(root/'agent2telegram/__init__.py')), \
                 patch.object(updater, '_running_bridges', return_value=[]), \
                 patch.object(updater, '_backfill_usernames'), \
                 patch.object(config, 'config_path', return_value=root/'missing.json'), \
                 patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'','')):
                self.assertEqual(updater.run(), 0)
            self.assertIn('agent2telegram setup', output.getvalue())
            self.assertNotIn('agent2telegram run', output.getvalue())

    def test_provider_selection_excludes_other_and_unknown_sessions(self):
        types = {cls.name:cls for cls in adapters.available()}
        for kind, other in [('codex','claude-code'), ('claude-code','codex')]:
            with self.subTest(kind=kind), patch.object(wizard, '_tmux', return_value=True), \
                 patch.object(wizard, '_list_sessions', return_value=['Wrong','Right','Unknown']), \
                 patch.object(wizard, '_detect_agent_in_session', side_effect=[types[other],types[kind],None]), \
                 patch.object(wizard, '_ask', return_value='1'), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(wizard._choose_session(types[kind]), 'Right')
                self.assertNotIn('1) Wrong', output.getvalue())

    def test_session_detection_uses_executables_not_prompt_arguments(self):
        for command, expected in [('codex "read /project/claude/docs"', 'codex'),
                                  ('/opt/bin/claude --continue', 'claude-code'),
                                  ('node /tools/claude-code/cli.js', 'claude-code'),
                                  ('node /tools/codex/bin/codex.js', 'codex'),
                                  ('bash -c "echo /bin/codex"', None)]:
            with self.subTest(command=command), patch.object(subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([],0,'42',''),
                subprocess.CompletedProcess([],0,'42 1 '+command,'')]):
                got = wizard._detect_agent_in_session('Fixture')
                self.assertEqual(got.name if got else None, expected)

    def test_http_401_has_human_hint_without_token_or_traceback_leak(self):
        token = '101:synthetic-secret'
        error = urllib.error.HTTPError('https://api.telegram.org/bot'+token+'/getMe',
                                       401, 'Unauthorized '+token, {}, None)
        client = telegram.TelegramClient(token, max_retries=0, opener=Mock())
        client._opener.open.side_effect = error
        try:
            client.get_me()
        except telegram.TelegramError as exc:
            self.assertIn('copy it again in full from @BotFather', str(exc))
            self.assertNotIn(token, traceback.format_exc())
        else:
            self.fail('401 must fail')

    def test_retry_download_upload_and_body_errors_never_expose_token(self):
        token = '101:synthetic-secret'
        for mode in ('call', 'download', 'upload', 'body'):
            with self.subTest(mode=mode):
                opener = MagicMock()
                client = telegram.TelegramClient(token, max_retries=1, opener=opener)
                if mode == 'body':
                    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
                        {'ok':False, 'description':'bad '+token}).encode()
                else:
                    opener.open.side_effect = urllib.error.URLError('https://api.telegram.org/bot'+token)
                with patch.object(client, '_backoff'), self.assertLogs(telegram.log, level='DEBUG') as logs:
                    telegram.log.debug('fixture start')
                    try:
                        if mode == 'download':
                            client.download('file')
                        elif mode == 'upload':
                            client._call_multipart('sendDocument', {}, 'document', 'x.txt', b'x')
                        else:
                            client.get_me()
                    except telegram.TelegramError as exc:
                        self.assertNotIn(token, traceback.format_exc())
                        if mode != 'body':
                            self.assertTrue(telegram.is_network_error(exc))
                    else:
                        self.fail('failure must be visible')
                self.assertNotIn(token, '\n'.join(logs.output))

    def test_short_secret_is_fully_masked_in_terminal(self):
        fake = io.StringIO('abc\n')
        fake.isatty = lambda: True
        fake.fileno = lambda: 0
        with patch.object(wizard.sys, 'stdin', fake), patch('termios.tcgetattr', return_value=[]), \
             patch('termios.tcsetattr'), patch('tty.setraw'), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wizard._ask_secret('Token'), 'abc')
        self.assertNotIn('abc', output.getvalue())
        self.assertIn('***', output.getvalue())


if __name__ == '__main__':
    unittest.main()
