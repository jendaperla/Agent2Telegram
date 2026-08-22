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
                out.append(json.loads(p.read_text("utf-8")))
            except Exception:
                pass
    return out


def _read_pin(signal: str) -> str | None:
    """The transcript the UserPromptSubmit hook pinned for this bridge, or None (no pin)."""
    try:
        from .prompt_hook import PIN_NAME
        raw = (Path(signal).parent / PIN_NAME).read_text("utf-8")
        return json.loads(raw).get("transcript") or None
    except (OSError, ValueError):
        return None


def _same_file(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return a == b


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

    cfgs = [c for c in _all_cfgs() if c.get("signal_file")]
    if not cfgs:                                   # legacy: just the env/default config
        c = _load_cfg()
        if c.get("signal_file"):
            cfgs = [c]

    # A config whose bridge has a PIN (the UserPromptSubmit hook recorded which transcript the
    # driven session writes) trusts the pin and nothing else: mark only when THIS Stop event is
    # for the pinned transcript. Background-agent sessions fire Stop hooks too (Claude Code
    # ≥ 2.1.232) — without this, any of them would end the bridge's turn mid-work and the
    # backstop could ship a stale answer. Configs without a pin keep the legacy behaviour:
    # session-id guard, then guard-less fallback.
    matched = False
    guardless = []
    for c in cfgs:
        pinned = _read_pin(c["signal_file"])
        if pinned is not None:
            if _same_file(path, pinned):
                _mark(c["signal_file"])
                matched = True
            continue                    # pinned bridge never falls back to the guard paths
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
