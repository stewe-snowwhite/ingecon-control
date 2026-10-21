#!/bin/bash
# Explicit root activation. No firewall, routes, SSH, Tailscale or Telegram restart.
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run from a root shell; do not send a password to chat.' >&2; exit 1; }
[[ $# == 1 ]] || { echo 'Usage: bash deploy/activate_discovery.sh /absolute/manifest.json' >&2; exit 1; }
cd /opt/ingecon-control
manifest=$1
metadata=$(/usr/bin/python3 -I - "$manifest" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
base = pathlib.Path('/opt/ingecon-control/instance/releases')
if p.is_symlink() or p.resolve().parent.parent != base or p.name != 'manifest.json':
    raise SystemExit('Unexpected manifest path')
data = json.loads(p.read_text())
if data['version'] != 1 or pathlib.Path(data['directory']) != p.parent:
    raise SystemExit('Unexpected manifest')
for key in ('expected_sha','previous_sha','backup_dir','previous_source'):
    value = data[key]
    if not isinstance(value,str) or '\n' in value:
        raise SystemExit('Invalid manifest value')
    if key.endswith('_sha') and (len(value)!=40 or any(c not in '0123456789abcdef' for c in value)):
        raise SystemExit('Invalid SHA')
    if key in ('backup_dir','previous_source') and not pathlib.Path(value).is_relative_to('/opt/ingecon-control/instance'):
        raise SystemExit('Unexpected runtime path')
    print(value)
PY
)
readarray -t fields <<< "$metadata"
expected=${fields[0]}
previous=${fields[1]}
backup=${fields[2]}
source_snapshot=${fields[3]}
app() { /usr/sbin/runuser -u codex -- "$@"; }
[[ $(app git rev-parse HEAD) == "$expected" ]]
[[ -z $(app git status --porcelain) ]]
remote=$(app git ls-remote origin refs/heads/feature/modernization)
[[ ${remote%%$'\t'*} == "$expected" ]]
# Validate the backup by restoring to a fresh private file; no production overwrite.
app .venv/bin/python - "$manifest" <<'PY'
import json, pathlib, sqlite3, sys
from deploy.discovery_release import verify_protected
m=json.loads(pathlib.Path(sys.argv[1]).read_text())
verify_protected(m)
with sqlite3.connect(pathlib.Path(m['database_backup']).as_uri()+'?mode=ro',uri=True) as c:
    assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
with sqlite3.connect('file:/opt/ingecon-control/instance/production/ingecon.sqlite?mode=ro',uri=True) as c:
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'discovery_scan' in tables:
        assert c.execute("SELECT count(*) FROM discovery_scan WHERE state IN ('queued','running','cancelling')").fetchone()[0]==0, 'Pending scan; resolve it manually before activation'
PY
for service in ingecon-web ingecon-telegram nginx; do systemctl is-active --quiet "$service"; done
# Do not replace an existing unrelated monitor/discovery configuration.
for name in ingecon-monitor ingecon-discovery; do
    if [[ -e /etc/systemd/system/$name.service ]]; then
        cmp /etc/systemd/system/$name.service "deploy/systemd/$name.service"
    fi
done
systemd-analyze verify deploy/systemd/ingecon-web.service deploy/systemd/ingecon-monitor.service deploy/systemd/ingecon-discovery.service
state=$backup/systemd
mkdir -m 0700 "$state"
for name in ingecon-web ingecon-monitor ingecon-discovery; do
    if [[ -e /etc/systemd/system/$name.service || -L /etc/systemd/system/$name.service ]]; then
        cp -a "/etc/systemd/system/$name.service" "$state/$name.service"
    fi
    if [[ -d /etc/systemd/system/$name.service.d ]]; then
        cp -a "/etc/systemd/system/$name.service.d" "$state/$name.service.d"
    fi
    systemctl is-active "$name" > "$state/$name.active" || true
    systemctl is-enabled "$name" > "$state/$name.enabled" 2>/dev/null || true
    systemctl cat "$name" > "$state/$name.effective" 2>/dev/null || true
done
[[ ! -e /etc/systemd/system/ingecon-web.service.d/90-discovery-rollback.conf ]]
changed=0
activation_started=$(date --iso-8601=seconds)
rollback() {
    result=$?
    if [[ $result == 0 ]]; then result=1; fi
    trap - ERR INT TERM
    set +e
    if [[ $changed == 1 ]]; then
        for name in ingecon-discovery ingecon-monitor; do
            if ! grep -qx active "$state/$name.active"; then systemctl stop "$name"; fi
        done
        for name in ingecon-web ingecon-monitor ingecon-discovery; do
            if [[ -e $state/$name.service || -L $state/$name.service ]]; then
                cp -a "$state/$name.service" "/etc/systemd/system/$name.service"
            else
                # Only remove a new unit installed by this activation, never runtime data.
                if cmp -s "/etc/systemd/system/$name.service" "deploy/systemd/$name.service"; then
                    unlink "/etc/systemd/system/$name.service"
                fi
            fi
        done
        # Serve the previous committed source from its snapshot; keep the Git tree intact.
        install -d -m 0755 /etc/systemd/system/ingecon-web.service.d
        cat > /etc/systemd/system/ingecon-web.service.d/90-discovery-rollback.conf <<ROLLBACK
[Service]
WorkingDirectory=$source_snapshot
Environment=INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
Environment=INGECON_MODBUS_CONFIG_FILE=/opt/ingecon-control/instance/production/modbus.json
ExecStart=
ExecStart=/opt/ingecon-control/.venv/bin/gunicorn --config $source_snapshot/deploy/gunicorn.conf.py portal_wsgi:app
ROLLBACK
        systemctl daemon-reload
        for name in ingecon-web ingecon-monitor ingecon-discovery; do
            if grep -qx enabled "$state/$name.enabled"; then systemctl enable "$name"; else systemctl disable "$name"; fi
        done
        systemctl restart ingecon-web
        if grep -qx active "$state/ingecon-monitor.active"; then systemctl restart ingecon-monitor; fi
        if grep -qx active "$state/ingecon-discovery.active"; then systemctl restart ingecon-discovery; fi
        app .venv/bin/python - <<'PY'
from deploy.check_portal import http_check
http_check('http://127.0.0.1:5000')
http_check('http://127.0.0.1:80')
PY
        systemctl is-active ingecon-web ingecon-telegram nginx
        echo "ROLLBACK: previous source $previous; service snapshots: $state" >&2
        echo 'Additive tables retained for compatibility; production DB and new operational data were not overwritten.' >&2
    fi
    exit "${result:-1}"
}
trap rollback ERR INT TERM
changed=1
# This command makes and verifies a second, immediate backup before additive DDL.
app .venv/bin/python -m discovery_schema --database /opt/ingecon-control/instance/production/ingecon.sqlite --backup-dir "$backup"
for name in ingecon-web ingecon-monitor ingecon-discovery; do
    install -o root -g root -m 0644 "deploy/systemd/$name.service" "/etc/systemd/system/$name.service"
done
systemctl daemon-reload
systemctl enable ingecon-web ingecon-monitor ingecon-discovery
systemctl start ingecon-monitor ingecon-discovery
systemctl restart ingecon-web
# Readiness: wait at most 45s. HTTP GETs and SQLite heartbeat reads only.
ready=0
for attempt in {1..15}; do
    if app .venv/bin/python -m deploy.discovery_release check --manifest "$manifest"; then ready=1; break; fi
    sleep 3
done
[[ $ready == 1 ]]
for name in ingecon-web ingecon-monitor ingecon-discovery ingecon-telegram; do
    systemctl is-active --quiet "$name"
    systemctl is-enabled --quiet "$name"
done
# Capture errors locally; never print unrestricted journal text that might include secrets.
journalctl -q -u ingecon-web -u ingecon-monitor -u ingecon-discovery --since "$activation_started" -p err --no-pager > "$backup/activation-errors.log"
[[ ! -s "$backup/activation-errors.log" ]]
trap - ERR INT TERM
printf 'DEPLOYMENT_CHECKED commit=%s backup=%s\n' "$expected" "$backup"
printf 'No scan was queued. Publish the production release tag after reviewing these checks.\n'
