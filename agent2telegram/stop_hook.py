"""Claude Code **Stop hook** for Agent2Telegram (attach mode).

Claude Code has no end-of-turn record in its transcript, so to switch the "typing…" indicator
off and clear the live status bubble at the *exact* moment a turn ends, the bridge relies on a
tiny marker file. Claude Code runs this hook at the end of every assistant turn; it writes that
marker for the matching session. (Codex needs no hook — its rollout log records ``task_complete``.)

The bridge forwards every assistant message by tailing the transcript directly, so this hook does
**not** send anything itself — it only signals "turn ended".

Register it once (the setup wizard does this) in the agent's settings, e.g. ``~/.claude/settings.json``::

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
       "command": "python3 -m agent2telegram.stop_hook"}]}]}}

It reads the signal path / optional session guard from the Agent2Telegram config, so the command
line needs no arguments. Always exits 0 (never blocks the agent).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _load_cfg() -> dict:
    try:
        from .config import config_path
        return json.loads(Path(config_path()).read_text("utf-8"))
    except Exception:
        return {}


def _all_cfgs() -> list[dict]:
    """Every bridge config in the config directory — so a single global Stop hook serves multiple
    Claude bridges (each `agent2telegram connect` writes its own <name>.json)."""
    try:
        from .config import config_path
        d = Path(config_path()).parent
    except Exception:
        return []
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                item = json.loads(p.read_text("utf-8"))
                if isinstance(item, dict):
                    out.append(item)
            except Exception:
                pass
    return out


def _mark(signal: str) -> None:
    marker = Path(signal).parent / "turn_end"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    path = payload.get("transcript_path")
    if not path:
        return
    base = os.path.basename(path)

    def eligible(c):
        # Legacy configs omitted agent/mode. Never finish a Codex turn from a Claude hook.
        return (isinstance(c, dict) and c.get('agent', 'claude-code') == 'claude-code'
                and c.get('mode', 'attach') == 'attach' and c.get('signal_file'))

    cfgs = [c for c in _all_cfgs() if eligible(c)]
    if not cfgs:                                   # legacy: just the env/default config
        c = _load_cfg()
        if eligible(c):
            cfgs = [c]

    # A config with a session guard fires only for ITS session. If none matched (e.g. a single
    # legacy bridge with no guard), fall back to the guard-less configs — old behaviour preserved.
    matched = False
    guardless = []
    for c in cfgs:
        guard = c.get("claude_session_id", "")
        if guard:
            if base.startswith(guard):
                _mark(c["signal_file"])
                matched = True
        else:
            guardless.append(c)
    if not matched:
        for c in guardless:
            _mark(c["signal_file"])


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
