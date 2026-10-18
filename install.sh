#!/usr/bin/env bash
# Agent2Telegram one-command installer.
# Usage:  curl -fsSL <raw-url>/install.sh | bash      (or run it from a clone)
# It checks Python, installs the package for the current user, and launches setup.
# Parse everything before an update can replace the script currently being read.
main() {
set -euo pipefail
NO_SETUP=0
NO_PIP=0
for arg in "$@"; do
  case "$arg" in
    --no-setup) NO_SETUP=1 ;;
    --no-pip) NO_PIP=1 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# Recover the working directory if it was deleted (e.g. you just uninstalled while sitting in
# the source clone) — otherwise git/curl fail with "cannot access parent directories: getcwd".
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

REPO="https://github.com/petrludwig-collab/Agent2Telegram.git"
NEED_PY_MAJOR=3
NEED_PY_MINOR=10

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Pick where to drop the launcher. Prefer a directory ALREADY on PATH and writable — then the
# command works in the *current* terminal immediately (a piped installer can't change the parent
# shell's PATH, so installing into a dir it already searches is the only way to "just work" now).
# Fall back to ~/.local/bin (added to PATH via the rc files for future shells).
pick_bindir() {
  oldifs="$IFS"; IFS=:
  for d in $PATH; do
    case "$d" in
      "$HOME"/*) if [ -d "$d" ] && [ -w "$d" ]; then IFS="$oldifs"; printf '%s' "$d"; return; fi ;;
    esac
  done
  IFS="$oldifs"; printf '%s' "$HOME/.local/bin"
}

# 0) Preflight: collect EVERYTHING missing up front and give one install command, instead of
#    letting the user discover python, then tmux, then git one failed run at a time.
need=""
command -v python3 >/dev/null 2>&1 || need="$need python3"
command -v tmux    >/dev/null 2>&1 || need="$need tmux"      # attach mode drives a tmux session
# git is optional: the public source archive also works without it.
if [ -n "$need" ]; then
  need="${need# }"
  if command -v apt-get >/dev/null 2>&1; then
    err "Missing: $need — install all at once:  sudo apt-get update && sudo apt-get install -y $need"
  elif command -v dnf >/dev/null 2>&1; then
    err "Missing: $need — install all at once:  sudo dnf install -y $need"
  elif command -v brew >/dev/null 2>&1; then
    err "Missing: $need — install all at once:  brew install $need"
  else
    err "Missing: $need — install these with your package manager, then re-run."
  fi
fi

# 1) Python version check (presence is guaranteed by the preflight above)
PY="$(command -v python3)"
"$PY" - <<'PYEOF' || err "Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ required."
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)
PYEOF
say "Using $("$PY" --version)"

# 2) Get the code (clone if we're not already inside it)
if [ -f pyproject.toml ] && grep -q 'agent2telegram' pyproject.toml &&
   [ ! -e .a2t-archive-install ] && [ ! -L .a2t-archive-install ] &&
   { [ "$(pwd -P)" != "$(cd "$HOME" && pwd -P)/.agent2telegram-src" ] || [ -e .git ]; }; then
  SRC="$(pwd)"
  say "Installing from current directory"
else
  SRC="${HOME}/.agent2telegram-src"
  if [ -f .a2t-archive-install ]; then SRC="$(pwd -P)"; fi
  cd "$HOME"
  # git first: a real clone keeps `agent2telegram update` working later. But git over HTTPS
  # can be refused where plain HTTPS is fine — a datacentre IP that GitHub rate-limits for
  # anonymous git answers 401 and git then asks for a password, on a PUBLIC repo and on a
  # brand-new machine. That is a bad way to meet a tool, so falling back to the tarball keeps
  # the install going. Never prompt: an unattended installer must not stop on a password.
  is_archive_install() {
    [ -d "$SRC" ] && [ ! -L "$SRC" ] || return 1
    [ ! -e "$SRC/.git" ] && [ ! -L "$SRC/.git" ] || return 1
    [ -f "$SRC/pyproject.toml" ] && [ -f "$SRC/agent2telegram/__init__.py" ] &&
      grep -q 'agent2telegram' "$SRC/pyproject.toml" || return 1
    if [ -e "$SRC/.a2t-archive-install" ] || [ -L "$SRC/.a2t-archive-install" ]; then
      [ ! -L "$SRC/.a2t-archive-install" ] &&
        [ "$(cat "$SRC/.a2t-archive-install")" = a2t-archive-v1 ] || return 1
    else
      # Legacy installers had no marker; recognise only the intact default location.
      [ "$SRC" = "${HOME}/.agent2telegram-src" ] || return 1
    fi
    [ -z "$(find "$SRC" -type l -print -quit)" ]
  }
  fetch_tarball() (
    say "Fetching the source archive (no git)"
    tmp="$(mktemp -d "${SRC}.stage.XXXXXX")" || return 1
    trap 'rm -rf "$tmp"' EXIT
    url="https://codeload.github.com/petrludwig-collab/Agent2Telegram/tar.gz/refs/heads/main"
    curl -fsSL --retry 2 --connect-timeout 15 --max-time 120 "$url" -o "$tmp/src.tgz" || return 1
    # Reject links and paths outside the staging directory, on Python 3.10 too.
    "$PY" - "$tmp" <<'PYARCHIVE' || return 1
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1]).resolve()
with tarfile.open(root / 'src.tgz') as archive:
    for member in archive.getmembers():
        target = (root / member.name).resolve()
        if root not in target.parents or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe archive member')
    archive.extractall(root)
PYARCHIVE
    dir="$(find "$tmp" -maxdepth 1 -type d -name 'Agent2Telegram-*' | head -1)"
    [ -n "$dir" ] || return 1
    [ -f "$dir/agent2telegram/__init__.py" ] &&
      grep -q 'agent2telegram' "$dir/pyproject.toml" || return 1
    printf 'a2t-archive-v1\n' > "$dir/.a2t-archive-install"
    backup=""
    if [ -e "$SRC" ] || [ -L "$SRC" ]; then
      is_archive_install || return 1
      backup="$(mktemp -d "${SRC}.backup.XXXXXX")" || return 1
      rmdir "$backup" || return 1
      mv "$SRC" "$backup" || return 1
    fi
    if ! mv "$dir" "$SRC"; then
      [ -z "$backup" ] || mv "$backup" "$SRC"
      return 1
    fi
    [ -z "$backup" ] || say "Previous source and local files preserved in $backup"
    say "Installed from archive — 'agent2telegram update' will ask you to re-run this installer."
  )
  if [ -L "$SRC" ]; then
    err "$SRC is a symlink; files preserved. Update its target manually."
  elif [ -d "$SRC/.git" ] || [ -f "$SRC/.git" ]; then
    say "Updating $SRC"
    status="$(git -C "$SRC" status --porcelain)" || err "Cannot inspect $SRC; files preserved."
    [ -z "$status" ] || err "Local changes in $SRC; commit or back them up first."
    updated=0
    for n in 1 2 3; do
      if GIT_TERMINAL_PROMPT=0 git -C "$SRC" pull --ff-only; then updated=1; break; fi
      [ "$n" -eq 3 ] || { say "Update refused, retrying ($((n+1))/3)"; sleep 2; }
    done
    [ "$updated" -eq 1 ] || err "Update failed; existing checkout preserved."
  elif is_archive_install; then
    fetch_tarball || err "Archive update failed; previous files preserved."
  elif [ -e "$SRC" ]; then
    err "$SRC already exists without a recognised installation; files preserved."
  elif command -v git >/dev/null 2>&1; then
    say "Cloning into $SRC"
    # GitHub answers 401 to anonymous git from datacentre IPs intermittently — the very next
    # attempt usually succeeds (Hermes' own installer recovers the same way, on try 2 of 4).
    # Retry before giving up on git, because a real clone is what keeps `update` working;
    # the tarball is the last resort, not the second choice.
    clone_stage="$(mktemp -d "${SRC}.clone.XXXXXX")"
    trap 'rm -rf "$clone_stage"' EXIT
    n=1
    while [ "$n" -le 3 ]; do
      GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$REPO" "$clone_stage/source" 2>/dev/null && break
      rm -rf "$clone_stage/source"
      n=$((n+1))
      [ "$n" -le 3 ] && { say "Clone refused, retrying ($n/3)"; sleep 2; }
    done
    if [ -d "$clone_stage/source/.git" ]; then
      [ ! -e "$SRC" ] && [ ! -L "$SRC" ] || err "Install target appeared during download; refusing replacement."
      mv "$clone_stage/source" "$SRC"
    else
      fetch_tarball || err "Could not fetch the project."
    fi
    rm -rf "$clone_stage"
    trap - EXIT
  else
    fetch_tarball || err "Could not fetch the project (no git, and the archive download failed)."
  fi
fi

# 3) Make `agent2telegram` a real command. pip is OPTIONAL (the core is pure standard library):
#    if pip is there we install the package too (so hooks can `python3 -m agent2telegram…`), but
#    EITHER WAY we drop our own launcher into a directory on PATH so the command works right after
#    install — no PYTHONPATH to remember, and (when a PATH dir is writable) no new shell needed.
if [ "$NO_PIP" -eq 0 ] && { "$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade >/dev/null 2>&1; }; then
  say "Installing the package"
  "$PY" -m pip install --user --upgrade "$SRC" >/dev/null 2>&1 \
    || "$PY" -m pip install --user --break-system-packages --upgrade "$SRC" >/dev/null 2>&1 || true
fi

BIND="$(pick_bindir)"
mkdir -p "$BIND"
"$PY" - "$SRC" "$PY" "$BIND/agent2telegram" <<'PYLAUNCHER'
import pathlib, shlex, sys
source, interpreter, target = sys.argv[1:]
pathlib.Path(target).write_text('#!/bin/sh\nexec env ' + shlex.quote('PYTHONPATH=' + source) +
                              ' ' + shlex.quote(interpreter) + ' -m agent2telegram "$@"\n')
PYLAUNCHER
chmod +x "$BIND/agent2telegram"
RUN=("$BIND/agent2telegram"); HOW="agent2telegram"
case ":$PATH:" in
  *":$BIND:"*)
    say "Installed launcher in $BIND (already on PATH) — 'agent2telegram' works now." ;;
  *)
    say "Installed launcher in $BIND — adding it to PATH for new shells."
    for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
      [ -e "$rc" ] || continue
      grep -qs "$BIND" "$rc" || echo "export PATH=\"$BIND:\$PATH\"" >> "$rc"
    done
    # ~/.bashrc may not exist on a minimal server — create it so login shells pick it up.
    grep -qs "$BIND" "$HOME/.bashrc" 2>/dev/null || echo "export PATH=\"$BIND:\$PATH\"" >> "$HOME/.bashrc"
    export PATH="$BIND:$PATH"
    say "For THIS terminal, run:  export PATH=\"$BIND:\$PATH\"   (new terminals get it automatically)" ;;
esac

# 4) Launch the setup wizard.
# When invoked as `curl … | bash`, this script's stdin is the pipe, not your keyboard,
# so the interactive wizard must read from the controlling terminal (/dev/tty).
say "Run it later with:  $HOW run"
if [ "$NO_SETUP" -eq 0 ] && [ -e /dev/tty ] && (: </dev/tty) 2>/dev/null; then
  say "Starting setup…"
  exec "${RUN[@]}" setup </dev/tty
else
  say "Installed. Finish setup with:"
  echo "    $HOW setup"
fi
}
main "$@"; exit
