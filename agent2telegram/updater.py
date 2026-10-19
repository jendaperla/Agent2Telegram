"""``agent2telegram update`` — pull the latest code and restart the running bridge(s).

Mirrors the bridge's install story: the source lives in ``~/.agent2telegram-src`` (the clone the
installer made). We ``git pull`` it, reinstall if it was pip-installed, then restart every running
bridge **on the new code** — preserving each bridge's own ``--config`` so a multi-bridge install
keeps all of its bots. No re-setup, no token re-entry.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
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


@dataclass
class RestartPlan:
    pid: str
    config: str | None
    service: str = ''
    env: dict = field(default_factory=dict, repr=False)


def _launchd_bridge() -> tuple[str, str | None]:
    """Only the documented per-user service, and only its actual top-level PID."""
    if sys.platform != 'darwin':
        return '', None
    target = f'gui/{os.getuid()}/com.agent2telegram.bridge'
    try:
        result = subprocess.run(['launchctl', 'print', target], capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Cannot inspect launchd; no bridges restarted.') from exc
    if result.returncode:
        # 113: service not loaded; 3: GUI domain does not exist (headless login).
        if result.returncode not in (3, 113):
            raise RuntimeError('Cannot inspect launchd service; no bridges restarted.')
        return target, None
    match = re.search(r'^\tpid = (\d+)\s*$', result.stdout, re.MULTILINE)
    return target, match[1] if match else None


def _file_environment(config: str | None) -> dict:
    """Fallback for self-contained configs; never inherit another bot's overrides."""
    from .config import Config, ConfigError
    if not config or not Path(config).is_absolute():
        return {}
    try:
        raw = json.loads(Path(config).read_text('utf-8'))
        cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})
        cfg.validate()
        if not cfg.state_dir:
            return {}
    except (OSError, ValueError, TypeError, AttributeError, ConfigError):
        return {}
    env = {k: v for k, v in os.environ.items()
           if k in ('HOME', 'USER', 'LOGNAME', 'SHELL', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL')}
    env['AGENT2TELEGRAM_CONFIG'] = config
    env['AGENT2TELEGRAM_STATE'] = str(Path(cfg.state_dir).expanduser())
    return env


def _plan_restarts(bridges: list[tuple[str, str | None]], src: Path) -> list[RestartPlan]:
    """Inspect everything BEFORE pull. An unknown environment means manual restart."""
    service, service_pid = _launchd_bridge() if bridges else ('', None)
    plans = []
    for pid, cfg in bridges:
        if pid == service_pid:
            plans.append(RestartPlan(pid, cfg, service=service))
            continue
        env = _proc_env(pid) or _file_environment(cfg)
        if env:
            env['PYTHONPATH'] = str(src) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        plans.append(RestartPlan(pid, cfg, env=env))
    return plans


def _manual_restart(plan: RestartPlan, src: Path) -> None:
    print(f'WARNING: source updated; bridge PID {plan.pid} is still on its previous code. '
          'Its original environment could not be recovered.')
    print('Use its original supervisor to restart ONLY this bridge; leave agent/tmux sessions running.')
    print('For a manually started bridge, stop it first, then run this command in its original '
          'environment. Required variables must contain its ORIGINAL config/state paths:')
    config = shlex.quote(plan.config) if plan.config else (
        '"${AGENT2TELEGRAM_CONFIG:?Set the original bridge config path}"')
    state = '"${AGENT2TELEGRAM_STATE:?Set the original bridge state path}"'
    argv = [sys.executable, '-m', 'agent2telegram', 'run']
    if plan.config:
        argv += ['--config', plan.config]
    print(f'  PYTHONPATH={shlex.quote(str(src))} AGENT2TELEGRAM_CONFIG={config} '
          f'AGENT2TELEGRAM_STATE={state} {shlex.join(argv)}')


def _restart(plans: list[RestartPlan], src: Path) -> int:
    # A keeper may have replaced a process during pull. Do not signal a stale selection.
    current = set(_running_bridges())
    for plan in plans:
        if (plan.env or plan.service) and (plan.pid, plan.config) not in current:
            raise RuntimeError('Bridge processes changed during update; restart through their supervisor.')
    restarted = 0
    for plan in plans:
        pid, cfg, env = plan.pid, plan.config, plan.env
        if plan.service:
            if _launchd_bridge() != (plan.service, pid):
                raise RuntimeError('launchd bridge changed during update; no replacement started.')
            result = subprocess.run(['launchctl', 'kickstart', '-k', plan.service],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f'launchctl kickstart failed; retry: launchctl kickstart -k {plan.service}')
            restarted += 1
            continue
        if not env:
            _manual_restart(plan, src)
            continue
        # Preserve the bridge's ORIGINAL environment (config selection, PYTHONPATH, PATH) so it
        # comes back identical — critical for env-configured / multi-bridge setups.
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        if not _wait_gone(pid):
            raise RuntimeError('Old bridge did not exit; replacement not started.')
        argv = [sys.executable, "-m", "agent2telegram", "run"] + (["--config", cfg] if cfg else [])
        # Config state_dir also applies when a Linux process did not use an env override.
        config_env = _file_environment(cfg or env.get('AGENT2TELEGRAM_CONFIG'))
        logdir = Path(env.get('AGENT2TELEGRAM_STATE') or config_env.get('AGENT2TELEGRAM_STATE')
                      or Path(env.get('HOME', str(Path.home()))) / '.local/state/agent2telegram')
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
        plans = [] if no_restart else _plan_restarts(_running_bridges(), src)
        for attempt in range(3):
            r = subprocess.run(['git', '-C', str(src), 'pull', '--ff-only'],
                               capture_output=True, text=True, env=env, timeout=120)
            if r.returncode == 0:
                break
            if attempt < 2:
                time.sleep(2)
    except RuntimeError as exc:
        print(f'Update stopped before pull: {exc}')
        return 1
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
    _backfill_usernames()
    if no_restart:
        print('✓ Source updated. Running bridges unchanged; restart them through their supervisor.')
        return 0
    if plans:
        try:
            n = _restart(plans, src)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            # Do not include subprocess output/environment, which may contain credentials.
            if not isinstance(exc, RuntimeError):
                exc = f'Restart failed ({type(exc).__name__}); check the bridge supervisor.'
            print(f'✗ {exc}')
            return 1
        print(f"✓ Updated and restarted {n} bridge(s) on the new code.")
    else:
        from .config import load, ConfigError
        try:
            load().validate()
            command = 'agent2telegram run'
        except (ConfigError, OSError, TypeError, ValueError):
            command = 'agent2telegram setup'
        print(f"✓ Updated. No running bridge found – next step:  {command}")
    return 0
