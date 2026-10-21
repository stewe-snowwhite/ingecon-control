# ingecon-control — Agent Instructions

## Project
Modernize the existing Ingeteam inverter management application.

Target hardware:
- Dell Wyse 3040
- Debian 13 x86_64
- Intel Atom x5-Z8350
- 2 GB RAM
- 8 GB eMMC

## Architecture
- Prefer Python, Flask, SQLite and systemd.
- Reuse existing working Modbus logic.
- Support multiple inverter manufacturers through adapters.
- Keep memory usage and disk writes low.
- Do not introduce Docker unless explicitly requested.

## Required Features
1. Responsive web dashboard.
2. Inverter availability monitoring.
3. Background monitoring service.
4. Telegram bot for status and commands.
5. Start/stop inverter controls.
6. Event history and audit logging.
7. Authentication and role-based access.
8. Nginx and Fail2ban integration.

## Critical Safety Rules
- Never send real Modbus write commands during development or testing.
- Never start or stop physical inverters without explicit approval.
- Never assume Modbus registers are correct for all inverter models.
- Use mocks for all control-command tests.
- Do not expose the application publicly without authentication.
- Do not store secrets in source code.
- Do not run destructive system commands.
- Do not modify firewall, SSH or network configuration without approval.
- Do not run production migrations without a backup.
- Do not commit credentials, databases or logs.
- Ask before installing system packages or modifying systemd services.

## Development Workflow
- Inspect existing code before making changes.
- Preserve existing functionality.
- Make small, testable changes.
- Run tests after modifications.
- Explain changes and risks.
- Use Ukrainian for progress reports and explanations.


## Mandatory Git and Production Release Policy
- Before every change, inspect `git status`, the current branch and synchronization with GitHub. Fetch remote refs without overwriting local work.
- Preserve all uncommitted changes; do not discard or overwrite another person's work.
- The only working branch is `main`. Do not create additional branches without explicit user permission. Retain explicitly authorized backup refs until their removal is authorized.
- Commit every completed logical change to code, application configuration, migrations, documentation or tests with a clear message.
- After successful checks and at the end of every completed task, run `git push origin main`. If authentication or another blocker prevents publication, report it explicitly; never claim unpublished changes are synchronized. This policy supersedes earlier task-specific instructions to make local-only commits.
- Before production deployment, verify that the exact intended commit is available in GitHub's `main`. Do not deploy unpublished commits.
- After successful production validation, create an annotated production release tag and push that tag to GitHub. Do not label an unverified deployment as a successful release.
- Every final report must state branch, full commit SHA, production release tag (or why none exists), and the push result.
- Never force push or rewrite Git history. Do not delete remote branches without separate explicit authorization.
- Never commit `.env` files, credentials, passwords, tokens, private keys, SQLite databases, logs, backups or other runtime secrets.
- Production migrations require a verified SQLite backup. Preserve a source/configuration snapshot and an automatic rollback path before activation; do not restore an older database over newer operational data.
