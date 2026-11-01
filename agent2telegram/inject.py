"""``agent2telegram inject`` — deliver a text into the attached agent's tmux pane from a script
and wait until the agent actually received it.

Why: a scheduler (cron, systemd timer) that wants to wake the agent used to restart it with the
prompt as an argument. ``inject`` instead types the text into the live session the same way the
bridge delivers Telegram messages (pane check, ``send-keys`` / bracketed paste) and then waits
for proof of delivery: the Claude Code **UserPromptSubmit** hook (``prompt_hook.py``) sees the
submitted prompt and acknowledges it. Text on screen is not proof — only the hook is.

Contract (exit codes)::

    0  delivered and acknowledged by the hook
    1  the pane is not running the agent (nothing was typed)
    2  typed, but the hook did not acknowledge within --timeout
    3  tmux error (session missing, send-keys failed) or bad config

Every attempt writes one line to syslog (the systemd journal on Linux), tag ``agent2telegram-inject``.

Handshake: before typing, ``inject`` writes ``<state>/inject/<nonce>.req`` with the digest of the
normalized text; the hook writes ``<nonce>.ack`` when a submitted prompt has the same digest.
State dir = the directory of the bridge's ``signal_file``. The text is injected WITHOUT the
bridge's origin prefix: it is not a Telegram message, so it must not move the transcript pin.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

log = logging.getLogger("agent2telegram.inject")

INJECT_DIR = "inject"
EXIT_OK, EXIT_NOT_AGENT, EXIT_NO_ACK, EXIT_TMUX = 0, 1, 2, 3
#: A request older than this is stale; the hook ignores it and inject cleans it up.
REQ_TTL = 3600


def normalize(text: str) -> str:
    """The form both sides compare: send-keys joins lines with spaces and drops control bytes,
    the TUI may trim; collapse all whitespace so formatting differences don't matter."""
    return re.sub(r"\s+", " ", text or "").strip()


#: Claude Code wraps a longer bracketed paste (seen: 1213 B wrapped, 612 B not) as
#: ``<pasted_content id="X">…</pasted_content id="X">`` before the hook sees the prompt.
_PASTE_WRAPPER = re.compile(r'<pasted_content id="([^"]*)">(.*)</pasted_content id="\1">', re.DOTALL)


def unwrap_paste(text: str) -> str | None:
    """The inner text when one paste wrapper encloses the whole prompt, else None."""
    m = _PASTE_WRAPPER.fullmatch((text or "").strip())
    return m.group(2) if m else None


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def inject_dir(signal_file: str) -> Path:
    return Path(signal_file).expanduser().parent / INJECT_DIR


def acknowledge(signal_file: str, prompt: str, now: float | None = None) -> list[str]:
    """Called by the hook for every submitted prompt: write ``.ack`` for each pending request
    whose digest matches. Returns the acknowledged nonces. Never raises."""
    acked = []
    d = inject_dir(signal_file)
    if not d.is_dir():
        return acked
    # the prompt as submitted, and without the TUI's paste wrapper (the request never has it)
    inner = unwrap_paste(prompt)
    want = {digest(prompt)} | ({digest(inner)} if inner is not None else set())
    now = now or time.time()
    for req in d.glob("*.req"):
        try:
            data = json.loads(req.read_text("utf-8"))
            if now - float(data.get("ts", 0)) > REQ_TTL or data.get("digest") not in want:
                continue
            ack = req.with_suffix(".ack")
            tmp = req.with_suffix(".ack.tmp")
            tmp.write_text(json.dumps({"ts": now}), encoding="utf-8")
            tmp.replace(ack)
            acked.append(req.stem)
        except (OSError, ValueError, TypeError):
            continue
    return acked


def _journal(line: str) -> None:
    """One line per attempt into the journal (syslog); stderr as well for the caller's log."""
    print(f"agent2telegram inject: {line}", file=sys.stderr)
    try:
        import syslog
        syslog.openlog("agent2telegram-inject")
        syslog.syslog(syslog.LOG_INFO, line)
    except Exception:  # noqa: BLE001 — no syslog (tests, odd platforms) must not fail inject
        pass


def run(cfg, text: str, timeout: float = 60, session=None, poll: float = 0.25) -> int:
    """Deliver *text* and wait for the hook's ack. ``session`` = a TmuxSession stand-in (tests)."""
    from .session import SessionError, TmuxSession
    text = (text or "").strip()
    preview = normalize(text)[:60]
    if not text:
        _journal("rc=3 empty text")
        return EXIT_TMUX
    if not cfg.tmux_session or not cfg.signal_file:
        _journal("rc=3 config needs tmux_session and signal_file (attach mode, Claude Code hooks)")
        return EXIT_TMUX
    d = inject_dir(cfg.signal_file)
    nonce = uuid.uuid4().hex[:12]
    req, ack = d / f"{nonce}.req", d / f"{nonce}.ack"
    try:
        d.mkdir(parents=True, exist_ok=True)
        req.write_text(json.dumps({"digest": digest(text), "ts": time.time()}), encoding="utf-8")
    except OSError as e:
        _journal(f"rc=3 cannot write request ({e.__class__.__name__}) session={cfg.tmux_session}")
        return EXIT_TMUX
    try:
        if session is None:
            from .attach import _expected_agent_commands
            session = TmuxSession([], name=cfg.tmux_session, cwd=Path.home(), origin_prefix="",
                                  boot_wait=0, expected_agent_commands=_expected_agent_commands(cfg))
        try:
            session.inject(text)
        except SessionError as e:
            rc = EXIT_NOT_AGENT if "refusing to inject" in str(e) else EXIT_TMUX
            _journal(f"rc={rc} session={cfg.tmux_session} {e} text={preview!r}")
            return rc
        except Exception as e:  # noqa: BLE001 — subprocess / timeout from tmux
            _journal(f"rc=3 session={cfg.tmux_session} tmux: {e.__class__.__name__}: {e} text={preview!r}")
            return EXIT_TMUX
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ack.exists():
                _journal(f"rc=0 session={cfg.tmux_session} acknowledged by hook text={preview!r}")
                return EXIT_OK
            time.sleep(poll)
        _journal(f"rc=2 session={cfg.tmux_session} no hook ack within {timeout:g}s text={preview!r}")
        return EXIT_NO_ACK
    finally:
        for p in (req, ack):
            try:
                p.unlink()
            except OSError:
                pass
        _cleanup_stale(d)


def _cleanup_stale(d: Path) -> None:
    now = time.time()
    for p in d.glob("*"):
        try:
            if now - p.stat().st_mtime > REQ_TTL:
                p.unlink()
        except OSError:
            pass


def cmd(args) -> int:
    from .config import ConfigError, load
    if getattr(args, "config", None):
        os.environ["AGENT2TELEGRAM_CONFIG"] = args.config
    try:
        cfg = load()
    except ConfigError as e:
        _journal(f"rc=3 config: {e}")
        return EXIT_TMUX
    text = args.text if args.text is not None else sys.stdin.read()
    return run(cfg, text, timeout=args.timeout)
