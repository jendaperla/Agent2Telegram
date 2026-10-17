"""Operational logs must not become a second message transcript (no network)."""
import hashlib
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent2telegram import attach, bridge
from agent2telegram.readers import Ev
from agent2telegram.redaction import update_metadata
from agent2telegram.session import SessionError
from tests.test_v2_durability import _bridge, _msg
from tests.test_attach_backstop import _bridge as backstop_bridge


SAMPLES = [
    "Aq7vF2xMp9Jc4Zs6" * 3 + "#SyntheticOAuthState",
    "123456789:AAExampleTokenForTestsOnlyNeverUseThisValue",
    "Private medical appointment details never belong in operational logs.",
]


def assert_private(caplog, text):
    # Including fragments catches truncation-before-redaction, not only full-string leaks.
    assert text[:12] not in caplog.text
    assert text[12:24] not in caplog.text
    assert "len=" in caplog.text
    assert "sha256=" in caplog.text


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("route", ["in", "caption", "quote", "legacy", "durable", "retry",
                                  "not_forwarded", "inject_notice", "backstop", "voice"])
def test_message_routes_log_metadata_only(tmp_path, monkeypatch, caplog, text, route):
    caplog.set_level(logging.INFO)
    b = _bridge(tmp_path)
    if route in {"in", "caption", "quote"}:
        upd = _msg(99, text)
        if route == "caption":
            upd["message"].pop("text")
            upd["message"].update(caption=text, document={"file_id": "fixture"})
        if route == "quote":
            upd["message"].update(text="safe reply", reply_to_message={"message_id": 88, "text": text})
        b._submit_inbound_update = Mock()
        assert b._handle_update_once(upd, 0) == 100
        b._submit_inbound_update.assert_called_once()
        assert "update=99" in caplog.text
        if route == "quote":
            assert "reply_to_id=88" in caplog.text
    elif route in {"legacy", "durable"}:
        b._use_durable_outbox = route == "durable"
        b._send_final(text, key="fixture-reply")
        b._flush_pending()
        assert b.tg.sent == [text]
    elif route == "retry":
        b._pending_send = [{"text": text, "key": "fixture-reply"}]
        b._flush_pending()
        assert b.tg.sent == [text]
    elif route == "not_forwarded":
        b._handle_event(Ev("text", text=text, key="fixture-reply"))
        assert not b.tg.sent
    elif route == "inject_notice":
        b._notify_inject_failed(text, "fixture failure")
        assert text in b.tg.sent[0]  # Owner's notice is allowed to retain their own message.
    elif route == "backstop":
        b = backstop_bridge(tmp_path)
        b._last_assistant_text = lambda: text
        b._finish_turn()
        assert b.tg.sent == [(7, text)]
    elif route == "voice":
        b.cfg.elevenlabs_api_key = "fixture-only"
        b.tg.send_voice = Mock()
        monkeypatch.setattr(attach.shutil, "which", lambda _: "fixture-ffmpeg")
        monkeypatch.setattr(attach.tts, "synthesize_long", lambda *a, **kw: [b"audio"])
        monkeypatch.setattr(attach.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
        monkeypatch.setattr(attach.os.path, "exists", lambda _: True)
        monkeypatch.setattr(attach.os.path, "getsize", lambda _: 100)
        monkeypatch.setattr(attach, "_ogg_duration", lambda _: 1)
        assert b._try_send_voice(text)
        b.tg.send_voice.assert_called_once()
    assert_private(caplog, text)
    if route != "quote":
        assert "sha256=" + hashlib.sha256(text.encode()).hexdigest()[:8] in caplog.text


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("unsafe_pane", [False, True])
def test_injection_exception_does_not_echo_message(tmp_path, monkeypatch, caplog, text, unsafe_pane):
    caplog.set_level(logging.INFO)
    b = _bridge(tmp_path)
    error = (SessionError("refusing to inject " + text) if unsafe_pane
             else subprocess.TimeoutExpired(["tmux", "send-keys", text], timeout=10))
    b._session.inject = Mock(side_effect=error)
    monkeypatch.setattr(attach, "INJECT_RETRY_WAIT", 0)
    assert not b._inject(text)
    assert_private(caplog, text)


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("mode", ["attach", "bridge"])
def test_malformed_update_never_dumps_payload(tmp_path, caplog, text, mode):
    caplog.set_level(logging.INFO)
    upd = _msg(99, text)
    upd["update_id"] = text
    if mode == "attach":
        b = _bridge(tmp_path)
        assert b._handle_update_once(upd, 0) == 0
    else:
        b = object.__new__(bridge.Bridge)
        b.cfg = SimpleNamespace(allowed_user_ids=[], agent="generic", poll_timeout=1)
        b._allowed = set()
        b._connect = lambda: {"username": "fixture"}
        b._install_signal_handlers = b._save_offset = b._shutdown = Mock()
        b._load_offset = lambda: 0
        b._stop = Mock()
        b._stop.is_set.side_effect = [False, True]
        b.tg = Mock()
        b.tg.get_updates.return_value = [upd]
        b.run()
    assert_private(caplog, text)


def test_durable_files_only_and_photo_variants_count_as_one_attachment(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    b = _bridge(tmp_path)
    b._use_durable_outbox = True
    b._send_one_file = Mock()
    b._pending_files = ["fixture.pdf"]
    b._send_final("", key="fixture-files")
    b._flush_pending()
    assert "kind=file" in caplog.text and "attachments=1" in caplog.text
    assert "len=0" in caplog.text
    caplog.clear()
    upd = _msg(100, "")
    upd["message"]["photo"] = [{"file_id": "small"}, {"file_id": "large"}]
    b._submit_inbound_update = Mock()
    b._handle_update_once(upd, 0)
    assert "attachments=1" in caplog.text  # Resolutions of a single photo are not separate files.


@pytest.mark.parametrize("route", ["legacy", "durable", "retry", "voice", "inject_notice"])
def test_provider_failure_cannot_echo_message_to_log(tmp_path, monkeypatch, caplog, route):
    caplog.set_level(logging.INFO)
    text = SAMPLES[2]
    b = _bridge(tmp_path)
    b.tg.send_message = Mock(side_effect=RuntimeError("provider echoed: " + text))
    if route == "inject_notice":
        b._notify_inject_failed(text, "fixture")
    elif route == "voice":
        b.cfg.elevenlabs_api_key = "fixture-only"
        monkeypatch.setattr(attach.shutil, "which", lambda _: "fixture-ffmpeg")
        monkeypatch.setattr(attach.tts, "synthesize_long", Mock(side_effect=RuntimeError(text)))
        assert not b._try_send_voice(text)
    elif route == "retry":
        b._pending_send = [{"text": text, "key": "fixture"}]
        b._flush_pending()
        assert len(b._pending_send) == 1
    else:
        b._use_durable_outbox = route == "durable"
        b._send_final(text, key="fixture")
        b._flush_pending()
    assert text[:12] not in caplog.text
    assert "RuntimeError" in caplog.text


def test_fingerprint_includes_suffix_after_old_preview_boundary(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    b = _bridge(tmp_path)
    for suffix in ("first", "second"):
        text = "x" * 80 + suffix
        b._send_final(text)
        b._flush_pending()
        assert hashlib.sha256(text.encode()).hexdigest()[:8] in caplog.text
    assert "x" * 12 not in caplog.text


@pytest.mark.parametrize("route", ["inbound_worker", "oneshot_agent"])
def test_worker_exception_never_logs_prompt(tmp_path, caplog, route):
    caplog.set_level(logging.INFO)
    text = SAMPLES[2]
    if route == "inbound_worker":
        b = _bridge(tmp_path)
        b._ensure_inbound_worker_state()
        b._inbound_queue.put((_msg(99, text), None))
        b._handle = Mock(side_effect=RuntimeError(text))
        b._stop.set()
        b._inbound_worker_loop()
    else:
        b = bridge.Bridge(attach.Config(agent="generic", token="1:fixture", workdir=str(tmp_path),
                                       command=["fixture-never-executed"]),
                          client=Mock())
        b.adapter = Mock()
        b.adapter.run.side_effect = RuntimeError(text)
        b.process(7, bridge.Task(text=text))
    assert text[:12] not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("update", [None, [], SAMPLES[2], {"message": SAMPLES[2]},
    {"message": {"message_id": SAMPLES[2], "reply_to_message": {"message_id": SAMPLES[2]}}}])
def test_malformed_fields_are_not_logged_or_allowed_to_break_diagnostics(update):
    metadata = update_metadata(update)
    assert SAMPLES[2][:12] not in metadata
    assert "sha256=" in metadata
