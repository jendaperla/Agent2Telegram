"""Shared, non-destructive Claude Stop-hook registration for setup, run and doctor.

Custom Python hooks are inspected, never executed: a statically resolved write to this
bridge's marker counts as an existing hook. Dynamic/custom project hooks require manual
inspection; doctor checks the active user settings, not managed/project policy overrides.
"""
from __future__ import annotations

import ast
import fcntl
import json
import logging
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

from .config import config_path, load

log = logging.getLogger(__name__)


def settings_path() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser() / 'settings.json'


def repair_command(path: Path | None = None) -> str:
    return shlex.join(['env', f'PYTHONPATH={Path(__file__).resolve().parent.parent}',
                       f'CLAUDE_CONFIG_DIR={settings_path().parent.resolve()}',
                       sys.executable, '-m', 'agent2telegram', 'claude-hook',
                       '--config', str(path or config_path())])


def _literal(node, names):
    """Resolve only literal paths and common path constructors, with no eval/import."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _literal(node.left, names), _literal(node.right, names)
        return str(Path(left) / right) if left is not None and right is not None else None
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name == 'Path.home' and not node.args:
            return str(Path.home())
        if len(node.args) == 1 and name in ('Path', 'str', 'os.path.expanduser'):
            value = _literal(node.args[0], names)
            return os.path.expanduser(value) if value is not None else None
    return None


def _writes_marker(script: Path, marker: Path) -> bool:
    try:
        if not script.is_absolute() or script.stat().st_size > 256 * 1024:
            return False
        tree = ast.parse(script.read_text('utf-8'))
        names = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = _literal(node.value, names)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name) and node.func.id == 'open' and node.args:
                mode = node.args[1] if len(node.args) > 1 else next(
                    (kw.value for kw in node.keywords if kw.arg == 'mode'), None)
                if mode is not None and _literal(mode, names) in ('w', 'a', 'wb', 'ab'):
                    target = _literal(node.args[0], names)
            elif isinstance(node.func, ast.Attribute) and node.func.attr in ('write_text', 'write_bytes', 'touch'):
                target = _literal(node.func.value, names)
            if target and Path(target).is_absolute() and Path(target).resolve() == marker:
                return True
    except (OSError, UnicodeError, SyntaxError, ValueError):
        pass
    return False


def _covers(command: str, marker: Path, cfg_path: Path) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv or any(arg in (';', '&&', '||', '|', '>', '>>', '<') for arg in argv):
        return False
    bound = str(Path.home() / '.config/agent2telegram/config.json')
    if Path(argv[0]).name == 'env':
        argv.pop(0)
    while argv and '=' in argv[0]:
        key, value = argv.pop(0).split('=', 1)
        if not key.isidentifier():
            return False
        if key == 'AGENT2TELEGRAM_CONFIG':
            bound = value
    if not argv:
        return False
    executable = Path(argv.pop(0)).expanduser()
    if executable.suffix == '.py' and not argv:
        return _writes_marker(executable, marker)
    if not executable.name.startswith('python'):
        return False
    while argv and argv[0] in ('-u', '-B'):
        argv.pop(0)
    # The global module hook scans the directory of its configured bridge config.
    # An explicit environment binding must not accidentally cover a different directory.
    if argv == ['-m', 'agent2telegram.stop_hook']:
        return Path(bound).expanduser().resolve().parent == cfg_path.resolve().parent
    # Accept real custom marker writers, not merely a script named "stop" or a comment.
    return bool(argv and argv[0].endswith('.py') and _writes_marker(Path(argv[0]).expanduser(), marker))


def _read(path: Path) -> tuple[dict, bytes | None]:
    if path.is_symlink():
        raise ValueError('symlinked settings; inspect the target manually')
    raw = path.read_bytes() if path.exists() else None
    data = json.loads(raw) if raw is not None else {}
    if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
        raise ValueError('invalid settings structure')
    stops = data.get('hooks', {}).get('Stop', [])
    if not isinstance(stops, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get('hooks', []), list)
        or any(not isinstance(h, dict) for h in entry.get('hooks', [])) for entry in stops
    ):
        raise ValueError('invalid Stop hook structure')
    if data.get('disableAllHooks'):
        raise ValueError('hooks are disabled; enable them explicitly before registering')
    return data, raw


def _present(data: dict, cfg, path: Path) -> bool:
    marker = (Path(cfg.signal_file).expanduser().parent / 'turn_end').resolve()
    return any(hook.get('type') == 'command' and isinstance(hook.get('command'), str)
               and _covers(hook['command'], marker, path)
               for entry in data.get('hooks', {}).get('Stop', []) for hook in entry.get('hooks', []))


def check(cfg, path: Path | None = None) -> tuple[bool, str]:
    if not cfg.signal_file:
        return False, 'signal_file is empty; configure a marker path first'
    try:
        data, _ = _read(settings_path())
        found = _present(data, cfg, path or config_path())
        return found, 'present' if found else 'missing'
    except (OSError, ValueError, UnicodeError) as exc:
        # Never print invalid JSON/settings content (it can contain credentials).
        return False, f'cannot safely inspect settings ({type(exc).__name__})'


def register(cfg=None, path: Path | None = None) -> str:
    """Return present/registered/error. Preserve invalid files; serialize concurrent starts."""
    path = Path(path or config_path()).expanduser().resolve()
    settings = settings_path()
    try:
        cfg = cfg or load(path)
        if not cfg.signal_file:
            raise ValueError('signal_file required')
        settings.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(settings) + '.a2t.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data, raw = _read(settings)
            if _present(data, cfg, path):
                return 'present'
            command = shlex.join(['env', f'AGENT2TELEGRAM_CONFIG={path}',
                f'PYTHONPATH={Path(__file__).resolve().parent.parent}',
                sys.executable, '-m', 'agent2telegram.stop_hook'])
            data.setdefault('hooks', {}).setdefault('Stop', []).append(
                {'matcher': '', 'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]})
            fd, staged = tempfile.mkstemp(prefix='.settings-a2t-', dir=settings.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    if raw is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(settings.stat().st_mode))
                    json.dump(data, stream, indent=2); stream.write('\n')
                    stream.flush(); os.fsync(stream.fileno())
                if _read(settings)[1] != raw:
                    raise ValueError('settings changed concurrently')
                os.replace(staged, settings)
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
        log.warning('Claude Stop hook registered in %s. Restart the existing Claude session '
                    'using resume (same session ID) to load it; the bridge does not restart agents.', settings)
        return 'registered'
    except (OSError, ValueError, UnicodeError) as exc:
        log.warning('Claude Stop hook registration failed (%s); settings preserved. '
                    'Inspect %s and signal_file, then run: %s',
                    type(exc).__name__, settings, repair_command(path))
        return 'error'
