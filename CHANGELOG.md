# Changelog

## 1.2.2 – 2026-10-19

- Setup and connect optionally register each started bridge with Agents Monitoring
  0.3.2+ through its public CLI. Config, interpreter, import path and state are
  preserved; repeated registration updates the same entry and never copies tokens.
  Missing monitoring is informational; registration failures leave the bridge running
  and print a warning. A process that exits during startup is not registered.
- Keep provider menu numbers stable, require an explicit choice, find common user-bin
  installations, and confirm the absolute CLI before creating a new tmux session.
- Refresh checked-hash bytecode during installation so same-second upgrades cannot
  import stale code even when optional pip installation is skipped.

## 1.2.1 – 2026-10-18

- Claude attach startup and setup share atomic, idempotent Stop-hook registration, honour
  `CLAUDE_CONFIG_DIR`, and preserve recognised custom marker hooks. Doctor checks hook
  coverage; `claude-hook --config ...` repairs it without starting another bridge.
  Resume the same Claude session to load a newly registered hook. Unconfirmed hooks no
  longer cause stale busy acknowledgements/typing after 90 seconds of transcript silence;
  late-answer delivery and cross-chat ownership remain unchanged.
- Default sender labels, comments, documentation, and operational logs are English.
  Explicit `language: "cs"` retains the existing Czech labels and notices.
- Public-language regression checks also work in source archives without Git metadata.
- Installer retries preserve existing Git checkouts on failure. Archive updates recognise
  older unmarked installations and keep the previous source in a neighbouring backup.
  Unknown directories and symlink targets are refused. Added `--no-setup` and `--no-pip`.
- Setup stops cleanly at end of input. New wizard configurations record an explicit
  state directory, so multiple bots have separate offsets and queues. Existing configs
  keep their previous state paths; `AGENT2TELEGRAM_STATE` still takes precedence.
- Update disables Git credential prompts, checks for local changes, and retries failed pulls.
  `update --no-restart` supports supervisor-managed bridges. Automatic restarts preserve
  each process's environment on Linux. Restart planning happens before pull; macOS supports
  PID-matched launchd kickstart and standalone file-backed config/state. Unknown environments
  keep running, with a successful source update and an explicit manual-restart warning.
  Restore missing bot username backfill after pull, before restart. Agent tmux sessions are
  never restarted.
- Doctor fails when no user is authorized. Uninstall limits process selection to the
  current user's bridge processes and leaves tmux agents running. Service exports include
  the source import path and an explicitly selected state directory.
- Package metadata and the CLI report the same version.
- Prevent concurrent inbox recovery and live polling from injecting the same durable
  message twice, including the case where recovery completes before the live submit.
