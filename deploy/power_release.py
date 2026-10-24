"""Snapshot and read-only checks for owner-approved power-controller activation."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from deploy import discovery_release as base

ROOT=base.ROOT
DATABASE=base.DATABASE


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def inventory_snapshot():
    import power_budget
    from models import User, TelegramLink
    engine=create_engine('sqlite:///file:'+str(DATABASE)+'?mode=ro&uri=true')
    try:
        with Session(engine) as session:
            targets=power_budget.inventory(session)
            users=[list(row) for row in session.execute(select(User.id,User.username,User.password_hash,User.role,User.active).order_by(User.id))]
            links=[list(row) for row in session.execute(select(TelegramLink.telegram_id,TelegramLink.user_id).order_by(TelegramLink.telegram_id))]
        return dict(count=len(targets),groups=dict(Counter(row['group'] for row in targets)),
                    fingerprint=fingerprint(targets),accounts=fingerprint(users),links=fingerprint(links))
    finally:engine.dispose()


def prepare(expected,previous):
    import power_budget
    import inverter_controls
    assert inverter_controls.enabled(),'Existing owner authorization required'
    config=power_budget.load()
    if config and config['enabled']:
        raise ValueError('Disable the active budget explicitly before replacing its controller')
    before=inventory_snapshot()
    path=base.prepare(expected,previous)
    manifest=json.loads(path.read_text())
    absent=[]
    for relative in ('instance/production/inverter-controls.json','instance/production/monitor-schedule.json',
                     'instance/production/power-budget.json'):
        source=ROOT/relative
        if not source.exists():
            assert not source.is_symlink();absent.append(relative);continue
        assert source.is_file() and not source.is_symlink()
        data=source.read_bytes()
        destination=Path(manifest['backup_dir'])/relative.replace('/','__')
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        assert destination.read_bytes()==data
        manifest['protected'][relative]=hashlib.sha256(data).hexdigest()
    assert before==inventory_snapshot(),'Fleet/accounts changed while preparing the backup'
    verified={str(file):hashlib.sha256(file.read_bytes()).hexdigest() for file in (
        Path(manifest['database_backup']),Path(manifest['backup_dir'])/'restore-verified.sqlite')}
    manifest['power_release']=dict(inventory=before,absent=absent,monitor_workers=8,
                                  controller_writes=True,migrations=False,backup_checksums=verified)
    # Existing private manifest stays owned by the application user.
    with path.open('w') as stream:
        json.dump(manifest,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    return path


def read_manifest(path):
    path=Path(path)
    assert not path.is_symlink() and path.resolve().parent.parent==ROOT/'instance/releases'
    assert path.name=='manifest.json'
    manifest=json.loads(path.read_text())
    assert manifest['version']==1 and manifest['directory']==str(path.parent)
    assert manifest['power_release']['controller_writes'] is True
    assert manifest['power_release']['monitor_workers']==8
    return manifest


def validate_instances(telegram_pids,telegram_main,controller_pids,controller_main):
    assert telegram_main!='0' and telegram_pids==[telegram_main],'Expected one existing Telegram poller'
    assert controller_pids==([controller_main] if controller_main!='0' else []),'Unexpected standalone/duplicate controller'


def check_instances():
    found={'telegram':[],'controller':[]}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():continue
        try:args=(path/'cmdline').read_bytes().split(b'\0')
        except OSError:continue
        if (b'telegram_bot' in args or any(a.endswith(b'telegram_bot.py') for a in args)) and b'run' in args:
            found['telegram'].append(path.name)
        if b'power_controller' in args or any(a.endswith(b'power_controller.py') for a in args):
            found['controller'].append(path.name)
    def main(service):
        result=subprocess.run(['systemctl','show',service,'--property=MainPID','--value'],
                              capture_output=True,text=True,timeout=10)
        return result.stdout.strip() or '0'
    validate_instances(found['telegram'],main('ingecon-telegram'),found['controller'],main('ingecon-power-controller'))


def guard(path):
    import inverter_controls
    import power_budget
    from modbus_safety import writes_locked
    manifest=read_manifest(path)
    assert base.git('branch','--show-current')=='main' and not base.git('status','--porcelain')
    assert base.git('rev-parse','HEAD')==manifest['expected_sha']
    assert base.git('ls-remote','origin','refs/heads/main').split()[0]==manifest['expected_sha']
    for filename,checksum in manifest['power_release']['backup_checksums'].items():
        saved=Path(filename)
        assert not saved.is_symlink() and saved.resolve().parent==Path(manifest['backup_dir']).resolve()
        assert hashlib.sha256(saved.read_bytes()).hexdigest()==checksum,'Verified backup changed'
    base.verify_protected(manifest)
    for relative in manifest['power_release']['absent']:
        assert not (ROOT/relative).exists() and not (ROOT/relative).is_symlink()
    assert inventory_snapshot()==manifest['power_release']['inventory']
    assert inverter_controls.enabled() and writes_locked(),'Keep legacy/raw writes locked'
    config=power_budget.load()
    assert not config or not config['enabled'],'Do not introduce a generation target during deployment'
    assert (Path(manifest['previous_source'])/'app.py').is_file()
    check_instances()
    return manifest


def validate_snapshot(data,expected_count):
    assert data['counts']['total']==expected_count,'Registered fleet differs from backed-up fleet'
    assert len(data['inverters'])==expected_count
    assert data['monitor']['alive'],'Monitor heartbeat missing'


def check(path):
    """No inverter requests, control POSTs, Telegram messages or polling instances."""
    from app import create_app
    from configuration import read_secret_file
    from deploy.check_portal import http_check, telegram_check
    from models import User
    manifest=guard(path)
    engine=create_engine('sqlite:///file:'+str(DATABASE)+'?mode=ro&uri=true')
    try:
        with Session(engine) as session:
            user=session.scalar(select(User).where(User.role=='Administrator',User.active.is_(True)))
            assert user is not None
            payload=dict(user_id=user.id,session_version=user.session_version)
        application=create_app(dict(SECRET_KEY=read_secret_file(ROOT/'instance/production/web-secret.key'),
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',SESSION_COOKIE_SECURE=False))
        cookie=application.session_interface.get_signing_serializer(application).dumps(payload)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for origin in ('http://127.0.0.1:5000','http://127.0.0.1:80'):
            http_check(origin)
            for route,marker in (('/power-budget','data-status-url'),('/add','Автоматичний пошук'),
                                 ('/settings/telegram','Telegram')):
                query=urllib.request.Request(origin+route,headers={'Cookie':'session='+cookie})
                with opener.open(query,timeout=10) as response:
                    assert response.status==200 and marker in response.read().decode()
            for route in ('/api/statuses','/power-budget/status'):
                query=urllib.request.Request(origin+route,headers={'Cookie':'session='+cookie})
                with opener.open(query,timeout=10) as response:data=json.load(response)
                if route=='/api/statuses':validate_snapshot(data,manifest['power_release']['inventory']['count'])
                else:assert data['enabled'] is False,'An operator target was not supplied during deployment'
        telegram_check()
        return dict(result='READY',sha=manifest['expected_sha'],inventory=manifest['power_release']['inventory']['count'])
    finally:engine.dispose()


def release_tag(path):
    # Called only after root activation has completed all production checks.
    manifest=guard(path)
    check(path)
    tag='production-power-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+manifest['expected_sha'][:7]
    base.git('-c','user.name=Codex','-c','user.email=codex@localhost','tag','-a',tag,
             manifest['expected_sha'],'-m','Activate dynamic fleet power controller with owner-approved writes')
    base.git('push','origin','refs/tags/'+tag)
    return tag


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','guard','check','tag'))
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--expected-sha');parser.add_argument('--previous-sha')
    args=parser.parse_args()
    try:
        if args.action=='prepare':print(prepare(args.expected_sha,args.previous_sha))
        elif args.action=='guard':print(json.dumps(guard(args.manifest)))
        elif args.action=='check':print(json.dumps(check(args.manifest)))
        else:print(release_tag(args.manifest))
    except Exception:
        raise SystemExit('Power release check failed; credentials omitted. Inspect private deployment diagnostics.') from None


if __name__=='__main__':main()
