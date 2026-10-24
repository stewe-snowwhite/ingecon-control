"""Activation/rollback rehearsal: fake systemctl/Git/HTTP, isolated files only."""
import json
from types import SimpleNamespace

import pytest

from deploy.feedback_release import Activation, SERVICES, UNTOUCHED, context, digest
from tests.test_power_release import saved
from tests.test_inverter_controls import authorized


@pytest.fixture
def rehearsal(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    directory=root/'instance/releases/snapshot';directory.mkdir(parents=True)
    old=directory/'previous-source';old.mkdir()
    (root/'app.py').write_bytes(b'new application');(old/'app.py').write_bytes(b'old application')
    (root/'added.py').write_bytes(b'new unused helper')
    files={'app.py':dict(applied=digest(b'new application'),previous=digest(b'old application')),
           'added.py':dict(applied=digest(b'new unused helper'),previous=None)}
    data=dict(directory=str(directory),expected_sha='a'*40,previous_source=str(old),feedback_release={'files':files})
    manifest=directory/'manifest.json';manifest.write_text(json.dumps(data))
    states={name:dict(active=True,pid=str(n)) for n,name in enumerate((*SERVICES,*UNTOUCHED),1)}
    config=dict(enabled=True,pending=[dict(id='1',percent=35,at=100)],audit=['NEW-PRODUCTION-DATA'])
    mode=['ok'];calls=[]
    def run(args,**kwargs):
        calls.append(args)
        output=''
        if args[0]=='/usr/sbin/runuser':
            action=args[-3]
            if action=='preflight':output=json.dumps(data)
            elif action=='freeze':
                assert not any(states[name]['active'] for name in SERVICES)
                (directory/'stopped-policy.json').write_text(json.dumps(config))
            elif action=='check':
                assert all(states[name]['active'] for name in SERVICES)
                # Simulate a durable NEW controller request, which rollback must preserve.
                if len(config['pending'])==1:config['pending'].append(dict(id='2',percent=36,at=200))
                if mode[0] in ('check','foreign'):return SimpleNamespace(returncode=1,stdout='')
                output=json.dumps(dict(known=26,total=26))
            elif action=='disable-for-rollback':
                assert not states[SERVICES[0]]['active'];config['enabled']=False
            elif action=='rollback-check':
                assert all(states[name]['active'] for name in SERVICES[1:]) and not config['enabled']
            elif action=='tag':
                if mode[0]=='tag':return SimpleNamespace(returncode=1,stdout='')
                output='HTTP verified\nproduction-feedback-test'
            else:raise AssertionError(action)
        elif args[0]=='systemctl':
            command=args[1]
            if command=='show':
                name=args[2];state=states[name]
                output=('Id='+name+'\nMainPID='+state['pid']+'\nActiveState='+('active' if state['active'] else 'inactive')
                    +'\nExecStart=python'+(' --enable-writes' if name==SERVICES[0] else '')+'\nWorkingDirectory='+str(root))
            elif command in ('stop','start'):
                for name in args[2:]:
                    states[name]['active']=command=='start'
                    states[name]['pid']=str(int(states[name]['pid'])+100) if command=='start' else '0'
                if command=='start' and SERVICES[0] in args and mode[0]=='foreign':
                    (root/'app.py').write_bytes(b'FOREIGN EDIT')
            elif command=='is-active':assert all(states[name]['active'] for name in args[2:])
            else:raise AssertionError(args)
        else:raise AssertionError(args)
        return SimpleNamespace(returncode=0,stdout=output)
    monkeypatch.setattr('deploy.feedback_release.os.geteuid',lambda:0)
    activation=Activation(manifest,root=root,runner=run,pause=lambda _:None)
    return SimpleNamespace(root=root,directory=directory,activation=activation,mode=mode,calls=calls,states=states,config=config)


def test_only_existing_controller_web_and_single_bot_are_restarted(rehearsal):
    r=rehearsal;untouched={n:dict(r.states[n]) for n in UNTOUCHED}
    r.activation.activate()
    assert {n:r.states[n] for n in UNTOUCHED}==untouched
    assert r.config['enabled']
    assert r.config['audit']==['NEW-PRODUCTION-DATA']
    assert r.calls.count(['systemctl','start','ingecon-telegram'])==0
    assert sum(c[:2]==['systemctl','start'] and 'ingecon-telegram' in c for c in r.calls)==1
    result=json.loads((r.directory/'feedback-result.json').read_text())
    assert result['result']=='DEPLOYED' and result['tag']=='production-feedback-test'
    assert (r.root/'app.py').read_bytes()==b'new application'


def test_failed_readiness_stops_controller_restores_source_and_preserves_new_pending(rehearsal):
    r=rehearsal;r.mode[0]='check'
    with pytest.raises(RuntimeError,match='previous source restored'):r.activation.activate()
    assert not r.states[SERVICES[0]]['active'] and not r.config['enabled']
    assert len(r.config['pending'])==2 and r.config['audit']==['NEW-PRODUCTION-DATA']
    assert (r.root/'app.py').read_bytes()==b'old application'
    assert (r.root/'added.py').read_bytes()==b'new unused helper'
    assert all(r.states[n]['active'] for n in (*SERVICES[1:],*UNTOUCHED))
    assert json.loads((r.directory/'feedback-result.json').read_text())['result']=='ROLLED_BACK'


def test_foreign_changes_are_not_overwritten_and_controller_stays_stopped(rehearsal):
    r=rehearsal;r.mode[0]='foreign'
    with pytest.raises(RuntimeError,match='Rollback refused'):r.activation.activate()
    assert (r.root/'app.py').read_bytes()==b'FOREIGN EDIT' and not r.states[SERVICES[0]]['active']
    assert json.loads((r.directory/'feedback-result.json').read_text())['result']=='ROLLBACK_FAILED'


def test_failed_tag_push_keeps_healthy_deployment_running(rehearsal):
    r=rehearsal;r.mode[0]='tag'
    r.activation.activate()
    assert all(r.states[n]['active'] for n in SERVICES) and r.config['enabled']
    result=json.loads((r.directory/'feedback-result.json').read_text())
    assert result['result']=='DEPLOYED' and result['tag_push']=='pending'


def test_root_is_required_before_any_mutation(rehearsal,monkeypatch):
    monkeypatch.setattr('deploy.feedback_release.os.geteuid',lambda:1000)
    with pytest.raises(PermissionError):rehearsal.activation.activate()
    assert rehearsal.calls==[]


def test_goal_context_ignores_runtime_and_legacy_nominal_but_preserves_identity():
    before=dict(version=1,rated_w=100000,target_w=900000,revision='x',owner_id=1,targets=[1],state={'pending':[1]})
    after=dict(before,version=2,algorithm='measured_feedback_v1',state={'pending':[1,2]})
    after.pop('rated_w')
    assert context(before)==context(after)
    assert context(before)!=context(dict(after,target_w=400000))
    assert context(before)!=context(dict(after,owner_id=2))


@pytest.fixture
def prepared_active(saved,monkeypatch):
    from deploy import feedback_release as release
    import power_budget
    root,engine,path,clients=saved
    monkeypatch.setattr(release,'ROOT',root)
    monkeypatch.setattr(release.base,'DATABASE',root/'instance/production/ingecon.sqlite')
    (root/'app.py').write_text('new application source')
    previous_git=release.base.git
    def git(*args,**kwargs):
        if args[:2]==('diff','--name-only'):return 'app.py'
        return previous_git(*args,**kwargs)
    monkeypatch.setattr(release.base,'git',git)
    service=power_budget.BudgetService(engine)
    intent,_=service.preview(1,900000)
    assert service.confirm(1,intent)=='configured'
    assert release.prepare('a'*40,'b'*40)==path
    return root,engine,path,clients


def test_active_goal_preparation_and_guard_do_not_modify_data_or_send_commands(prepared_active):
    from deploy import feedback_release as release
    import power_budget
    root,engine,path,clients=prepared_active
    before=(root/'instance/production/ingecon.sqlite').read_bytes()
    policy=power_budget.policy_path().read_bytes()
    assert release.guard(path)['feedback_release']['inventory']['count']==26
    assert power_budget.policy_path().read_bytes()==policy
    assert (root/'instance/production/ingecon.sqlite').read_bytes()==before and clients==[]
    # Controller runtime is allowed to advance while awaiting root activation.
    config=power_budget.load();config['state']['last_cycle_at']=100
    power_budget.save(config)
    release.guard(path)


def test_guard_refuses_goal_drift_without_overwriting_it(prepared_active):
    from deploy import feedback_release as release
    import power_budget
    _,_,path,clients=prepared_active
    config=power_budget.load();config['target_w']=400000
    power_budget.save(config)
    with pytest.raises(AssertionError):release.guard(path)
    assert power_budget.load()['target_w']==400000 and clients==[]


def test_rollback_policy_preserves_new_pending_goal_and_all_operational_rows(prepared_active):
    from deploy import feedback_release as release
    import power_budget
    root,_,path,clients=prepared_active
    release.freeze(path)
    before=(root/'instance/production/ingecon.sqlite').read_bytes()
    config=power_budget.load()
    config['state'].update(pending=[dict(id='1',percent=33,at=200)],history={'1':{'power_w':30000}})
    power_budget.save(config)
    release.disable_for_rollback(path)
    current=power_budget.load()
    assert not current['enabled'] and current['version']==1 and current['target_w']==900000
    assert current['state']['actual_w'] is None and current['state']['known']==0
    assert current['state']['pending']==config['state']['pending']
    assert current['state']['history']==config['state']['history']
    assert (root/'instance/production/ingecon.sqlite').read_bytes()==before and clients==[]
