"""Isolated root-deployment rehearsal: fake systemctl/runuser/HTTP/Git only."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.power_activate import Activation, SERVICES, UNTOUCHED


@pytest.fixture
def rehearsal(tmp_path,monkeypatch):
    root=tmp_path/'project';etc=tmp_path/'etc/systemd/system';backups=tmp_path/'backups'
    root.mkdir();etc.mkdir(parents=True)
    template=root/'deploy/systemd/ingecon-power-controller.service';template.parent.mkdir(parents=True)
    template.write_text('[Service]\nUser=codex\nExecStart=/mock/python -m power_controller\n')
    database=root/'instance/production/ingecon.sqlite';database.parent.mkdir(parents=True)
    database.write_bytes(b'preserve all production data')
    previous=root/'instance/releases/old/previous-source';previous.mkdir(parents=True)
    for name in SERVICES[:3]:(etc/(name+'.service')).write_text('previous '+name+'\n')
    custom=etc/'ingecon-monitor.service.d/local.conf';custom.parent.mkdir();custom.write_text('UNIQUE OWNER SETTINGS\n')
    services={name:dict(ActiveState='active',UnitFileState='enabled',MainPID=str(i+10),
        WorkingDirectory=str(root),LoadState='loaded',ExecStart='previous '+name)
        for i,name in enumerate((*SERVICES[:3],*UNTOUCHED))}
    services['ingecon-power-controller']=dict(ActiveState='inactive',UnitFileState='disabled',MainPID='0',
                                            WorkingDirectory=str(root),LoadState='not-found',ExecStart='')
    calls=[];mode=[None]
    def runner(args,**kwargs):
        calls.append(args)
        code=0;out=''
        if args[0]=='/usr/sbin/runuser':
            action=args[-3]
            if action=='guard':out=json.dumps(dict(expected_sha='a'*40,previous_source=str(previous),power_release={'inventory':{'count':26}}))
            elif action=='check':
                if services['ingecon-power-controller']['ActiveState']=='active':
                    if mode[0]=='foreign':
                        (etc/'ingecon-power-controller.service.d/20-writes.conf').write_text('FOREIGN CHANGE\n')
                    if mode[0] in ('check','foreign'):code=1
                out='{"result":"READY"}'
            elif action=='tag':out='production-power-rehearsal'
            else:raise AssertionError(action)
        elif args[0]=='systemd-analyze':code=int(mode[0]=='verify')
        elif args[0]=='systemctl':
            action=args[1]
            if action=='show':out='\n'.join(key+'='+value for key,value in services[args[2]].items())
            elif action=='cat':
                path=etc/(args[2]+'.service');out=path.read_text() if path.exists() else ''
            elif action=='daemon-reload':
                path=etc/'ingecon-power-controller.service'
                services['ingecon-power-controller']['LoadState']='loaded' if path.exists() else 'not-found'
                services['ingecon-power-controller']['ExecStart']='python -m power_controller --enable-writes' if (etc/'ingecon-power-controller.service.d/20-writes.conf').exists() else ''
                services['ingecon-monitor']['ExecStart']='python -m monitor --workers 8' if (etc/'ingecon-monitor.service.d/35-power-budget-polling.conf').exists() else 'previous ingecon-monitor'
            elif action in ('start','restart'):
                for name in args[2:]:services[name].update(ActiveState='active',MainPID=str(int(services[name]['MainPID'])+100))
            elif action=='stop':services[args[2]].update(ActiveState='inactive',MainPID='0')
            elif action=='enable':services[args[2]]['UnitFileState']='enabled'
            elif action=='disable':services[args[2]]['UnitFileState']='disabled'
            elif action=='is-active':code=int(any(services[name]['ActiveState']!='active' for name in args[2:]))
            elif action=='is-enabled':code=int(services[args[2]]['UnitFileState']!='enabled')
            else:raise AssertionError(args)
        else:raise AssertionError('Unexpected process: '+str(args))
        return SimpleNamespace(stdout=out,stderr='',returncode=code)
    monkeypatch.setattr('deploy.power_activate.os.geteuid',lambda:0)
    activation=Activation(tmp_path/'manifest.json',root=root,etc=etc,backups=backups,runner=runner,pause=lambda _:None)
    return SimpleNamespace(activation=activation,root=root,etc=etc,services=services,calls=calls,mode=mode,
                           database=database,custom=custom)


def test_activation_installs_write_capable_service_without_sending_commands(rehearsal):
    r=rehearsal
    untouched={name:r.services[name]['MainPID'] for name in UNTOUCHED}
    result=r.activation.activate(enable_writes=True)
    assert result['result']=='DEPLOYED' and result['tag_push']=='success'
    assert '--enable-writes' in (r.etc/'ingecon-power-controller.service.d/20-writes.conf').read_text()
    assert '--workers 8' in (r.etc/'ingecon-monitor.service.d/35-power-budget-polling.conf').read_text()
    assert r.services['ingecon-power-controller']['UnitFileState']=='enabled'
    assert {name:r.services[name]['MainPID'] for name in UNTOUCHED}==untouched
    assert r.database.read_bytes()==b'preserve all production data'
    assert r.custom.read_text()=='UNIQUE OWNER SETTINGS\n'
    assert not (r.root/'instance/production/power-budget.json').exists()
    assert sum(call[:3]==['systemctl','start','ingecon-power-controller'] for call in r.calls)==1


@pytest.mark.parametrize('failure',['verify','check'])
def test_failure_rolls_back_owned_files_and_keeps_data_and_existing_settings(rehearsal,failure):
    r=rehearsal;r.mode[0]=failure
    with pytest.raises(RuntimeError,match='rolled back'):r.activation.activate(enable_writes=True)
    assert r.services['ingecon-power-controller']['ActiveState']=='inactive'
    assert r.services['ingecon-power-controller']['UnitFileState']=='disabled'
    assert not (r.etc/'ingecon-power-controller.service').exists()
    assert not (r.etc/'ingecon-monitor.service.d/35-power-budget-polling.conf').exists()
    assert r.custom.read_text()=='UNIQUE OWNER SETTINGS\n'
    assert r.database.read_bytes()==b'preserve all production data'
    assert all(r.services[name]['ActiveState']=='active' for name in SERVICES[:3])
    assert json.loads((r.activation.state/'result.json').read_text())['result']=='ROLLED_BACK'
    for name in SERVICES[:3]:
        text=(r.etc/(name+'.service.d/zz-power-budget-rollback.conf')).read_text()
        assert 'previous-source' in text and 'INGECON_INVERTER_CONTROLS_FILE=' in text
    assert not any(call[0]=='/usr/sbin/runuser' and call[-3]=='tag' for call in r.calls)


def test_foreign_edit_stops_controller_and_refuses_to_overwrite_configuration(rehearsal):
    r=rehearsal;r.mode[0]='foreign'
    with pytest.raises(RuntimeError,match='Rollback refused'):r.activation.activate(enable_writes=True)
    assert r.services['ingecon-power-controller']['ActiveState']=='inactive'
    assert (r.etc/'ingecon-power-controller.service.d/20-writes.conf').read_text()=='FOREIGN CHANGE\n'
    assert json.loads((r.activation.state/'result.json').read_text())['result']=='ROLLBACK_FAILED'


def test_write_flag_is_required_before_any_changes(rehearsal):
    with pytest.raises(ValueError):rehearsal.activation.activate(enable_writes=False)
    assert rehearsal.calls==[]


def test_root_is_required_before_any_changes(rehearsal,monkeypatch):
    monkeypatch.setattr('deploy.power_activate.os.geteuid',lambda:1000)
    with pytest.raises(PermissionError):rehearsal.activation.activate(enable_writes=True)
    assert rehearsal.calls==[]
