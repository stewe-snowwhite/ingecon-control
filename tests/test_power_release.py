"""Production guards use synthetic 26-device SQLite and mocked Git/process state."""
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from deploy import power_release as release
from models import db, Inverter, MonitorConfig, TelegramLink, User
import inverter_controls
import power_budget
from tests.test_inverter_controls import authorized

SHA='a'*40
PREVIOUS='b'*40


@pytest.fixture
def saved(tmp_path,monkeypatch,authorized):
    root=tmp_path/'project';production=root/'instance/production';production.mkdir(parents=True)
    database=production/'ingecon.sqlite';engine=create_engine('sqlite:///'+str(database))
    db.metadata.create_all(engine)
    with Session(engine) as s,s.begin():
        user=User(id=1,username='admin',role='Administrator');user.set_password('synthetic-password-only')
        s.add(user);s.flush()
        s.add(TelegramLink(telegram_id=1001,user_id=1,user_version=user.session_version,linked_at=100))
        for number in range(1,27):
            s.add(Inverter(id=number,name='Mock '+str(number),group='КТП 1' if number<=8 else 'КТП 2' if number<=17 else 'КТП 3',
                           model='ingecon_sun_100tl',ip_address='192.0.2.'+str(number)))
            s.add(MonitorConfig(inverter_id=number,port=502,device_id=1))
    policy=production/'inverter-controls.json';policy.write_bytes(inverter_controls.policy_path().read_bytes());policy.chmod(0o600)
    monkeypatch.setenv('INGECON_INVERTER_CONTROLS_FILE',str(policy))
    monkeypatch.setenv('INGECON_POWER_BUDGET_FILE',str(production/'power-budget.json'))
    monkeypatch.setattr(release,'ROOT',root);monkeypatch.setattr(release,'DATABASE',database)
    monkeypatch.setattr(release.base,'ROOT',root)
    monkeypatch.setattr(release,'check_instances',lambda:None)
    folder=root/'instance/releases/prepared';folder.mkdir(parents=True)
    backups=production/'backups/prepared';backups.mkdir(parents=True)
    old=folder/'previous-source';old.mkdir();(old/'app.py').write_text('previous source')
    manifest=folder/'manifest.json'
    def prepare(expected,previous):
        assert (expected,previous)==(SHA,PREVIOUS)
        saved=backups/'production.sqlite'
        with sqlite3.connect(database) as source,sqlite3.connect(saved) as destination:source.backup(destination)
        release.base.check_copy(saved,backups/'restore-verified.sqlite')
        manifest.write_text(json.dumps(dict(version=1,directory=str(folder),backup_dir=str(backups),
            expected_sha=SHA,previous_sha=PREVIOUS,previous_source=str(old),protected={},database_backup=str(saved))))
        manifest.chmod(0o600)
        return manifest
    def git(*args,**kwargs):
        if args==('status','--porcelain'):return ''
        if args==('branch','--show-current'):return 'main'
        if args==('rev-parse','HEAD'):return SHA
        if args==('ls-remote','origin','refs/heads/main'):return SHA+'\trefs/heads/main'
        raise AssertionError(args)
    monkeypatch.setattr(release.base,'prepare',prepare)
    monkeypatch.setattr(release.base,'git',git)
    yield root,engine,manifest,authorized
    engine.dispose()


def test_preparation_binds_all_26_targets_and_protects_settings_without_touching_data(saved):
    root,engine,path,clients=saved
    original=(root/'instance/production/ingecon.sqlite').read_bytes()
    assert release.prepare(SHA,PREVIOUS)==path
    data=release.guard(path)
    assert data['power_release']['inventory']['count']==26
    assert data['power_release']['inventory']['groups']=={'КТП 1':8,'КТП 2':9,'КТП 3':9}
    assert data['power_release']['controller_writes'] is True
    assert data['power_release']['monitor_workers']==8
    assert data['power_release']['migrations'] is False
    assert (root/'instance/production/ingecon.sqlite').read_bytes()==original and clients==[]


@pytest.mark.parametrize('change',['device','account','link','policy','remote','backup'])
def test_guard_refuses_drift_before_activation(saved,monkeypatch,change):
    root,engine,path,clients=saved
    release.prepare(SHA,PREVIOUS)
    with Session(engine) as s,s.begin():
        if change=='device':s.get(Inverter,1).ip_address='192.0.2.100'
        elif change=='account':s.get(User,1).set_password('new-synthetic-password')
        elif change=='link':s.get(TelegramLink,1001).user_id=2
    if change=='policy':inverter_controls.set_authorization(False)
    if change=='remote':monkeypatch.setattr(release.base,'git',lambda *args,**kwargs:'different')
    if change=='backup':
        data=json.loads(path.read_text());Path(data['database_backup']).write_bytes(b'changed backup')
    with pytest.raises((AssertionError,ValueError)):release.guard(path)
    assert clients==[]


def test_web_logout_generation_does_not_look_like_account_loss(saved):
    _,engine,path,_=saved
    release.prepare(SHA,PREVIOUS)
    with Session(engine) as s,s.begin():
        user=s.get(User,1);user.session_version+=1
        s.get(TelegramLink,1001).user_version=user.session_version
    assert release.guard(path)['power_release']['inventory']['count']==26


def test_active_automation_must_be_explicitly_disabled_before_replacement(saved):
    _,engine,_,clients=saved
    service=power_budget.BudgetService(engine)
    intent,_=service.preview(1,400000);service.confirm(1,intent)
    with pytest.raises(ValueError,match='Disable'):release.prepare(SHA,PREVIOUS)
    assert clients==[]


@pytest.mark.parametrize('bot,main,controllers,controller_main',[([], '10',[], '0'),
    (['10','11'],'10',[],'0'),(['10'],'10',['20'],'0'),(['10'],'10',['20','21'],'20')])
def test_rogue_or_duplicate_processes_are_refused(bot,main,controllers,controller_main):
    with pytest.raises(AssertionError):release.validate_instances(bot,main,controllers,controller_main)


def test_single_service_owned_processes_are_accepted():
    release.validate_instances(['10'],'10',[],'0')
    release.validate_instances(['10'],'10',['20'],'20')


def test_status_readiness_checks_full_fleet_not_twenty():
    release.validate_snapshot(dict(counts={'total':26},inverters=[{}]*26,monitor={'alive':True}),26)
    with pytest.raises(AssertionError):
        release.validate_snapshot(dict(counts={'total':20},inverters=[{}]*20,monitor={'alive':True}),26)
