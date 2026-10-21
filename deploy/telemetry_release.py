"""Read-only readiness checks for migrated 100TL telemetry. No inverter I/O."""
import argparse
import json
from pathlib import Path
import sqlite3
import urllib.request

from deploy.discovery_release import ROOT, DATABASE, check, verify_protected, private_json


def verify_targets(manifest, expected):
    """Bind confirmation to the backed-up endpoint, not a possibly reused ID."""
    def endpoints(path):
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as connection:
            return {r[0]:r[1:] for r in connection.execute('''
                SELECT i.id,i.ip_address,COALESCE(c.port,502),COALESCE(c.device_id,1)
                FROM inverter i LEFT JOIN monitor_config c ON c.inverter_id=i.id''')}
    saved = endpoints(Path(manifest['database_backup']))
    current = endpoints(DATABASE)
    if any(key not in saved or current.get(key) != saved[key] for key in expected):
        raise ValueError('Confirmed inverter identity/transport changed since backup')


def validate_rows(rows, expected):
    by_id = {row['id']: row for row in rows}
    for key in expected:
        row = by_id.get(key)
        if not row or row.get('model') != 'ingecon_sun_100tl' or row.get('stale') or row['state'] != 'Online':
            raise ValueError('Confirmed 100TL has no fresh Online sample yet')
        if row['checks']['modbus']['ok'] is not True:
            raise ValueError('No confirmed Modbus sample')
        raw = row.get('active_power_raw')
        if type(raw) is not int or not 0 <= raw <= 65535:
            raise ValueError('Missing raw active power')
        watts = (raw-65536 if raw >= 32768 else raw)*10
        if row.get('active_power_w') != watts:
            raise ValueError('Incorrect active power scale')
        for name in ('operating_state_code', 'limit_reason_code'):
            if type(row.get(name)) is not int:
                raise ValueError('Missing 100TL telemetry')
        if row.get('power_limit_percent') is None or not 0 <= row['power_limit_percent'] <= 100:
            raise ValueError('Missing power limit')
        if row['operating_state_code'] == 2 and row.get('operating_state_label') != 'Підключений до мережі':
            raise ValueError('Operating state confused with availability')


def validate(manifest_path, expected):
    from configuration import read_secret_file
    from app import create_app
    manifest = json.loads(manifest_path.read_text())
    verify_protected(manifest)
    verify_targets(manifest, expected)
    check(manifest)
    # Sign only a temporary existing administrator session; never change its DB row.
    application = create_app(dict(SECRET_KEY=read_secret_file(ROOT/'instance/production/web-secret.key'),
                                 SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SESSION_COOKIE_SECURE=False))
    with sqlite3.connect(DATABASE.as_uri()+'?mode=ro', uri=True) as connection:
        row = connection.execute("SELECT id,session_version FROM user WHERE role='Administrator' AND active=1 LIMIT 1").fetchone()
    if not row:
        raise ValueError('No existing administrator')
    cookie = application.session_interface.get_signing_serializer(application).dumps(dict(user_id=row[0], session_version=row[1]))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    samples = []
    for base in ('http://127.0.0.1:5000', 'http://127.0.0.1:80'):
        query = urllib.request.Request(base+'/api/statuses', headers={'Cookie':'session='+cookie})
        with opener.open(query, timeout=10) as response:
            data = json.load(response)
        validate_rows(data['inverters'], expected)
        query = urllib.request.Request(base+'/', headers={'Cookie':'session='+cookie})
        with opener.open(query, timeout=10) as response:
            html = response.read().decode()
        if not all(text in html for text in ('Доступність', 'Робочий стан', 'Активна потужність', 'Причина обмеження')):
            raise ValueError('Telemetry columns are missing in live HTML')
        samples = [{k:r.get(k) for k in ('id','model','state','checked_at','operating_state_code','active_power_raw','active_power_w','power_limit_percent','limit_reason_code')}
                   for r in data['inverters'] if r['id'] in expected]
    private_json(Path(manifest['backup_dir'])/'telemetry-validated.json',
                 dict(commit=manifest['expected_sha'], samples=samples))
    print('Validated fresh 100TL telemetry in SQLite/API and live HTML; writes remain blocked.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--confirm-inverter-id', type=int, action='append', required=True)
    args = parser.parse_args()
    validate(args.manifest, args.confirm_inverter_id)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Telemetry readiness not confirmed; no credentials printed.') from None
