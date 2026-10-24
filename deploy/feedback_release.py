"""Controlled upgrade of an existing controller; no test/control requests are sent."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import time
import urllib.request

from deploy import discovery_release as base
from deploy.power_activate import atomic
from deploy.power_release import inventory_snapshot, check_instances

ROOT=base.ROOT
SERVICES=('ingecon-power-controller','ingecon-web','ingecon-telegram')
UNTOUCHED=('ingecon-monitor','ingecon-discovery','nginx','tailscaled')


def digest(data):return hashlib.sha256(data).hexdigest()


def context(config):
    # Controller state changes every cycle. Goal/actor/device identity must not drift.
    return digest(json.dumps({k:v for k,v in config.items() if k not in
        ('state','version','algorithm','rated_w')},sort_keys=True).encode())


def prepare(expected,previous):
    import power_budget
    config=power_budget.load()
    if not config or not config['enabled']:raise ValueError('An existing confirmed budget is required')
    before=inventory_snapshot()
    path=base.prepare(expected,previous)
    data=json.loads(path.read_text())
    for relative in ('instance/production/inverter-controls.json','instance/production/monitor-schedule.json'):
        source=ROOT/relative
        if source.exists():
            assert source.is_file() and not source.is_symlink()
            content=source.read_bytes()
            destination=Path(data['backup_dir'])/relative.replace('/','__')
            fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(content)
            data['protected'][relative]=digest(content)
    changed=base.git('diff','--name-only',previous,expected).splitlines()
    files={}
    for relative in changed:
        current=(ROOT/relative).read_bytes()
        old=Path(data['previous_source'])/relative
        files[relative]=dict(applied=digest(current),previous=digest(old.read_bytes()) if old.exists() else None)
    assert before==inventory_snapshot()
    assert context(config)==context(power_budget.load())
    data['feedback_release']=dict(inventory=before,context=context(config),files=files,
        backup_sha256=digest(Path(data['database_backup']).read_bytes()))
    with path.open('w') as stream:
        json.dump(data,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    return path


def guard(path):
    import power_budget
    from power_controller import PowerController
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    data=json.loads(Path(path).read_text())
    assert Path(path).resolve()==Path(data['directory'])/'manifest.json'
    assert Path(data['directory']).resolve().parent==ROOT/'instance/releases'
    assert Path(data['previous_source']).resolve()==Path(data['directory'])/'previous-source'
    assert base.git('branch','--show-current')=='main' and not base.git('status','--porcelain')
    assert base.git('rev-parse','HEAD')==data['expected_sha']
    assert base.git('ls-remote','origin','refs/heads/main').split()[0]==data['expected_sha']
    expected=set(base.git('diff','--name-only',data['previous_sha'],data['expected_sha']).splitlines())
    assert set(data['feedback_release']['files'])==expected
    assert all(not Path(name).is_absolute() and '..' not in Path(name).parts for name in expected)
    base.verify_protected(data)
    assert digest(Path(data['database_backup']).read_bytes())==data['feedback_release']['backup_sha256']
    assert inventory_snapshot()==data['feedback_release']['inventory']
    config=power_budget.load()
    assert config and config['enabled'] and context(config)==data['feedback_release']['context']
    engine=create_engine('sqlite:///'+str(base.DATABASE),creator=lambda:sqlite3.connect(
        base.DATABASE.as_uri()+'?mode=ro',uri=True,timeout=5))
    try:
        with Session(engine) as session:PowerController(engine)._authorized(session,config)
    finally:engine.dispose()
    return data


def freeze(path):
    import power_budget
    data=guard(path)
    destination=Path(data['directory'])/'stopped-policy.json'
    with power_budget.command_lock():
        original=power_budget.policy_path().read_bytes()
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(original);stream.flush();os.fsync(stream.fileno())


def disable_for_rollback(path):
    import power_budget
    data=json.loads(Path(path).read_text())
    with power_budget.command_lock():
        config=power_budget.load()
        assert context(config)==data['feedback_release']['context'],'Foreign goal change; do not overwrite'
        # Retain EVERY pending/sent/fault record from the NEW controller, not an old backup.
        config.update(version=1,enabled=False)
        config['state'].update(phase='disabled',actual_w=None,known=0,armed=False,last_cycle_at=None,
            reason='Відкат оновлення: автоматизацію зупинено; останні ліміти й записи аудиту збережені.')
        power_budget.save(config)


def check(path):
    import power_budget
    from app import create_app
    from configuration import read_secret_file
    from deploy.check_portal import http_check, telegram_check
    from sqlalchemy import create_engine,select
    from sqlalchemy.orm import Session
    from models import User
    data=guard(path);check_instances()
    state=power_budget.status()
    assert power_budget.load()['version']==2 and state['algorithm']==power_budget.ALGORITHM
    assert state['controller_alive'] and state['armed'] and state['phase']!='paused'
    engine=create_engine('sqlite:///file:'+str(base.DATABASE)+'?mode=ro&uri=true')
    try:
        with Session(engine) as session:
            user=session.scalar(select(User).where(User.role=='Administrator',User.active.is_(True)))
            assert user is not None
            identity=dict(user_id=user.id,session_version=user.session_version)
        application=create_app(dict(SECRET_KEY=read_secret_file(ROOT/'instance/production/web-secret.key'),
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',SESSION_COOKIE_SECURE=False))
        cookie=application.session_interface.get_signing_serializer(application).dumps(identity)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for origin in ('http://127.0.0.1:5000','http://127.0.0.1:80'):
            http_check(origin)
            for route in ('/power-budget','/power-budget/status','/api/statuses'):
                request=urllib.request.Request(origin+route,headers={'Cookie':'session='+cookie})
                with opener.open(request,timeout=10) as response:
                    assert response.status==200
                    content=response.read().decode()
                if route=='/power-budget':assert 'name="rated_kw"' not in content
                elif route=='/power-budget/status':
                    status=json.loads(content)
                    assert status['algorithm']==power_budget.ALGORITHM and status['controller_alive']
                else:
                    status=json.loads(content)
                    assert status['counts']['total']==data['feedback_release']['inventory']['count']
                    assert status['monitor']['alive']
        telegram_check()  # Single existing bot + getMe only; no Telegram messages.
    finally:engine.dispose()
    return dict(sha=data['expected_sha'],phase=state['phase'],known=state['known'],total=state['total'])


class Activation:
    def __init__(self,manifest,*,root=ROOT,runner=subprocess.run,pause=time.sleep):
        self.manifest=str(manifest);self.root=root;self.runner=runner;self.pause=pause
        self.data=None

    def run(self,*args):
        result=self.runner(list(args),cwd=self.root,capture_output=True,text=True,timeout=120)
        if result.returncode:
            if self.data:
                path=Path(self.data['directory'])/'feedback-command-failure.json'
                atomic(path,json.dumps(dict(command=list(args),returncode=result.returncode,
                    stdout=result.stdout,stderr=getattr(result,'stderr','')),indent=2).encode(),0o600)
            raise RuntimeError('Command failed: '+args[0])
        return result.stdout.strip()

    def helper(self,action):
        return self.run('/usr/sbin/runuser','-u','codex','--',str(self.root/'.venv/bin/python'),
                        '-m','deploy.feedback_release',action,'--manifest',self.manifest)

    def service(self,name):
        return self.run('systemctl','show',name,'--property=Id,MainPID,ActiveState,ExecStart,WorkingDirectory')

    def record(self,result,**details):
        atomic(Path(self.data['directory'])/'feedback-result.json',
            (json.dumps(dict(result=result,sha=self.data['expected_sha'],at=time.time(),**details),indent=2)+'\n').encode(),0o644)

    def restore_source(self):
        files=self.data['feedback_release']['files']
        # Check ALL paths before returning any previous source; never overwrite foreign edits.
        for relative,item in files.items():
            target=self.root/relative
            assert not target.is_symlink() and digest(target.read_bytes())==item['applied'],'Foreign source edit'
            if item['previous'] is not None:
                previous=Path(self.data['previous_source'])/relative
                assert not previous.is_symlink() and digest(previous.read_bytes())==item['previous']
        for relative,item in files.items():
            if item['previous'] is not None:
                target=self.root/relative
                original=target.stat()
                atomic(target,(Path(self.data['previous_source'])/relative).read_bytes(),original.st_mode&0o777)
                os.chown(target,original.st_uid,original.st_gid,follow_symlinks=False)
        # Additions are unused by the old source and kept; no files/data/backups are deleted.

    def activate(self):
        if os.geteuid()!=0:raise PermissionError('Run as root; never send the password to chat')
        self.data=json.loads(self.helper('preflight'))
        untouched={name:self.service(name) for name in UNTOUCHED}
        for name in SERVICES:
            state=self.service(name)
            assert 'ActiveState=active' in state and 'WorkingDirectory='+str(self.root) in state
            if name==SERVICES[0]:assert '--enable-writes' in state
            atomic(Path(self.data['directory'])/(name+'.before'),state.encode(),0o600)
        self.record('PREPARED')
        try:
            # Stop current writers before the schema/algorithm transition. Monitor stays up.
            self.run('systemctl','stop',SERVICES[0])
            self.run('systemctl','stop',*SERVICES[1:])
            self.helper('freeze')  # Captures final durable pending markers AFTER shutdown.
            self.run('systemctl','start',*SERVICES[1:])
            self.run('systemctl','start',SERVICES[0])
            ready=None
            for _ in range(12):
                try:
                    ready=self.helper('check');break
                except Exception:self.pause(3)
            if ready is None:raise RuntimeError('Feedback readiness failed')
            assert all(self.service(name)==before for name,before in untouched.items()),'Untouched service changed'
        except BaseException as error:
            self.record('FAILED',error=type(error).__name__)
            self.run('systemctl','stop',SERVICES[0])
            self.run('systemctl','stop',*SERVICES[1:])
            try:
                self.helper('disable-for-rollback')
                self.restore_source()
                self.run('systemctl','start',*SERVICES[1:])
                self.run('systemctl','is-active',*SERVICES[1:],*UNTOUCHED[:2])
                self.helper('rollback-check')
                self.record('ROLLED_BACK',error=type(error).__name__,controller='stopped',budget='disabled',database_restored=False)
            except BaseException:
                self.record('ROLLBACK_FAILED',controller='stopped')
                raise RuntimeError('Rollback refused/failed; inspect private diagnostics') from None
            raise RuntimeError('Activation failed; previous source restored, controller stopped; new data preserved') from None
        self.record('DEPLOYED',readiness=ready)
        try:
            tag=self.helper('tag').splitlines()[-1]
            self.record('DEPLOYED',tag=tag,tag_push='success',readiness=ready)
        except Exception:
            self.record('DEPLOYED',tag_push='pending',readiness=ready)
        return 'Feedback activation verified; see '+str(Path(self.data['directory'])/'feedback-result.json')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','preflight','guard','freeze','check','disable-for-rollback','rollback-check','activate','tag'))
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--expected-sha');parser.add_argument('--previous-sha')
    args=parser.parse_args()
    if args.action=='prepare':print(prepare(args.expected_sha,args.previous_sha));return
    if not args.manifest:parser.error('--manifest required')
    if args.action in ('guard','preflight'):
        data=guard(args.manifest)
        if args.action=='preflight':check_instances()
        print(json.dumps(data))
    elif args.action=='freeze':freeze(args.manifest)
    elif args.action=='disable-for-rollback':disable_for_rollback(args.manifest)
    elif args.action=='check':print(json.dumps(check(args.manifest)))
    elif args.action=='rollback-check':
        from deploy.check_portal import http_check
        for origin in ('http://127.0.0.1:5000','http://127.0.0.1:80'):http_check(origin)
    elif args.action=='tag':
        check(args.manifest)
        data=guard(args.manifest)
        tag='production-feedback-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+data['expected_sha'][:7]
        base.git('-c','user.name=Codex','-c','user.email=codex@localhost','tag','-a',tag,data['expected_sha'],
                 '-m','Verified measured-generation controller deployment')
        base.git('push','origin',tag);print(tag)
    else:
        if os.geteuid()!=0:raise SystemExit('Run as root; never send the password to chat.')
        fd=os.open('/run/ingecon-controls-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            print(Activation(args.manifest).activate())
        finally:os.close(fd)


if __name__=='__main__':
    try:main()
    except Exception:
        raise SystemExit('Feedback operation failed. Secrets omitted; inspect private release diagnostics.') from None
