"""Offline release guards: installation never destroys existing user files."""
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from agent2telegram import __version__, updater, wizard
from agent2telegram.config import Config, _state_dir

ROOT = Path(__file__).resolve().parents[1]


class InstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.src = self.home / '.agent2telegram-src'
        self.env = {**os.environ, 'HOME': str(self.home),
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'PYTHONNOUSERSITE': '1'}
        self.script('tmux', 'exit 0')
        self.script('git', 'exit 128')
        self.script('sleep', 'exit 0')
        self.script('python3', f'case "$*" in *pip*) exit 1;; esac\nexec "{sys.executable}" "$@"')
        tree = self.root / 'Agent2Telegram-fixture'
        (tree / 'agent2telegram').mkdir(parents=True)
        (tree / 'pyproject.toml').write_text('[project]\nname="agent2telegram"\n')
        (tree / 'agent2telegram/__init__.py').write_text('NEW=True\n')
        (tree / 'agent2telegram/__main__.py').write_text('print("fixture")\n')
        archive = self.root / 'archive.tgz'
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(tree, arcname=tree.name)
        self.script('curl', f'while [ "$1" != "-o" ]; do shift; done\ncp "{archive}" "$2"')

    def script(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def install(self, cwd=None):
        return subprocess.run(['bash', str(ROOT / 'install.sh'), '--no-setup', '--no-pip'],
                              cwd=cwd or self.home, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20, start_new_session=True)

    def test_failed_pull_preserves_git_checkout_and_local_files(self):
        (self.src / '.git').mkdir(parents=True)
        (self.src / 'keep.txt').write_text('LOCAL WORK')
        calls = self.root / 'pulls'
        self.script('git', f'case "$*" in *status*) exit 0;; *pull*) echo pull >> "{calls}";; esac\nexit 128')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.src / '.git').is_dir())
        self.assertEqual((self.src / 'keep.txt').read_text(), 'LOCAL WORK')
        self.assertEqual(calls.read_text().splitlines(), ['pull'] * 3)

    def test_unknown_directory_and_symlink_are_never_replaced(self):
        self.src.mkdir()
        (self.src / 'keep.txt').write_text('KEEP')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.src / 'keep.txt').read_text(), 'KEEP')
        target = self.root / 'outside'
        self.src.rename(target)
        self.src.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertTrue(self.src.is_symlink())
        self.assertEqual((target / 'keep.txt').read_text(), 'KEEP')

    def test_archive_rerun_upgrades_unmarked_legacy_install_from_inside(self):
        (self.src / 'agent2telegram').mkdir(parents=True)
        (self.src / 'pyproject.toml').write_text('[project]\nname="agent2telegram"\n')
        (self.src / 'agent2telegram/__init__.py').write_text('OLD=True\n')
        (self.src / 'local.txt').write_text('KEEP IN BACKUP')
        result = self.install(self.src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.src / 'agent2telegram/__init__.py').read_text(), 'NEW=True\n')
        backups = list(self.home.glob('.agent2telegram-src.backup.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'local.txt').read_text(), 'KEEP IN BACKUP')
        self.assertEqual(self.install(self.src).returncode, 0)


class ReleaseSafetyTests(unittest.TestCase):
    def test_process_discovery_failure_cannot_be_reported_as_no_bridges(self):
        with patch.object(updater.subprocess, 'run', side_effect=OSError('fixture')):
            with self.assertRaises(RuntimeError):
                updater._running_bridges()

    def test_uninstall_disables_supervisor_before_stopping_any_bridge(self):
        from argparse import Namespace
        from agent2telegram.__main__ import _cmd_uninstall
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            service = home / '.config/systemd/user/agent2telegram.service'
            service.parent.mkdir(parents=True)
            service.write_text('fixture')
            order = []
            def run(argv, **kwargs):
                if argv[0] == 'systemctl': order.append('disable')
                return Mock(returncode=0, stdout='')
            with patch('pathlib.Path.home', return_value=home), \
                 patch('agent2telegram.config.DEFAULT_PATH', home/'config/config.json'), \
                 patch('shutil.which', return_value='/fixture/systemctl'), \
                 patch('subprocess.run', side_effect=run), \
                 patch.object(updater, '_running_bridges', return_value=[('123', None)]), \
                 patch.object(updater, '_wait_gone', return_value=True), \
                 patch('os.kill', side_effect=lambda *args: order.append('stop')), redirect_stdout(io.StringIO()):
                self.assertEqual(_cmd_uninstall(Namespace(yes=True)), 0)
            self.assertEqual(order, ['disable', 'stop'])

    def test_recovery_between_reservation_and_submit_cannot_enqueue_twice(self):
        from tests.test_v2_durability import _bridge, _msg
        for completed in (False, True):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as tmp:
                bridge = _bridge(tmp)
                bridge._start_inbound_worker = Mock()
                bridge._ensure_inbound_worker_state()
                def replay_then_optionally_complete(update):
                    bridge._replay_pending_inbound()
                    if completed:
                        _, record = bridge._inbound_queue.get_nowait()
                        bridge._inbound_done(record)
                        bridge._inbound_queue.task_done()
                bridge._maybe_ack_queued = replay_then_optionally_complete
                bridge._handle_update_once(_msg(100, 'exactly once'), 0)
                self.assertEqual(bridge._inbound_queue.qsize(), 0 if completed else 1)

    def test_version_metadata_matches_cli(self):
        import re
        metadata = (ROOT / 'pyproject.toml').read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', metadata, re.M)[1], __version__)

    def test_eof_aborts_setup_instead_of_looping_or_creating_sessions(self):
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(EOFError):
                wizard._ask('Choose', '1')

    def test_new_config_state_is_explicit_without_migrating_old_defaults(self):
        cfg = Config(agent='codex', token='fixture:only')
        cfg.state_dir = '/tmp/fixture-bridge-b'
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_state_dir(cfg), Path('/tmp/fixture-bridge-b'))
            self.assertEqual(_state_dir(), Path.home() / '.local/state/agent2telegram')
        with patch.dict(os.environ, {'AGENT2TELEGRAM_STATE': '/tmp/fixture-override'}):
            self.assertEqual(_state_dir(cfg), Path('/tmp/fixture-override'))

    def test_failed_update_preserves_processes_and_disables_git_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.git').mkdir()
            with patch.object(updater, '_src', return_value=Path(tmp)), \
                 patch.object(updater.subprocess, 'run', return_value=Mock(returncode=1, stdout='', stderr='refused')) as run, \
                 patch.object(updater, '_restart') as restart, redirect_stdout(io.StringIO()):
                self.assertNotEqual(updater.run(), 0)
            restart.assert_not_called()
            git_calls = [call for call in run.call_args_list if call.args[0][0] == 'git']
            self.assertTrue(git_calls)
            for call in git_calls:
                self.assertEqual(call.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_process_discovery_excludes_other_users_and_shell_mentions(self):
        uid = os.getuid()
        processes = (f'{uid} 111 python3 -m agent2telegram run --config /tmp/a.json\n'
                     f'{uid + 1} 222 python3 -m agent2telegram run --config /tmp/other.json\n'
                     f'{uid} 333 sh -c "python3 -m agent2telegram run"\n')
        with patch.object(updater.subprocess, 'run', return_value=Mock(stdout=processes, returncode=0)):
            self.assertEqual(updater._running_bridges(), [('111', '/tmp/a.json')])

    def test_restart_preserves_distinct_state_and_never_inherits_another_bot(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(updater.Path, 'home', return_value=Path(tmp)), \
             patch.object(updater, '_proc_env', return_value={
                 'AGENT2TELEGRAM_STATE': '/tmp/fixture-b', 'PATH': '/usr/bin',
                 'HOME': tmp, 'AGENT2TELEGRAM_CONFIG': '/tmp/b.json'}), \
             patch.object(updater.os, 'kill'), patch.object(updater, '_wait_gone', return_value=True), \
             patch.object(updater.subprocess, 'Popen') as start:
            start.return_value.poll.return_value = None
            with patch.object(updater.time, 'sleep'):
                self.assertEqual(updater._restart([('123', '/tmp/b.json')], Path(tmp)), 1)
            self.assertEqual(start.call_args.kwargs['env']['AGENT2TELEGRAM_STATE'], '/tmp/fixture-b')

    def test_doctor_fails_with_no_authorized_user(self):
        from agent2telegram.__main__ import _cmd_doctor
        cfg = Config(agent='generic', token='fixture:only')
        with patch('agent2telegram.config.load', return_value=cfg), \
             patch('agent2telegram.telegram.TelegramClient') as client, redirect_stdout(io.StringIO()):
            client.return_value.get_me.return_value = {'username': 'fixture_bot'}
            self.assertNotEqual(_cmd_doctor(None), 0)

    def test_uninstall_never_kills_tmux_or_uses_broad_pgrep(self):
        from argparse import Namespace
        from agent2telegram.__main__ import _cmd_uninstall
        with tempfile.TemporaryDirectory() as tmp, \
             patch('pathlib.Path.home', return_value=Path(tmp)), \
             patch('agent2telegram.config.DEFAULT_PATH', Path(tmp)/'config/config.json'), \
             patch.object(updater, '_running_bridges', return_value=[]), \
             patch('subprocess.run', return_value=Mock(returncode=0, stdout='')) as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(_cmd_uninstall(Namespace(yes=True)), 0)
            for call in run.call_args_list:
                self.assertNotIn(call.args[0][0], ('tmux', 'pkill', 'pgrep'))

    def test_uninstall_reports_package_removal_failure(self):
        from argparse import Namespace
        from agent2telegram.__main__ import _cmd_uninstall
        with tempfile.TemporaryDirectory() as tmp, \
             patch('pathlib.Path.home', return_value=Path(tmp)), \
             patch('agent2telegram.config.DEFAULT_PATH', Path(tmp)/'config/config.json'), \
             patch.object(updater, '_running_bridges', return_value=[]), \
             patch('subprocess.run', return_value=Mock(returncode=1, stdout='')), \
             redirect_stdout(io.StringIO()):
            self.assertNotEqual(_cmd_uninstall(Namespace(yes=True)), 0)

    def test_uninstall_pip_runs_outside_deleted_source(self):
        from argparse import Namespace
        from agent2telegram.__main__ import _cmd_uninstall
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home/'.agent2telegram-src'
            source.mkdir()
            def run(argv, **kwargs):
                self.assertFalse(source.exists())
                self.assertEqual(kwargs.get('cwd'), home)
                return Mock(returncode=0, stdout='')
            with patch('pathlib.Path.home', return_value=home), \
                 patch('agent2telegram.config.DEFAULT_PATH', home/'config/config.json'), \
                 patch.object(updater, '_running_bridges', return_value=[]), \
                 patch('subprocess.run', side_effect=run), redirect_stdout(io.StringIO()):
                self.assertEqual(_cmd_uninstall(Namespace(yes=True)), 0)

    def test_uninstall_stdlib_only_install_does_not_require_pip(self):
        from argparse import Namespace
        from agent2telegram.__main__ import _cmd_uninstall
        with tempfile.TemporaryDirectory() as tmp, \
             patch('pathlib.Path.home', return_value=Path(tmp)), \
             patch('agent2telegram.config.DEFAULT_PATH', Path(tmp)/'config/config.json'), \
             patch.object(updater, '_running_bridges', return_value=[]), \
             patch('importlib.util.find_spec', return_value=None), \
             patch('subprocess.run', return_value=Mock(returncode=1)) as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(_cmd_uninstall(Namespace(yes=True)), 0)
            run.assert_not_called()

    def test_service_exports_source_path_and_explicit_state_without_secrets(self):
        from agent2telegram import service
        import plistlib
        with patch.dict(os.environ, {'AGENT2TELEGRAM_STATE': '/tmp/fixture-state',
                                     'TELEGRAM_BOT_TOKEN': 'SYNTHETIC-SECRET'}):
            unit = service.systemd_unit()
            data = plistlib.loads(service.launchd_plist().encode())
        self.assertIn(str(ROOT), unit)
        self.assertIn('/tmp/fixture-state', unit)
        self.assertEqual(data['EnvironmentVariables']['PYTHONPATH'], str(ROOT))
        self.assertNotIn('SYNTHETIC-SECRET', unit + str(data))


if __name__ == '__main__':
    unittest.main()
