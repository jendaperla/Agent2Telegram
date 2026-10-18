"""``agent2telegram update`` — pull the latest code and restart the running bridge(s).

Mirrors the bridge's install story: the source lives in ``~/.agent2telegram-src`` (the clone the
installer made). We ``git pull`` it, reinstall if it was pip-installed, then restart every running
bridge **on the new code** — preserving each bridge's own ``--config`` so a multi-bridge install
keeps all of its bots. No re-setup, no token re-entry.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import agent2telegram


def _src() -> Path:
    return Path.home() / ".agent2telegram-src"


def _running_bridges() -> list[tuple[str, str | None]]:
    """Each running bridge as (pid, --config path or None). Uses ``ps`` for the full command line
    so it works the same on Linux and macOS."""
    try:
        result = subprocess.run(['ps', '-axww', '-o', 'uid=,pid=,command='],
                                capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Cannot inspect bridge processes; no process changes made.') from exc
    out = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] != str(os.getuid()) or not parts[1].isdigit():
            continue
        pid, cmd = parts[1:]
        if int(pid) == os.getpid():
            continue
        try:
            toks = shlex.split(cmd)
        except ValueError:
            continue
        if not toks:
            continue
        executable = Path(toks[0]).name.lower()
        if executable.startswith('python'):
            if toks[1:3] == ['-m', 'agent2telegram']:
                args = toks[3:]
            elif len(toks) > 1 and Path(toks[1]).name == 'agent2telegram':
                args = toks[2:]
            else:
                continue
        elif executable == 'agent2telegram':
            args = toks[1:]
        else:
            continue
        if args[:1] in (['-v'], ['--verbose']):
            args = args[1:]
        if args[:1] != ['run']:
            continue
        cfg = None
        if '--config' in args:
            i = args.index('--config')
            if i + 1 < len(args):
                cfg = args[i + 1]
        for arg in args:
            if arg.startswith('--config='):
                cfg = arg.partition('=')[2]
        out.append((pid, cfg))
    return out


def _proc_env(pid: str) -> dict:
    """Best-effort read of a process's own environment (Linux ``/proc/<pid>/environ``). Lets us
    relaunch a bridge with the SAME ``AGENT2TELEGRAM_CONFIG`` / ``PYTHONPATH`` / ``PATH`` it was
    started with — a supervisor may start attach-mode bridges through an
    env var rather than a ``--config`` flag, so without this the relaunch would silently fall back
    to the DEFAULT config and the bridge would come back wrong (or without the new key). Empty on
    platforms without ``/proc`` (e.g. macOS), where the existing supervisor must restart it."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for chunk in raw.split(b"\x00"):
        if b"=" in chunk:
            k, _, v = chunk.partition(b"=")
            try:
                env[k.decode()] = v.decode()
            except UnicodeDecodeError:
                pass
    return env


def _wait_gone(pid: str, timeout: float = 6.0) -> bool:
    """Wait until *pid* has actually exited (so it releases the bot's getUpdates long-poll before
    the replacement starts — two pollers on one token would 409). Escalates to SIGKILL near the end."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', pid], capture_output=True, text=True, timeout=5)
            if state.returncode == 0 and state.stdout.strip().startswith('Z'):
                return True  # Exited, awaiting parent reaping; no poller remains.
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.kill(int(pid), 0)            # probe: alive?
        except (OSError, ValueError):
            return True                     # gone
        if deadline - time.time() < 2.0:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return True
    return False


def _restart(bridges: list[tuple[str, str | None]], src: Path) -> int:
    state = Path.home() / ".local" / "state" / "agent2telegram"
    state.mkdir(parents=True, exist_ok=True)
    # Without /proc, a supervisor's environment cannot be reconstructed safely.
    # Refuse before stopping ANY bridge; the owner can use their existing supervisor.
    plans = []
    for pid, cfg in bridges:
        env = _proc_env(pid)
        if not env:
            raise RuntimeError('Cannot recover the bridge environment; use update --no-restart '
                               'and restart bridge processes through their supervisor.')
        env['PYTHONPATH'] = str(src) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        plans.append((pid, cfg, env))
    restarted = 0
    for pid, cfg, env in plans:
        # Preserve the bridge's ORIGINAL environment (config selection, PYTHONPATH, PATH) so it
        # comes back identical — critical for env-configured / multi-bridge setups.
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        if not _wait_gone(pid):
            raise RuntimeError('Old bridge did not exit; replacement not started.')
        argv = [sys.executable, "-m", "agent2telegram", "run"] + (["--config", cfg] if cfg else [])
        logdir = Path(env.get('AGENT2TELEGRAM_STATE', state))
        logdir.mkdir(parents=True, exist_ok=True)
        with open(logdir / 'run.log', 'a') as log:
            child = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=src)
        time.sleep(0.5)
        if child.poll() is not None:
            raise RuntimeError('Replacement bridge exited during startup; check its run.log.')
        restarted += 1
    return restarted


def _backfill_usernames() -> None:
    """Fill in each bridge config's bot @username (non-secret) so other tools — e.g. the Agents
    Monitoring dashboard — can show a 't.me/<bot>' link without ever touching the token."""
    from .config import config_path, load, save
    from .telegram import TelegramClient
    d = config_path().parent
    if not d.is_dir():
        return
    for p in sorted(d.glob("*.json")):
        try:
            cfg = load(p)
        except Exception:
            continue
        if cfg.bot_username:
            continue
        try:
            me = TelegramClient(cfg.token).get_me()
            if me.get("username"):
                cfg.bot_username = me["username"]
                save(cfg, p)
        except Exception:
            pass


def run(*, no_restart: bool = False) -> int:
    src = _src()
    if not (src / ".git").exists():
        print(f"No source clone at {src}. Re-run the installer, or update manually "
              "(git pull / pip install -U).")
        return 1
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    try:
        status = subprocess.run(['git', '-C', str(src), 'status', '--porcelain'],
                                capture_output=True, text=True, env=env, timeout=30)
        if status.returncode or status.stdout.strip():
            print('Cannot update: source has local changes or git status failed. Files preserved.')
            return 1
        for attempt in range(3):
            r = subprocess.run(['git', '-C', str(src), 'pull', '--ff-only'],
                               capture_output=True, text=True, env=env, timeout=120)
            if r.returncode == 0:
                break
            if attempt < 2:
                time.sleep(2)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'Update failed ({type(exc).__name__}); no bridges restarted.')
        return 1
    print((r.stdout + r.stderr).strip()[:400] or "(no output)")
    if r.returncode != 0:
        return 1
    # If running from a pip install (not straight from this clone), reinstall the refreshed code.
    if str(src.resolve()) not in str(Path(agent2telegram.__file__).resolve()):
        if subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", str(src)],
                          capture_output=True).returncode != 0:
            if subprocess.run([sys.executable, "-m", "pip", "install", "--user",
                               "--break-system-packages", "--upgrade", str(src)], capture_output=True).returncode != 0:
                print('Package installation failed; no bridges restarted. Re-run install.sh to repair.')
                return 1
    if no_restart:
        print('✓ Source updated. Running bridges unchanged; restart them through their supervisor.')
        return 0
    try:
        bridges = _running_bridges()
    except RuntimeError as exc:
        print(f'✗ {exc}')
        return 1
    if bridges:
        try:
            n = _restart(bridges, src)
        except RuntimeError as exc:
            print(f'✗ {exc}')
            return 1
        print(f"✓ Updated and restarted {n} bridge(s) on the new code.")
    else:
        print("✓ Updated. No running bridge found — start one with:  agent2telegram run")
    return 0
