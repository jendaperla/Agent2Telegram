#!/usr/bin/env bash
# Agent2Telegram one-command uninstaller — the mirror of install.sh.
# Usage:  curl -fsSL <raw-url>/uninstall.sh | bash
# Stops any running bridge and removes config, state, the source clone and the pip package.
set -euo pipefail

# Recover the working directory if it was deleted out from under us.
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

PY="$(command -v python3 || command -v python || true)"

# Find a way to run the package's own `uninstall` (it does all the heavy lifting):
#   1) installed on PATH (pip), or 2) the source clone we created at install time.
run_uninstall() {
  [ -n "$PY" ] || return 1
  if "$PY" -m agent2telegram --version >/dev/null 2>&1; then
    "$PY" -m agent2telegram uninstall --yes; exit $?
  fi
  if [ -d "$HOME/.agent2telegram-src/agent2telegram" ]; then
    env "PYTHONPATH=$HOME/.agent2telegram-src" "$PY" -m agent2telegram uninstall --yes; exit $?
  fi
  return 1
}

if run_uninstall; then
  exit 0
fi

# Without the package we cannot safely identify its processes or their owner.
say "Package not found. No processes stopped and no files removed."
say "Re-run install.sh --no-setup, then run agent2telegram uninstall --yes."
exit 1
