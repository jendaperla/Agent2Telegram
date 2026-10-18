import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config, ConfigError, save, load
from agent2telegram.notices import restart_notice


class NoticeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'AGENT2TELEGRAM_STATE':str(self.root)})
        env.start(); self.addCleanup(env.stop)

    def bridge(self, **cfg):
        return SimpleNamespace(cfg=Config(agent='codex',token='1:FIXTURE',allowed_user_ids=[7],
                               tmux_session='fixture',**cfg),tg=Mock())

    def test_disabled_by_default_and_no_state_written(self):
        b = self.bridge(); restart_notice(b,7)
        b.tg.send_message.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_one_attempt_per_start_and_persistent_flap_cooldown(self):
        with patch('agent2telegram.notices.time.time',return_value=100):
            b = self.bridge(restart_notice=True,language='cs')
            restart_notice(b, -99); restart_notice(b, -99)
            b.tg.send_message.assert_called_once_with(-99,'🔄 Most je znovu připojený.')
        marker = next(self.root.glob('last_start_notified*'))
        self.assertEqual(marker.stat().st_mode & 0o777,0o600)
        for now in (90,110,129):
            with patch('agent2telegram.notices.time.time',return_value=now):
                restarted = self.bridge(restart_notice=True)
                restart_notice(restarted,-99); restarted.tg.send_message.assert_not_called()
        with patch('agent2telegram.notices.time.time',return_value=131):
            restart_notice(b,-99)  # Same process must stay quiet even after cooldown.
            b.tg.send_message.assert_called_once()
            restarted = self.bridge(restart_notice=True,restart_notice_text='🔄 Bot is back.')
            restart_notice(restarted,-99)
            restarted.tg.send_message.assert_called_once_with(-99,'🔄 Bot is back.')

    def test_uncertain_delivery_is_not_retried_by_restart(self):
        with patch('agent2telegram.notices.time.time',return_value=100):
            b = self.bridge(restart_notice=True)
            b.tg.send_message.side_effect=OSError('SECRET-provider-url')
            with self.assertLogs('agent2telegram.notices',level='WARNING') as logs:
                restart_notice(b,7)
            self.assertNotIn('SECRET',str(logs.output))
            other = self.bridge(restart_notice=True); restart_notice(other,7)
            other.tg.send_message.assert_not_called()

    def test_corrupt_or_symlinked_marker_never_causes_spam_or_overwrite(self):
        b = self.bridge(restart_notice=True); restart_notice(b,7)
        marker = next(self.root.glob('last_start_notified*'))
        marker.write_text('broken')
        other = self.bridge(restart_notice=True); restart_notice(other,7)
        other.tg.send_message.assert_not_called()
        marker.unlink(); outside=self.root/'keep'; outside.write_text('KEEP')
        marker.symlink_to(outside)
        other=self.bridge(restart_notice=True); restart_notice(other,7)
        other.tg.send_message.assert_not_called(); self.assertEqual(outside.read_text(),'KEEP')

    def test_startup_hook_only_after_live_agent_check(self):
        for alive, pane in ((False,False),(True,False),(True,True)):
            b=object.__new__(AttachBridge)
            b.cfg=self.bridge(restart_notice=True).cfg
            b.tg=Mock(); b._owner_chat=-99; b._session=Mock(alive=alive)
            b._session._pane_ok.return_value=(pane,'fixture')
            b._transcript=None; b._cleanup_orphan_status=Mock(); b._inbound_loop=Mock()
            with patch('agent2telegram.attach.threading.Thread'), patch('agent2telegram.attach.restart_notice') as send:
                if not alive:
                    with self.assertRaises(RuntimeError): b._run_locked()
                else: b._run_locked()
                self.assertEqual(send.call_count,int(alive and pane))

    def test_busy_notice_respects_language_and_keeps_authorization(self):
        for language, expected in (('en','Got your message'),('cs','Zprávu mám')):
            b=object.__new__(AttachBridge)
            b.cfg=self.bridge(language=language).cfg; b._allowed={7}; b.tg=Mock()
            b._turn_active=threading.Event(); b._turn_active.set()
            bad={'message':{'from':{'id':8},'chat':{'id':7}}}
            b._maybe_ack_queued(bad); b.tg.send_message.assert_not_called()
            b._maybe_ack_queued({'message':{'from':{'id':7},'chat':{'id':7}}})
            self.assertIn(expected,b.tg.send_message.call_args.args[1])
            b._maybe_ack_queued({'message':{'from':{'id':7},'chat':{'id':7}}})
            b.tg.send_message.assert_called_once()

    def test_configuration_roundtrip_and_validation(self):
        cfg=self.bridge(restart_notice=True,language='cs',restart_notice_text='🔄 Back.').cfg
        path=self.root/'config.json'; save(cfg,path); loaded=load(path)
        self.assertTrue(loaded.restart_notice); self.assertEqual(loaded.language,'cs')
        for fields in ({'restart_notice':'true'},{'language':'xx'},{'restart_notice_text':'x\ny'},
                       {'restart_notice_text':'a'*201}):
            with self.assertRaises(ConfigError): self.bridge(**fields).cfg.validate()


if __name__ == '__main__': unittest.main()
