"""Claude Code **UserPromptSubmit hook** for Agent2Telegram (attach mode).

Why this exists: the attach bridge has to know *which* transcript file the driven session is
writing, and it used to guess — tail the newest ``*.jsonl`` in the session's per-cwd project
directory. Claude Code ≥ 2.1.232 breaks that guess: background-agent sessions write sibling
transcripts into the same directory, so the "newest file" can change mid-turn and the bridge
switches away from the transcript the answer is being written to (losing it — the cursor in
the abandoned file is discarded).

The fix is to stop guessing. Claude Code runs this hook the moment a prompt is submitted and
hands it the exact ``transcript_path``. When the prompt is one the bridge injected (it starts
with the bridge's origin prefix), we record that path where the bridge can read it — the
"pin". The bridge then follows the pin instead of the mtime heuristic.

The origin-prefix filter is essential, not cosmetic: UserPromptSubmit also fires for
``<task-notification>`` turns (background-task results delivered to the model) and for
anything typed in the terminal. None of those may move the pin.

Register it once (the wizard and ``agent2telegram install-hooks`` do this) in
``~/.claude/settings.json``::

    {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
       "command": "python3 -m agent2telegram.prompt_hook", "timeout": 15}]}]}}

For UserPromptSubmit specifically, anything printed to stdout is injected into the model's
context and a non-zero exit blocks the prompt — so this hook prints NOTHING, never raises,
and always exits 0. Configs contain the bot token: no exception path may print one.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

#: File name of the pin, written next to the bridge's signal file.
PIN_NAME = "active_transcript"

#: Origin prefixes accepted in addition to the configured one — must stay in sync with the
#: legacy set in AttachBridge._origins, so hook and bridge agree on what "from Telegram" means.
LEGACY_PREFIXES = ("Telegram:", "[TG]")


def _load_cfgs() -> list[dict]:
    """Every bridge config in the config directory (one per `agent2telegram connect`)."""
    try:
        from .config import config_path
        d = Path(config_path()).parent
    except Exception:
        return []
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text("utf-8")))
            except Exception:
                pass
    return out


def _resolved(p: str) -> str:
    try:
        return str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError):
        return p


def _prefixes(cfg: dict) -> tuple:
    return tuple({p for p in ((cfg.get("origin_prefix") or "").strip(), *LEGACY_PREFIXES) if p})


def _pin(signal: str, transcript: str, session_id: str) -> None:
    target = Path(signal).expanduser().parent / PIN_NAME
    payload = json.dumps({"transcript": transcript, "session_id": session_id,
                          "ts": time.time()})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (PIN_NAME + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)              # atomic: the bridge never reads a half-written pin
    except OSError:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    transcript = payload.get("transcript_path")
    prompt = (payload.get("prompt") or "").lstrip()
    if not transcript or not prompt:
        return
    cwd = payload.get("cwd") or ""
    session_id = payload.get("session_id") or ""

    for c in _load_cfgs():
        if not c.get("signal_file"):
            continue
        # Claude-only: a Codex bridge has a signal_file too, but Codex has no hooks and its
        # transcripts live elsewhere — a pin there would point it at a Claude transcript.
        if (c.get("agent") or "").lower() != "claude-code":
            continue
        if not prompt.startswith(_prefixes(c)):
            continue
        # Scope to the bridge's tmux-session cwd when both sides are known, so two Claude
        # bridges on one machine can't pin each other's transcript (cross-chat delivery).
        want = (c.get("session_cwd") or "").strip()
        if want and cwd and _resolved(want) != _resolved(cwd):
            continue
        _pin(c["signal_file"], transcript, session_id)


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
