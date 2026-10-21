"""set-elevenlabs must restart running bridges so the new key works at once."""
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agent2telegram import config, stt, updater, wizard


class SetElevenLabsRestart(unittest.TestCase):
    def test_running_bridges_are_restarted_through_restart_plans(self):
        cfg = SimpleNamespace(elevenlabs_api_key='', elevenlabs_language='')
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{}')
            plan = updater.RestartPlan('123', str(path), env={'PATH': '/bin'})
            with patch.object(config, 'load', return_value=cfg), \
                 patch.object(config, 'save', return_value=path), \
                 patch.object(config, 'mark_secret_from_file'), \
                 patch.object(config, 'config_path', return_value=path), \
                 patch.object(wizard, '_ask_secret', return_value='sk_' + 'a' * 40), \
                 patch.object(stt, 'looks_like_api_key', return_value=True), \
                 patch('builtins.input', return_value='cs'), \
                 patch.object(wizard, '_also_configure_hermes'), \
                 patch.object(updater, '_running_bridges', return_value=[('123', str(path))]), \
                 patch.object(updater, '_plan_restarts', return_value=[plan]) as planned, \
                 patch.object(updater, '_restart', return_value=1) as restart, \
                 redirect_stdout(io.StringIO()) as out:
                self.assertEqual(wizard.set_elevenlabs(), 0)
        planned.assert_called_once()
        self.assertEqual(restart.call_args.args[0], [plan])
        self.assertIn('Restarted 1 bridge', out.getvalue())
        self.assertNotIn('restart skipped', out.getvalue())


if __name__ == '__main__':
    unittest.main()
