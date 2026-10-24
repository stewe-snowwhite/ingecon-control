"""Root-only controlled activation. No generation target or inverter command is sent."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

ROOT=Path('/opt/ingecon-control')
ETC=Path('/etc/systemd/system')
SERVICES=('ingecon-web','ingecon-telegram','ingecon-monitor','ingecon-power-controller')
UNTOUCHED=('ingecon-discovery','nginx','tailscaled')


def digest(data):return hashlib.sha256(data).hexdigest()


def atomic(path,data,mode=0o644):
    assert not path.is_symlink()
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


class Activation:
    def __init__(self,manifest,*,root=ROOT,etc=ETC,backups=Path('/var/backups/ingecon-control'),
                 runner=subprocess.run,pause=time.sleep):
        self.manifest=str(manifest);self.root=root;self.etc=etc;self.backups=backups
        self.runner,self.pause=runner,pause
        self.changed=[];self.before={};self.files={};self.state=None
        self.previous_source=None

    def run(self,*args,required=True):
        result=self.runner(list(args),cwd=self.root,text=True,capture_output=True,timeout=120)
        if required and result.returncode:raise RuntimeError('Command failed: '+args[0])
        return result.stdout.strip()

    def helper(self,action):
        return self.run('/usr/sbin/runuser','-u','codex','--',str(self.root/'.venv/bin/python'),
                        '-m','deploy.power_release',action,'--manifest',self.manifest)

    def service(self,name):
        result=self.run('systemctl','show',name,'--property=ActiveState,UnitFileState,MainPID,WorkingDirectory,LoadState,ExecStart',required=False)
        return dict(line.split('=',1) for line in result.splitlines() if '=' in line)

    def desired_files(self):
        command=str(self.root/'.venv/bin/python')
        database='sqlite:////opt/ingecon-control/instance/production/ingecon.sqlite'
        return {
            'ingecon-power-controller.service':(self.root/'deploy/systemd/ingecon-power-controller.service').read_bytes(),
            'ingecon-power-controller.service.d/20-writes.conf':(
                '[Service]\nExecStart=\nExecStart='+command+' -m power_controller --database-uri '+database+' --enable-writes\n').encode(),
            'ingecon-monitor.service.d/35-power-budget-polling.conf':(
                '[Service]\nExecStart=\nExecStart='+command+' -m monitor --database-uri '+database+' --interval 30 --workers 8\n').encode(),
        }

    def snapshot(self,manifest):
        self.previous_source=Path(manifest['previous_source'])
        assert re.fullmatch(r'[A-Za-z0-9_./-]+',str(self.previous_source))
        assert self.previous_source.is_dir() and not self.previous_source.is_symlink()
        for name in SERVICES[:3]:
            assert not (self.etc/(name+'.service.d/zz-power-budget-rollback.conf')).exists(),'Existing rollback requires review'
        assert not self.backups.is_symlink()
        self.backups.mkdir(parents=True,exist_ok=True,mode=0o700)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        self.state=self.backups/('power-'+stamp);self.state.mkdir(mode=0o700)
        for name in (*SERVICES,*UNTOUCHED):
            self.before[name]=self.service(name)
        for name in SERVICES[:3]:
            assert self.before[name]['ActiveState']=='active'
            assert self.before[name]['WorkingDirectory']==str(self.root)
        assert self.before['ingecon-power-controller'].get('ActiveState')!='active','Controller already running; inspect before replacing it'
        for name in SERVICES:
            effective=self.run('systemctl','cat',name,required=False)
            atomic(self.state/(name+'.effective'),effective.encode(),0o600)
            source=self.etc/(name+'.service')
            if source.exists():
                assert not source.is_symlink()
                atomic(self.state/(name+'.service'),source.read_bytes(),0o600)
            dropins=self.etc/(name+'.service.d')
            if dropins.exists():
                assert not dropins.is_symlink()
                for item in dropins.iterdir():
                    assert item.is_file() and not item.is_symlink()
                    atomic(self.state/(name+'__'+item.name),item.read_bytes(),0o600)
        for relative,applied in self.desired_files().items():
            path=self.etc/relative
            assert not path.is_symlink() and not path.parent.is_symlink()
            previous=path.read_bytes() if path.exists() else None
            self.files[relative]=dict(previous=previous,applied=applied,mode=path.stat().st_mode&0o777 if path.exists() else 0o644)
        self.record('PREPARED',sha=manifest['expected_sha'],inventory=manifest['power_release']['inventory']['count'])

    def record(self,result,**values):
        if self.state is None:return
        data=dict(result=result,at=time.time(),manifest=self.manifest,changed=self.changed,
                  services=self.before,files={key:dict(previous=digest(row['previous']) if row['previous'] is not None else None,
                    applied=digest(row['applied']),mode=row['mode']) for key,row in self.files.items()},**values)
        atomic(self.state/'result.json',(json.dumps(data,indent=2)+'\n').encode(),0o600)
        for index,(relative,row) in enumerate(self.files.items()):
            if row['previous'] is not None:atomic(self.state/('managed-original-'+str(index)),row['previous'],0o600)

    def rollback(self):
        # Always stop autonomous writes BEFORE returning older writer code/config.
        current=self.service('ingecon-power-controller')
        if current.get('LoadState')!='not-found':self.run('systemctl','stop','ingecon-power-controller')
        was_enabled=self.before['ingecon-power-controller'].get('UnitFileState')=='enabled'
        if not was_enabled and current.get('UnitFileState')=='enabled':
            self.run('systemctl','disable','ingecon-power-controller')
        # Guard all paths before restoring any: never overwrite foreign edits.
        for relative in self.changed:
            target=self.etc/relative
            assert not target.is_symlink() and target.read_bytes()==self.files[relative]['applied'],'Foreign configuration change; rollback refused'
        for relative in reversed(self.changed):
            target=self.etc/relative;row=self.files[relative]
            if row['previous'] is None:target.unlink()  # Only our verified newly created unit/drop-in.
            else:atomic(target,row['previous'],row['mode'])
        # Run the prior committed source, preserving the current Git checkout.
        environment=''.join('Environment='+key+'='+str(self.root/relative)+'\n' for key,relative in (
            ('INGECON_LOCK_DIR','instance/locks'),
            ('INGECON_MODBUS_CONFIG_FILE','instance/production/modbus.json'),
            ('INGECON_INVERTER_CONTROLS_FILE','instance/production/inverter-controls.json'),
            ('INGECON_MONITOR_SCHEDULE_FILE','instance/production/monitor-schedule.json'),
            ('INGECON_POWER_BUDGET_FILE','instance/production/power-budget.json')))
        python=str(self.root/'.venv/bin/python')
        commands={
            'ingecon-web':str(self.root/'.venv/bin/gunicorn')+' --config '+str(self.previous_source/'deploy/gunicorn.conf.py')+' portal_wsgi:app',
            'ingecon-monitor':python+' -m monitor --database-uri sqlite:////opt/ingecon-control/instance/production/ingecon.sqlite --interval 30',
            'ingecon-telegram':python+' '+str(self.previous_source/'telegram_bot.py')+' run --database '+str(self.root/'instance/production/ingecon.sqlite')+' --token-file '+str(self.root/'instance/telegram/bot.key')}
        for name,command in commands.items():
            path=self.etc/(name+'.service.d/zz-power-budget-rollback.conf')
            assert not path.exists() and not path.is_symlink(),'Foreign rollback file appeared'
            atomic(path,('[Service]\nWorkingDirectory='+str(self.previous_source)+'\n'+environment+'ExecStart=\nExecStart='+command+'\n').encode())
        self.run('systemctl','daemon-reload')
        self.run('systemctl','restart','ingecon-monitor','ingecon-web','ingecon-telegram')
        if self.before['ingecon-power-controller'].get('ActiveState')=='active':
            self.run('systemctl','start','ingecon-power-controller')
        if was_enabled:self.run('systemctl','enable','ingecon-power-controller')
        self.run('systemctl','is-active',*SERVICES[:3])
        self.helper('check')
        self.record('ROLLED_BACK',running_source=str(self.previous_source))

    def activate(self,*,enable_writes):
        if os.geteuid()!=0:raise PermissionError('Run from root; never send a password to chat')
        if enable_writes is not True:raise ValueError('Explicit --enable-writes is required')
        manifest=json.loads(self.helper('guard'))
        self.snapshot(manifest)
        # Repeat guards after saving service state, before the first change.
        self.helper('guard')
        try:
            for relative,row in self.files.items():
                target=self.etc/relative
                current=target.read_bytes() if target.exists() else None
                assert current==row['previous'],'Service configuration changed after snapshot'
                atomic(target,row['applied'])
                self.changed.append(relative)
            self.run('systemd-analyze','verify',str(self.etc/'ingecon-power-controller.service'))
            self.run('systemctl','daemon-reload')
            self.run('systemctl','restart','ingecon-monitor','ingecon-web','ingecon-telegram')
            self.run('systemctl','enable','ingecon-power-controller')
            self.run('systemctl','start','ingecon-power-controller')
            ready=False
            for _ in range(20):
                try:
                    self.helper('check')
                    self.run('systemctl','is-active',*SERVICES)
                    self.run('systemctl','is-enabled','ingecon-power-controller')
                    assert '--enable-writes' in self.service('ingecon-power-controller').get('ExecStart','')
                    assert '--workers 8' in self.service('ingecon-monitor').get('ExecStart','')
                    for name in UNTOUCHED:
                        assert self.service(name).get('MainPID')==self.before[name].get('MainPID'),'Untouched service changed'
                    ready=True;break
                except Exception:self.pause(3)
            if not ready:raise RuntimeError('Production readiness failed')
        except BaseException as error:
            self.record('FAILED',error=type(error).__name__)
            try:self.rollback()
            except BaseException as rollback_error:
                self.record('ROLLBACK_FAILED',error=type(rollback_error).__name__)
                raise RuntimeError('Rollback refused/failed; inspect private diagnostics') from None
            raise RuntimeError('Activation failed and was rolled back') from None
        self.record('DEPLOYED',sha=manifest['expected_sha'])
        # A tag-push failure must not undo a healthy deployment.
        try:
            tag=self.helper('tag')
            self.record('DEPLOYED',sha=manifest['expected_sha'],tag=tag,tag_push='success')
            return dict(result='DEPLOYED',sha=manifest['expected_sha'],tag=tag,tag_push='success',backup=str(self.state))
        except Exception:
            self.record('DEPLOYED',sha=manifest['expected_sha'],tag_push='pending')
            return dict(result='DEPLOYED',sha=manifest['expected_sha'],tag_push='pending',backup=str(self.state))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--enable-writes',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run as root; never send the password to chat.')
    fd=os.open('/run/ingecon-controls-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        activation=Activation(args.manifest)
        try:print(json.dumps(activation.activate(enable_writes=args.enable_writes)))
        except BaseException:
            raise SystemExit('Power activation failed/refused. Diagnostics: '+str(activation.state or '/var/backups/ingecon-control')+'; credentials omitted.') from None
    finally:os.close(fd)


if __name__=='__main__':main()
