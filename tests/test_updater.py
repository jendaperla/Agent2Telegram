"""Offline update preflight, macOS restart routing, and legacy config repair."""
import io
import json
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from agent2telegram import updater


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(TemporaryDirectory()))
        self.src = self.home / '.agent2telegram-src'
        (self.src / '.git').mkdir(parents=True)
        self.config = self.home / 'bot.json'
        self.state = self.home / 'bot-state'
        self.config.write_text(json.dumps({'agent': 'codex', 'token': '101:fixture',
                                          'state_dir': str(self.state)}))
        self.bridges = [('123', str(self.config))]
        self.service_pid = None
        self.kickstart_rc = 0
        self.pull_rc = 0
        self.events = []
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(patch.dict(os.environ, {'HOME': str(self.home),
                                                       'PATH': os.environ['PATH']}, clear=True))
        self.mock(updater, '_src', return_value=self.src)
        self.mock(updater.agent2telegram, '__file__', str(self.src/'agent2telegram/__init__.py'))
        self.mock(updater.sys, 'platform', 'darwin')
        self.mock(updater, '_running_bridges', side_effect=self.discover)
        self.proc_env = self.mock(updater, '_proc_env', side_effect=self.environment)
        self.mock(updater.subprocess, 'run', side_effect=self.command)
        self.start = self.mock(updater.subprocess, 'Popen')
        self.start.return_value.poll.return_value = None
        self.kill = self.mock(updater.os, 'kill')
        self.mock(updater, '_wait_gone', return_value=True)
        self.mock(updater.time, 'sleep')
        self.backfill = self.mock(updater, '_backfill_usernames',
                                 side_effect=lambda: self.events.append('backfill'))

    def mock(self, obj, name, *args, **kwargs):
        return self.stack.enter_context(patch.object(obj, name, *args, **kwargs))

    def discover(self):
        self.events.append('discover')
        return list(self.bridges)

    def environment(self, pid):
        self.events.append('environment')
        return {}

    def command(self, argv, **kwargs):
        self.events.append(tuple(argv))
        if argv[0] == 'git':
            return Mock(returncode=self.pull_rc if 'pull' in argv else 0, stdout='', stderr='')
        if argv[:2] == ['launchctl', 'print']:
            return Mock(returncode=0 if self.service_pid else 113,
                        stdout=f'job = {{\n\tpid = {self.service_pid}\n}}', stderr='')
        if argv[:2] == ['launchctl', 'kickstart']:
            return Mock(returncode=self.kickstart_rc, stdout='', stderr='fixture failure')
        self.fail(f'Unexpected subprocess: {argv}')

    def pull_index(self):
        return next(i for i, event in enumerate(self.events)
                    if isinstance(event, tuple) and 'pull' in event)

    def test_preflight_inspects_process_before_pull(self):
        updater.run()
        self.assertLess(self.events.index('discover'), self.pull_index())

    def test_mac_launchd_restarts_matching_pid_with_kickstart_only(self):
        self.service_pid = '123'
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        cmd = ('launchctl', 'kickstart', '-k',
               f'gui/{os.getuid()}/com.agent2telegram.bridge')
        self.assertIn(cmd, self.events)
        self.assertLess(self.events.index('backfill'), self.events.index(cmd))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_other_launchd_pid_does_not_restart_wrong_bot(self):
        self.service_pid = '987'
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        self.assertFalse(any(isinstance(e, tuple) and 'kickstart' in e for e in self.events))
        self.start.assert_called_once()

    def test_mac_explicit_config_and_state_can_restart_without_proc(self):
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        self.assertLess(self.events.index('environment'), self.pull_index())
        self.start.assert_called_once()
        args, kwargs = self.start.call_args
        self.assertEqual(args[0][-2:], ['--config', str(self.config)])
        self.assertEqual(kwargs['env']['AGENT2TELEGRAM_STATE'], str(self.state))
        self.assertEqual(kwargs['env']['PYTHONPATH'], str(self.src))

    def test_mac_config_restart_does_not_inherit_another_bots_overrides(self):
        with patch.dict(os.environ, {'AGENT2TELEGRAM_STATE': '/wrong-state',
                                     'AGENT2TELEGRAM_CONFIG': '/wrong-config',
                                     'TELEGRAM_BOT_TOKEN': '202:other-fixture',
                                     'ELEVENLABS_API_KEY': 'OTHER-FIXTURE'}):
            self.assertEqual(updater.run(), 0, self.output.getvalue())
        env = self.start.call_args.kwargs['env']
        self.assertEqual(env['AGENT2TELEGRAM_STATE'], str(self.state))
        self.assertNotIn('TELEGRAM_BOT_TOKEN', env)
        self.assertNotIn('ELEVENLABS_API_KEY', env)
        self.assertNotIn('other-fixture', self.output.getvalue())

    def test_env_only_without_proc_updates_with_warning_and_manual_command(self):
        self.bridges = [('123', None)]
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        text = self.output.getvalue()
        self.assertIn('WARNING', text)
        self.assertIn('123', text)
        self.assertIn('AGENT2TELEGRAM_CONFIG', text)
        self.assertIn('AGENT2TELEGRAM_STATE', text)
        self.assertIn('-m agent2telegram run', text)
        self.assertNotIn('restarted 1', text)
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_legacy_config_without_state_is_not_restarted_using_default(self):
        self.config.write_text(json.dumps({'agent': 'codex', 'token': '101:fixture'}))
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        self.assertIn('WARNING', self.output.getvalue())
        self.kill.assert_not_called()

    def test_config_with_env_only_token_is_not_restarted(self):
        self.config.write_text(json.dumps({'agent': 'codex', 'state_dir': str(self.state)}))
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        self.assertIn('WARNING', self.output.getvalue())
        self.kill.assert_not_called()

    def test_discovery_failure_aborts_before_pull(self):
        self.mock(updater, '_running_bridges', side_effect=RuntimeError('fixture ps failure'))
        self.assertEqual(updater.run(), 1)
        self.assertFalse(any(isinstance(e, tuple) and 'pull' in e for e in self.events))
        self.kill.assert_not_called()

    def test_backfill_runs_after_pull_even_with_no_restart(self):
        self.assertEqual(updater.run(no_restart=True), 0)
        self.assertLess(self.pull_index(), self.events.index('backfill'))
        self.assertNotIn('discover', self.events)
        self.kill.assert_not_called()

    def test_failed_pull_never_backfills_or_restarts(self):
        self.pull_rc = 1
        self.assertEqual(updater.run(), 1)
        self.backfill.assert_not_called()
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_failed_launchd_restart_is_reported_not_silently_manual(self):
        self.service_pid = '123'
        self.kickstart_rc = 1
        self.assertEqual(updater.run(), 1)
        self.assertIn('kickstart', self.output.getvalue())
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_launchd_inspection_error_aborts_before_pull(self):
        # Only when the launchd service is installed: then an unreadable state is a real unknown.
        plist = self.home / 'Library/LaunchAgents/com.agent2telegram.bridge.plist'
        plist.parent.mkdir(parents=True)
        plist.write_text('<plist/>')
        original = self.command
        def command(argv, **kwargs):
            if argv[:2] == ['launchctl', 'print']:
                return Mock(returncode=1, stdout='', stderr='permission denied')
            return original(argv, **kwargs)
        self.mock(updater.subprocess, 'run', side_effect=command)
        self.assertEqual(updater.run(), 1)
        self.assertFalse(any(isinstance(e, tuple) and 'pull' in e for e in self.events))
        self.kill.assert_not_called()

    def test_process_replaced_during_pull_is_not_signalled(self):
        self.mock(updater, '_running_bridges', side_effect=[self.bridges, [('456', str(self.config))]])
        self.assertEqual(updater.run(), 1)
        self.assertIn('changed during update', self.output.getvalue())
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_multi_bot_mix_restarts_only_safe_plans(self):
        self.bridges += [('456', None), ('789', None)]
        self.service_pid = '456'
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        self.start.assert_called_once()
        self.assertEqual([c.args[0] for c in self.kill.call_args_list], [123])
        self.assertIn('PID 789', self.output.getvalue())
        self.assertIn('restarted 2 bridge(s)', self.output.getvalue())

    def test_linux_keeps_original_environment_config_and_state(self):
        self.mock(updater.sys, 'platform', 'linux')
        self.bridges = [('123', None)]
        env = {'HOME': str(self.home), 'AGENT2TELEGRAM_CONFIG': str(self.config),
               'AGENT2TELEGRAM_STATE': str(self.home/'original-state'),
               'PYTHONPATH': '/fixture/existing', 'TELEGRAM_BOT_TOKEN': '303:original-fixture'}
        self.proc_env.side_effect = lambda pid: dict(env)
        self.assertEqual(updater.run(), 0, self.output.getvalue())
        expected = dict(env, PYTHONPATH=str(self.src)+os.pathsep+'/fixture/existing')
        self.assertEqual(self.start.call_args.kwargs['env'], expected)
        self.assertNotIn('original-fixture', self.output.getvalue())
        self.assertFalse(any(isinstance(e, tuple) and 'launchctl' in e for e in self.events))


class BackfillTests(unittest.TestCase):
    def test_legacy_username_is_filled_without_overwriting_existing_username(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            missing, present = root/'old.json', root/'new.json'
            missing.write_text(json.dumps({'agent': 'codex', 'token': '101:fixture'}))
            present.write_text(json.dumps({'agent': 'codex', 'token': '202:fixture',
                                          'bot_username': 'existing_bot'}))
            with patch('agent2telegram.config.config_path', return_value=missing), \
                 patch('agent2telegram.telegram.TelegramClient') as client:
                client.return_value.get_me.return_value = {'username': 'fixture_bot'}
                updater._backfill_usernames()
            self.assertEqual(json.loads(missing.read_text())['bot_username'], 'fixture_bot')
            self.assertEqual(json.loads(present.read_text())['bot_username'], 'existing_bot')
            client.assert_called_once_with('101:fixture')


if __name__ == '__main__':
    unittest.main()


class LaunchdInspectionTest(unittest.TestCase):
    """An unexpected launchctl code must not block bridges that launchd cannot be running."""

    def inspect(self, home, code):
        with patch.object(updater.sys, 'platform', 'darwin'), \
             patch.object(updater.Path, 'home', return_value=Path(home)), \
             patch.object(updater.subprocess, 'run', return_value=Mock(returncode=code, stdout='')):
            return updater._launchd_bridge()

    def test_unknown_code_without_plist_restarts_directly(self):
        with TemporaryDirectory() as home:
            target, pid = self.inspect(home, 125)
        self.assertTrue(target.endswith('com.agent2telegram.bridge'))
        self.assertIsNone(pid)

    def test_unknown_code_with_plist_still_refuses(self):
        with TemporaryDirectory() as home:
            plist = Path(home) / 'Library/LaunchAgents/com.agent2telegram.bridge.plist'
            plist.parent.mkdir(parents=True)
            plist.write_text('<plist/>')
            with self.assertRaises(RuntimeError):
                self.inspect(home, 125)
