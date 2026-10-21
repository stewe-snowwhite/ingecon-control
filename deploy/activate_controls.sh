#!/bin/bash
# Owner-authorized activation. Only web/bot restart; no inverter traffic or DDL.
set -euo pipefail
umask 077
[[ $EUID == 0 && $# == 1 ]] || { echo 'Run as root: bash deploy/activate_controls.sh /absolute/manifest.json' >&2; exit 1; }
cd /opt/ingecon-control
exec 9>/run/ingecon-controls-deploy.lock
flock -n 9 || { echo 'Another controls deployment is running.' >&2; exit 1; }
manifest=$1
app() { /usr/sbin/runuser -u codex -- "$@"; }
metadata=$(app .venv/bin/python - "$manifest" <<'PY'
import json,pathlib,sys,sqlite3
from deploy.discovery_release import git,verify_protected
from inverter_controls import policy_path,load_policy
p=pathlib.Path(sys.argv[1])
assert not p.is_symlink() and p.resolve().parent.parent==pathlib.Path('/opt/ingecon-control/instance/releases')
m=json.loads(p.read_text())
assert p.name=='manifest.json' and m['version']==1 and pathlib.Path(m['directory'])==p.parent
assert git('branch','--show-current')=='main' and not git('status','--porcelain')
assert git('rev-parse','HEAD')==git('ls-remote','origin','refs/heads/main').split()[0]==m['expected_sha']
verify_protected(m)
assert policy_path()==pathlib.Path('/opt/ingecon-control/instance/production/inverter-controls.json')
assert load_policy() is None, 'Existing authorization requires separate review; do not overwrite'
for key in ('backup_dir','previous_source'):
 value=m[key]
 assert '\n' not in value and pathlib.Path(value).resolve().is_relative_to('/opt/ingecon-control/instance')
 print(value)
for name in ('production.sqlite','restore-verified.sqlite'):
 with sqlite3.connect((pathlib.Path(m['backup_dir'])/name).as_uri()+'?mode=ro',uri=True) as c:
  assert c.execute('PRAGMA integrity_check').fetchone()==('ok',)
PY
)
readarray -t fields <<< "$metadata"
backup=${fields[0]}
source_snapshot=${fields[1]}
state=$backup/controls-systemd
mkdir -m 0700 "$state"
for name in ingecon-web ingecon-telegram; do
    systemctl is-active --quiet "$name"
    [[ -z $(systemctl show "$name" --property=DropInPaths --value) ]]
    [[ $(systemctl show "$name" --property=WorkingDirectory --value) == /opt/ingecon-control ]]
    cp -a "/etc/systemd/system/$name.service" "$state/$name.service"
    systemctl show "$name" --property=MainPID,ActiveState,ExecStart,WorkingDirectory > "$state/$name.state"
done
changed=0
rollback() {
    code=$?
    if [[ $code == 0 ]]; then code=1; fi
    trap - ERR INT TERM
    set +e
    if [[ $changed == 1 ]]; then
        ok=1
        app .venv/bin/python -m inverter_controls --disable || ok=0
        systemctl stop ingecon-web ingecon-telegram || ok=0
        for name in ingecon-web ingecon-telegram; do install -d -m 0755 "/etc/systemd/system/$name.service.d" || ok=0; done
        cat > /etc/systemd/system/ingecon-web.service.d/92-controls-rollback.conf <<ROLLBACK
[Service]
WorkingDirectory=$source_snapshot
Environment=INGECON_MODBUS_CONFIG_FILE=/opt/ingecon-control/instance/production/modbus.json
Environment=INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
ExecStart=
ExecStart=/opt/ingecon-control/.venv/bin/gunicorn --config $source_snapshot/deploy/gunicorn.conf.py portal_wsgi:app
ROLLBACK
        [[ $? == 0 ]] || ok=0
        cat > /etc/systemd/system/ingecon-telegram.service.d/92-controls-rollback.conf <<ROLLBACK
[Service]
WorkingDirectory=$source_snapshot
Environment=INGECON_MODBUS_CONFIG_FILE=/opt/ingecon-control/instance/production/modbus.json
Environment=INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
ExecStart=
ExecStart=/opt/ingecon-control/.venv/bin/python $source_snapshot/telegram_bot.py run --database /opt/ingecon-control/instance/production/ingecon.sqlite --token-file /opt/ingecon-control/instance/telegram/bot.key
ROLLBACK
        [[ $? == 0 ]] || ok=0
        systemctl daemon-reload || ok=0
        systemctl start ingecon-web ingecon-telegram || ok=0
        app .venv/bin/python -c "from deploy.check_portal import http_check; http_check('http://127.0.0.1:5000'); http_check('http://127.0.0.1:80')" || ok=0
        systemctl is-active ingecon-web ingecon-telegram ingecon-monitor ingecon-discovery || ok=0
        if [[ $ok == 1 ]]; then echo "ROLLED_BACK: previous source restored; authorization disabled; SQLite not restored." >&2
        else echo "ROLLBACK_FAILED: inspect $state locally; no older DB restored." >&2; fi
    fi
    exit "${code:-1}"
}
trap rollback ERR INT TERM
changed=1
# This authorization file is the only new runtime configuration; no secrets change.
app .venv/bin/python -m inverter_controls --enable-all-100tl --accept-unverified-firmware
systemctl restart ingecon-web ingecon-telegram
ready=0
for attempt in {1..15}; do
    if app .venv/bin/python -m deploy.control_release --manifest "$manifest"; then ready=1; break; fi
    sleep 2
done
[[ $ready == 1 ]]
systemctl is-active --quiet ingecon-web ingecon-telegram ingecon-monitor ingecon-discovery
trap - ERR INT TERM
echo "CONTROLS_ACTIVATED: all registered 100TL; unverified firmware acknowledged; backup=$backup"
echo 'No inverter command was sent. Monitoring/discovery/network settings/secrets unchanged.'
