#!/bin/bash
# Reviewed root activation. No packages, schema changes, global firewall reloads
# or automatic killing of processes. Stop if an unexpected service is found.
set -euo pipefail
umask 077
if [[ $EUID != 0 ]]; then
    echo 'Run from a root shell; never send a password to chat.' >&2
    exit 1
fi
cd /opt/ingecon-control
for tool in /usr/sbin/nft /usr/sbin/ip /usr/sbin/nginx /usr/bin/python3 /usr/sbin/runuser; do
    test -x "$tool"
done
test -x .venv/bin/gunicorn
systemctl is-active --quiet tailscaled.service
test -f instance/production/web-secret.key
test -f instance/telegram/bot.key
grep -qx 'INGECON_COOKIE_SECURE=false' instance/production/portal.env
grep -qx 'INGECON_DATABASE_URI=sqlite:////opt/ingecon-control/instance/production/ingecon.sqlite' instance/production/portal.env

# Reject an unexpected port-80 listener; this guard must not affect SSH/VPN.
if /usr/bin/ss -H -ltnp '( sport = :80 )' | /usr/bin/grep -v '"nginx"' | /usr/bin/grep -q .; then
    echo 'Unexpected listener on TCP:80; nothing changed.' >&2
    exit 1
fi
if ! systemctl is-active --quiet ingecon-web.service && /usr/bin/ss -H -ltn '( sport = :5000 )' | /usr/bin/grep -q .; then
    echo 'Port 5000 is occupied; nothing stopped. Resolve the existing server first.' >&2
    exit 1
fi
# The existing service is safe to start idempotently. A manual poller is not.
# Inspect all command lines without printing tokens or sending Telegram updates.
/usr/bin/python3 -I - <<'PY'
from pathlib import Path
import subprocess
main = subprocess.run(['systemctl','show','ingecon-telegram.service','--property=MainPID','--value'],capture_output=True,text=True)
allowed = main.stdout.strip()
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal() or proc.name == allowed:
        continue
    try:
        args = (proc/'cmdline').read_bytes().split(b'\0')
    except (OSError, PermissionError):
        continue
    if (any(a.endswith(b'telegram_bot.py') for a in args) or b'telegram_bot' in args) and b'run' in args:
        raise SystemExit('Existing manual Telegram poller PID '+proc.name+'; stop it gracefully after checking its identity. No new poller started.')
PY
for site in /etc/nginx/sites-enabled/*; do
    [[ -e "$site" || -L "$site" ]] || continue
    case "$site" in
        /etc/nginx/sites-enabled/default)
            [[ -L "$site" && $(readlink -f "$site") == /etc/nginx/sites-available/default ]] || exit 1 ;;
        /etc/nginx/sites-enabled/ingecon-control.conf)
            [[ -L "$site" && $(readlink -f "$site") == /etc/nginx/sites-available/ingecon-control.conf ]] || exit 1 ;;
        *) echo 'Other Nginx sites enabled; review them manually before activation.' >&2; exit 1 ;;
    esac
done

# Backup BEFORE installing files or changing any network rules. No full restore.
backup_dir=$(mktemp -d /root/ingecon-task07-XXXXXXXX)
chmod 0700 "$backup_dir"
/usr/sbin/nft list ruleset > "$backup_dir/nft-ruleset.nft"
/usr/sbin/nft --json list ruleset > "$backup_dir/nft-ruleset.json"
cp -a /etc/nginx "$backup_dir/nginx"
for unit in ingecon-web.service ingecon-telegram.service ingecon-http-guard.service nginx.service.d; do
    if [[ -e /etc/systemd/system/$unit ]]; then
        cp -a "/etc/systemd/system/$unit" "$backup_dir/$unit"
    fi
done
if [[ -d /etc/ingecon-control ]]; then cp -a /etc/ingecon-control "$backup_dir/ingecon-control"; fi
systemctl is-active nginx tailscaled ssh ingecon-web ingecon-telegram > "$backup_dir/active-before.txt" || true
systemctl is-enabled nginx tailscaled ingecon-web ingecon-telegram > "$backup_dir/enabled-before.txt" 2>&1 || true
/usr/sbin/runuser -u codex -- .venv/bin/python -m deploy.prepare_portal
/usr/bin/python3 -I deploy/http_guard.py --render --template deploy/nftables/http-guard.nft > "$backup_dir/candidate.nft"
/usr/sbin/nft --check --file "$backup_dir/candidate.nft"
echo "Verified backups and candidate rules: $backup_dir"

install -d -m 0755 /usr/local/libexec /etc/ingecon-control /etc/systemd/system/nginx.service.d
# The privileged daemon executes a ROOT-owned copy, never app-writable code.
install -o root -g root -m 0644 deploy/http_guard.py /usr/local/libexec/ingecon-http-guard.py
install -o root -g root -m 0644 deploy/nftables/http-guard.nft /etc/ingecon-control/http-guard.nft
if [[ ! -e /etc/ingecon-control/http-guard.env ]]; then
    install -o root -g root -m 0644 deploy/http-guard.env.example /etc/ingecon-control/http-guard.env
fi
for unit in ingecon-web.service ingecon-telegram.service ingecon-http-guard.service; do
    install -o root -g root -m 0644 "deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
install -o root -g root -m 0644 deploy/systemd/nginx.service.d/ingecon.conf /etc/systemd/system/nginx.service.d/ingecon.conf
install -o root -g root -m 0644 deploy/nginx/ingecon-control.conf /etc/nginx/sites-available/ingecon-control.conf
systemctl daemon-reload
# Ready only after the guard has successfully installed a restrictive baseline.
# A reload of nftables.service is deliberately NOT performed.
systemctl restart ingecon-http-guard.service
systemctl is-active --quiet ingecon-http-guard.service
if [[ -L /etc/nginx/sites-enabled/default ]]; then unlink /etc/nginx/sites-enabled/default; fi
if [[ ! -e /etc/nginx/sites-enabled/ingecon-control.conf ]]; then
    ln -s /etc/nginx/sites-available/ingecon-control.conf /etc/nginx/sites-enabled/ingecon-control.conf
fi
/usr/sbin/nginx -t
systemctl start ingecon-web.service
# Reload preserves the existing Nginx master; start also covers a stopped unit.
if systemctl is-active --quiet nginx.service; then systemctl reload nginx.service; else systemctl start nginx.service; fi
systemctl start ingecon-telegram.service
systemctl enable ingecon-http-guard.service ingecon-web.service ingecon-telegram.service nginx.service
systemctl is-active ingecon-http-guard.service ingecon-web.service ingecon-telegram.service nginx.service tailscaled.service
systemctl is-enabled ingecon-http-guard.service ingecon-web.service ingecon-telegram.service nginx.service
echo "Activation commands finished. Perform the checks in HTTP_PORTAL.md. Backups: $backup_dir"
