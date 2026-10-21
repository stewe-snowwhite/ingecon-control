#!/bin/bash
# Explicit owner-authorized root activation; no network configuration or writes.
set -euo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run from a root shell; never send a password to chat.' >&2; exit 1; }
[[ $# -ge 2 ]] || { echo 'Usage: activate_telemetry.sh /absolute/manifest.json CONFIRMED_INVERTER_ID [...]' >&2; exit 1; }
cd /opt/ingecon-control
manifest=$1
shift
for key in "$@"; do [[ $key =~ ^[1-9][0-9]*$ ]]; done
app() { /usr/sbin/runuser -u codex -- "$@"; }
metadata=$(app .venv/bin/python - "$manifest" <<'PY'
import json,pathlib,sys
from deploy.discovery_release import git,verify_protected
p=pathlib.Path(sys.argv[1])
base=pathlib.Path('/opt/ingecon-control/instance/releases')
assert not p.is_symlink() and p.resolve().parent.parent == base and p.name == 'manifest.json'
m=json.loads(p.read_text())
assert m['version']==1 and pathlib.Path(m['directory'])==p.parent
assert git('branch','--show-current')=='main' and not git('status','--porcelain')
assert git('rev-parse','HEAD')==m['expected_sha']
assert git('ls-remote','origin','refs/heads/main').split()[0]==m['expected_sha']
verify_protected(m)
for key in ('expected_sha','previous_sha','backup_dir','previous_source'):
 value=m[key]
 assert isinstance(value,str) and '\n' not in value
 if key.endswith('_sha'): assert len(value)==40 and all(c in '0123456789abcdef' for c in value)
 else: assert pathlib.Path(value).resolve().is_relative_to('/opt/ingecon-control/instance')
 print(value)
PY
)
readarray -t fields <<< "$metadata"
expected=${fields[0]}
previous=${fields[1]}
backup=${fields[2]}
source_snapshot=${fields[3]}
migrate_args=()
for key in "$@"; do migrate_args+=(--confirm-inverter-id "$key"); done
app .venv/bin/python - "$manifest" "$@" <<'PY'
import json,pathlib,sys
from deploy.telemetry_release import verify_targets
verify_targets(json.loads(pathlib.Path(sys.argv[1]).read_text()), [int(k) for k in sys.argv[2:]])
PY
for name in ingecon-web ingecon-monitor ingecon-discovery ingecon-telegram nginx; do systemctl is-active --quiet "$name"; done
for name in ingecon-web ingecon-monitor; do
    [[ ! -e /etc/systemd/system/$name.service.d/91-telemetry-rollback.conf ]]
done
[[ ! -e /etc/systemd/system/ingecon-web.service.d/90-discovery-rollback.conf ]]
# Backup config/effective state before stopping anything. Existing drop-ins untouched.
state=$backup/telemetry-systemd
mkdir -m 0700 "$state"
for name in ingecon-web ingecon-monitor; do
    cp -a "/etc/systemd/system/$name.service" "$state/$name.service"
    if [[ -d /etc/systemd/system/$name.service.d ]]; then cp -a "/etc/systemd/system/$name.service.d" "$state/$name.service.d"; fi
    systemctl cat "$name" > "$state/$name.effective"
    systemctl show "$name" --property=ActiveState,UnitFileState,MainPID,ExecStart,WorkingDirectory > "$state/$name.state"
done
app .venv/bin/python - "$manifest" <<'PY'
import json,pathlib,sqlite3,sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text())
for p in (pathlib.Path(m['database_backup']),pathlib.Path(m['backup_dir'])/'restore-verified.sqlite'):
 with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c:
  assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
assert (pathlib.Path(m['previous_source'])/'monitor.py').is_file()
PY
changed=0
activation_started=$(date --iso-8601=seconds)
rollback() {
    result=$?
    if [[ $result == 0 ]]; then result=1; fi
    trap - ERR INT TERM
    set +e
    if [[ $changed == 1 ]]; then
        rollback_ok=1
        systemctl stop ingecon-web ingecon-monitor || rollback_ok=0
        for name in ingecon-web ingecon-monitor; do install -d -m 0755 "/etc/systemd/system/$name.service.d" || rollback_ok=0; done
        cat > /etc/systemd/system/ingecon-web.service.d/91-telemetry-rollback.conf <<ROLLBACK
[Service]
WorkingDirectory=$source_snapshot
Environment=INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
Environment=INGECON_MODBUS_CONFIG_FILE=/opt/ingecon-control/instance/production/modbus.json
ExecStart=
ExecStart=/opt/ingecon-control/.venv/bin/gunicorn --config $source_snapshot/deploy/gunicorn.conf.py portal_wsgi:app
ROLLBACK
        [[ $? == 0 ]] || rollback_ok=0
        cat > /etc/systemd/system/ingecon-monitor.service.d/91-telemetry-rollback.conf <<ROLLBACK
[Service]
WorkingDirectory=$source_snapshot
Environment=INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
ExecStart=
ExecStart=/opt/ingecon-control/.venv/bin/python -m monitor --database-uri sqlite:////opt/ingecon-control/instance/production/ingecon.sqlite --interval 30
ROLLBACK
        [[ $? == 0 ]] || rollback_ok=0
        systemctl daemon-reload || rollback_ok=0
        systemctl start ingecon-monitor ingecon-web || rollback_ok=0
        app .venv/bin/python - <<'PY' || rollback_ok=0
from deploy.check_portal import http_check
http_check('http://127.0.0.1:5000')
http_check('http://127.0.0.1:80')
PY
        systemctl is-active ingecon-web ingecon-monitor ingecon-telegram ingecon-discovery nginx || rollback_ok=0
        if [[ $rollback_ok == 1 ]]; then
            echo "ROLLED_BACK to source $previous; additive columns retained; no older DB restored." >&2
        else
            echo "ROLLBACK_FAILED: inspect service state and diagnostics in $backup; no older DB restored." >&2
        fi
    fi
    exit "${result:-1}"
}
trap rollback ERR INT TERM
changed=1
systemctl stop ingecon-web ingecon-monitor
# Recheck after stopping writers, before accepting the IDs for migration.
app .venv/bin/python - "$manifest" "$@" <<'PY'
import json,pathlib,sys
from deploy.telemetry_release import verify_targets
verify_targets(json.loads(pathlib.Path(sys.argv[1]).read_text()), [int(k) for k in sys.argv[2:]])
PY
# Verified immediate online backup and transactional additive DDL; no DROP or data reset.
app .venv/bin/python -m telemetry_schema --database /opt/ingecon-control/instance/production/ingecon.sqlite --backup-dir "$backup" "${migrate_args[@]}"
systemctl start ingecon-monitor ingecon-web
ready=0
for attempt in {1..20}; do
    if app .venv/bin/python -m deploy.telemetry_release --manifest "$manifest" "${migrate_args[@]}"; then ready=1; break; fi
    sleep 5
done
[[ $ready == 1 ]]
for name in ingecon-web ingecon-monitor ingecon-discovery ingecon-telegram nginx; do systemctl is-active --quiet "$name"; done
journalctl -q -u ingecon-web -u ingecon-monitor --since "$activation_started" -p err --no-pager > "$backup/telemetry-activation-errors.log"
[[ ! -s "$backup/telemetry-activation-errors.log" ]]
trap - ERR INT TERM
printf 'TELEMETRY_DEPLOYED commit=%s backup=%s\n' "$expected" "$backup"
printf 'No scan queued, no inverter writes, no Telegram restart, no network configuration changes.\n'
