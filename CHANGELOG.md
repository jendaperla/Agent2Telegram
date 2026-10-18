# Changelog

## 1.2.1 – 2026-10-18

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
  each process's environment on Linux; platforms without readable process environments
  require supervisor-managed restarts. Agent tmux sessions are never restarted.
- Doctor fails when no user is authorized. Uninstall limits process selection to the
  current user's bridge processes and leaves tmux agents running. Service exports include
  the source import path and an explicitly selected state directory.
- Package metadata and the CLI report the same version.
- Prevent concurrent inbox recovery and live polling from injecting the same durable
  message twice, including the case where recovery completes before the live submit.
