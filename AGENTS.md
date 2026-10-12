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

