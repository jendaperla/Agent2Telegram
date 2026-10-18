"""Optional Agents Monitoring integration through its public CLI, never its config."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def command():
    executable = shutil.which('agentsmon')
    if executable:
        return [executable]
    try:
        if importlib.util.find_spec('agentsmon') is not None:
            return [sys.executable, '-m', 'agentsmon']
    except (ImportError, ValueError, OSError):
        pass
    python = shutil.which('python3')
    if python and python != sys.executable:
        try:
            result = subprocess.run([python, '-c',
                'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("agentsmon") else 1)'],
                capture_output=True, timeout=5)
            if result.returncode == 0:
                return [python, '-m', 'agentsmon']
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def register_bridge(path, state, env):
    """Best effort after startup; a missing/old monitor never prevents bridging."""
    cli = command()
    if cli is None:
        print('  Agents Monitoring is optional and is not installed; bridge startup is unchanged.')
        return
    argv = cli + ['add', '--non-interactive', '--bridge-config', str(Path(path).absolute()),
                  '--bridge-python', os.path.abspath(sys.executable),
                  '--bridge-state', str(Path(env.get('AGENT2TELEGRAM_STATE') or state).expanduser().absolute())]
    if env.get('PYTHONPATH'):
        argv += ['--bridge-pythonpath', env['PYTHONPATH']]
    try:
        result = subprocess.run(argv, env=env, capture_output=True, timeout=15)
        ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if ok:
        print('  ✓ This bridge is registered with Agents Monitoring (idempotent).')
    else:
        print('  Warning: bridge not registered with Agents Monitoring; update it to 0.3.2+ and run agentsmon add.')
