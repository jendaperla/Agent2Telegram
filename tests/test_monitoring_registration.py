import io
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent2telegram import wizard


class MonitoringRegistration(unittest.TestCase):
    def test_started_bridge_registers_exact_paths_without_credentials(self):
        from agent2telegram import monitoring
        env = {'PYTHONPATH':'/source path','AGENT2TELEGRAM_STATE':'/tmp/custom-state',
               'TELEGRAM_BOT_TOKEN':'fixture-private-value'}
        with patch.object(monitoring, 'command', return_value=['/bin/agentsmon']), \
             patch.object(monitoring.subprocess, 'run', return_value=CompletedProcess([],0,'','')) as run, \
             redirect_stdout(io.StringIO()):
            monitoring.register_bridge(Path('/tmp/first.json'), Path('/tmp/state'), env)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ['/bin/agentsmon','add','--non-interactive'])
        self.assertEqual(argv[argv.index('--bridge-config')+1], '/tmp/first.json')
        self.assertEqual(argv[argv.index('--bridge-state')+1], '/tmp/custom-state')
        self.assertEqual(argv[argv.index('--bridge-pythonpath')+1], '/source path')
        self.assertNotIn('fixture-private-value', str(argv))
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_missing_monitor_is_one_info_sentence_and_no_failure(self):
        from agent2telegram import monitoring
        out = io.StringIO()
        with patch.object(monitoring,'command',return_value=None), \
             patch.object(monitoring.subprocess,'run') as run, redirect_stdout(out):
            monitoring.register_bridge(Path('/tmp/a.json'), Path('/tmp/state'), {})
        run.assert_not_called()
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertIn('optional', out.getvalue())

    def test_failure_is_visible_but_subprocess_output_is_never_echoed(self):
        from agent2telegram import monitoring
        out = io.StringIO()
        with patch.object(monitoring,'command',return_value=['agentsmon']), \
             patch.object(monitoring.subprocess,'run',return_value=CompletedProcess([],2,'private-output','secret-error')), \
             redirect_stdout(out):
            monitoring.register_bridge(Path('/tmp/a.json'),Path('/tmp/state'),{})
        self.assertIn('not registered',out.getvalue())
        self.assertNotIn('private-output',out.getvalue())
        self.assertNotIn('secret-error',out.getvalue())

    def test_start_failure_never_registers_and_success_uses_shared_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock(poll=lambda:2)
            with patch.object(wizard.subprocess,'Popen',return_value=process), \
                 patch.object(wizard.time,'sleep'), \
                 patch('agent2telegram.monitoring.register_bridge') as register, \
                 redirect_stdout(io.StringIO()):
                self.assertFalse(wizard._start_bridge(root/'config.json',root))
                register.assert_not_called()
                process.poll = lambda:None
                self.assertTrue(wizard._start_bridge(root/'config.json',root))
                self.assertEqual(register.call_count,1)

    def test_module_fallback_and_missing_detection(self):
        from agent2telegram import monitoring
        with patch.object(monitoring.shutil,'which',return_value=None), \
             patch.object(monitoring.importlib.util,'find_spec',return_value=object()):
            self.assertEqual(monitoring.command(),[monitoring.sys.executable,'-m','agentsmon'])
        with patch.object(monitoring.shutil,'which',return_value=None), \
             patch.object(monitoring.importlib.util,'find_spec',return_value=None):
            self.assertIsNone(monitoring.command())

    def test_provider_menu_has_no_implicit_selection_and_finds_common_bin(self):
        with patch.object(wizard,'_find_binary',side_effect=lambda x:'/fixture/codex' if x=='codex' else None), \
             patch('builtins.input',side_effect=['','99','1','2']), redirect_stdout(io.StringIO()) as out:
            chosen = wizard._choose_provider()
        self.assertEqual(chosen.name,'codex')
        self.assertIn('1) Claude Code',out.getvalue())
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path,'home',return_value=Path(tmp)), \
             patch.object(wizard.shutil,'which',return_value=None):
            exe = Path(tmp)/'.local/bin/claude'
            exe.parent.mkdir(parents=True)
            exe.write_text('#!/bin/sh\nexit 0\n')
            exe.chmod(0o755)
            self.assertEqual(wizard._find_binary('claude'),str(exe))

    def test_new_tui_launch_quotes_absolute_cli_and_requires_confirmation(self):
        cls = Mock(binary='claude',label='Claude Code',tui_launch=lambda:['claude','--fixture'])
        with patch.object(wizard,'_find_binary',return_value='/tmp/path space/claude'), \
             patch.object(wizard,'_yes',return_value=False), patch.object(wizard.subprocess,'run') as run:
            self.assertFalse(wizard._create_session('Example',cls))
            run.assert_not_called()
        with patch.object(wizard,'_find_binary',return_value='/tmp/path space/claude'), \
             patch.object(wizard,'_yes',return_value=True), patch.object(wizard.subprocess,'run') as run, \
             redirect_stdout(io.StringIO()):
            self.assertTrue(wizard._create_session('Example',cls))
        self.assertIn("'/tmp/path space/claude' --fixture",run.call_args.args[0])
