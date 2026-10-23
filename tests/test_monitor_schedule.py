"""Shared polling policy, real worker logic, synthetic SQLite and offline FC04."""
import json
import logging
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import MultiDict

import monitor_schedule as schedule
import modbus_utils
from models import Inverter, InverterStatus, MonitorHeartbeat
from monitor import MonitorWorker
from monitoring import NetworkProbes, Observation, ProbeResult, current_statuses
from tests.fake_modbus import FakeModbusTcpClient
from tests.helpers import post


@pytest.mark.parametrize('seconds', [1, 2, 30, 90, 3600, 86400])
def test_temporary_interval_expires_without_rewriting_or_extending_file(seconds):
    assert schedule.current(now=1000)['interval_seconds'] == 30
    result=schedule.configure(seconds,now=1000.25)
    assert result['interval_seconds']==seconds and result['expires_at']==2800.25
    path=schedule.policy_path(); original=path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert schedule.current(now=2800.24)['temporary']
    assert schedule.current(now=2800.25)['interval_seconds']==30
    assert schedule.current(now=3000,default=60)['interval_seconds']==30
    assert not schedule.current(now=999)['temporary']
    assert path.read_bytes()==original


@pytest.mark.parametrize('seconds', [0, -1, 86401, True, 1.5, '1', None])
def test_invalid_interval_never_creates_policy(seconds):
    with pytest.raises(ValueError): schedule.interval_value(seconds)
    assert not schedule.policy_path().exists()


def test_reset_and_new_override_have_explicit_server_lifetimes():
    first=schedule.configure(1,now=1000)
    second=schedule.configure(17,now=1010)
    assert second['revision']!=first['revision'] and second['expires_at']==2810
    result=schedule.configure(now=1020)
    assert result['interval_seconds']==30 and not result['temporary'] and result['expires_at'] is None


@pytest.mark.parametrize('kind', ['json','duration','duplicate','permissions','symlink','oversized'])
def test_corrupt_or_insecure_policy_falls_back_and_is_not_overwritten(tmp_path,kind):
    schedule.configure(1,now=1000)
    path=schedule.policy_path()
    if kind=='json': path.write_text('{bad')
    if kind=='duration':
        data=json.loads(path.read_text());data['expires_at']=999999;path.write_text(json.dumps(data))
    if kind=='duplicate': path.write_text('{"version":1,"version":1}')
    if kind=='permissions': path.chmod(0o644)
    if kind=='oversized': path.write_text('x'*5000)
    if kind=='symlink':
        saved=tmp_path/'saved.json';path.rename(saved);path.symlink_to(saved)
    before=path.read_bytes()
    result=schedule.current(now=1001)
    assert result['interval_seconds']==30 and result['error']=='monitor_schedule_invalid'
    with pytest.raises((ValueError,OSError)): schedule.configure(2,now=1001)
    assert path.read_bytes()==before


@pytest.mark.parametrize('role,expected', [(None,401),('Viewer',403),('Operator',403),('Administrator',200)])
def test_interval_route_requires_admin_and_does_not_probe(web,modbus,role,expected):
    module,client=web
    if role is None: client=module.app.test_client()
    else:
        module.db.session.get(module.User,1).role=role;module.db.session.commit()
    response=post(client,'/monitor/polling',dict(intent='temporary',interval_seconds='1'),headers={'Accept':'application/json'})
    assert response.status_code==expected
    assert schedule.policy_path().exists()==(role=='Administrator')
    if role=='Administrator':
        from monitor_web import log
        assert log.isEnabledFor(logging.INFO) and log.handlers
    assert modbus[1]==[]


@pytest.mark.parametrize('value', ['0','-1','1.5','01','86401','100000','nan','', '1;command'])
def test_http_validation_and_no_policy_changes(web,value):
    _,client=web
    assert post(client,'/monitor/polling',dict(intent='temporary',interval_seconds=value)).status_code==400
    assert not schedule.policy_path().exists()


def test_csrf_duplicate_fields_and_get_are_rejected(web):
    _,client=web
    assert client.post('/monitor/polling',data={'intent':'temporary','interval_seconds':'1'}).status_code==400
    assert client.get('/monitor/polling').status_code==405
    for field in ('intent','interval_seconds'):
        data=MultiDict([('intent','temporary'),('interval_seconds','1')]);data.add(field,'2')
        assert post(client,'/monitor/polling',data).status_code==400
    assert not schedule.policy_path().exists()


def test_api_html_reset_and_expiry_use_server_clock_without_schema_changes(web,modbus,monkeypatch):
    module,client=web
    clock=[time.time()]
    monkeypatch.setattr(schedule,'time',SimpleNamespace(time=lambda:clock[0]))
    before=set(module.db.metadata.tables)
    response=post(client,'/monitor/polling',dict(intent='temporary',interval_seconds='1'),headers={'Accept':'application/json'})
    assert response.json['polling']['expires_at']==clock[0]+1800
    original=schedule.policy_path().read_bytes()
    assert client.get('/api/statuses').json['polling']['interval_seconds']==1
    html=client.get('/').text
    assert 'id="polling-form"' in html and 'min="1"' in html and 'Застосувати на 30 хв' in html
    clock[0]+=1800
    assert client.get('/api/statuses').json['polling']['interval_seconds']==30
    assert schedule.policy_path().read_bytes()==original and set(module.db.metadata.tables)==before
    assert post(client,'/monitor/polling',dict(intent='default')).status_code==302
    assert not client.get('/api/statuses').json['polling']['temporary']
    for role in ('Operator','Viewer'):
        module.db.session.get(module.User,1).role=role;module.db.session.commit()
        html=client.get('/').text
        assert 'id="polling-status"' in html and 'id="polling-form"' not in html
    assert modbus[1]==[]


def test_worker_reads_new_fc04_data_at_requested_interval_and_restores_30(web,modbus,monkeypatch):
    module,_=web
    inv=Inverter(name='Offline fixture',group='A',ip_address='192.0.2.1',model='ingecon_sun_100tl')
    module.db.session.add(inv);module.db.session.commit()
    clock=[1000];power=[100];clients=[]
    import monitoring
    monkeypatch.setattr(monitoring,'time',SimpleNamespace(time=lambda:clock[0]))
    def factory(*args,**kwargs):
        client=FakeModbusTcpClient(*args,**kwargs)
        client.input_registers.update({20:2,29:power[0],52:100,53:10})
        clients.append(client);return client
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',factory)
    probes=NetworkProbes()
    monkeypatch.setattr(probes,'icmp',lambda _:ProbeResult(False))
    monkeypatch.setattr(probes,'tcp',lambda _:ProbeResult(True))
    schedule.configure(1,now=1000)
    worker=MonitorWorker(module.db.engine,probes,clock=lambda:clock[0],monotonic=lambda:clock[0])
    worker.run_cycle();power[0]=200;clock[0]=1001;worker.run_cycle()
    module.db.session.expire_all()
    status=module.db.session.get(InverterStatus,inv.id)
    assert status.state=='Online' and status.active_power_w==2000 and status.checked_at==1001
    assert worker.interval==1
    clock[0]=2800;worker.run_cycle();module.db.session.expire_all()
    assert worker.interval==30 and module.db.session.get(MonitorHeartbeat,1).interval_seconds==30
    assert all(client.calls==[('connect',),('read_input',20,16,1),('read_input',52,3,1)] for client in clients)


def test_wait_reacts_to_shorter_interval_and_expiry_without_waiting_old_period(web):
    module,_=web
    clock=[1000]
    class Stop:
        def is_set(self):return False
        def wait(self,delay):
            clock[0]+=delay
            if clock[0]==1001:schedule.configure(1,now=clock[0])
    worker=MonitorWorker(module.db.engine,probes=object(),clock=lambda:clock[0],
                         monotonic=lambda:clock[0],stop_event=Stop())
    worker._wait_for_next_cycle(1000,1000)
    assert clock[0]==1001 and worker.interval==1
    schedule.configure(86400,now=1000)
    clock[0]=2799
    worker._flush([]);worker._has_started=True
    worker._wait_for_next_cycle(1000,1000)
    module.db.session.expire_all()
    heartbeat=module.db.session.get(MonitorHeartbeat,1)
    assert clock[0]==2800 and worker.interval==30 and heartbeat.interval_seconds==30
    assert current_statuses(module.db.session,now=2800)['monitor']['alive']


def test_one_second_period_subtracts_time_spent_polling(web):
    module,_=web
    schedule.configure(1,now=1000)
    elapsed=[0.4];waits=[]
    class Stop:
        def is_set(self):return False
        def wait(self,delay):waits.append(delay);elapsed[0]+=delay
    worker=MonitorWorker(module.db.engine,probes=object(),clock=lambda:1000+elapsed[0],
                         monotonic=lambda:elapsed[0],stop_event=Stop())
    worker._wait_for_next_cycle(0,0.4)
    assert sum(waits)==pytest.approx(0.6) and elapsed[0]==pytest.approx(1)


def test_slow_cycle_reports_achievable_period_and_does_not_create_false_staleness(web):
    module,_=web
    module.db.session.add(Inverter(name='Slow mock',group='A',ip_address='192.0.2.1',model='ingecon_sun_100tl'))
    module.db.session.commit()
    clock=[1000];schedule.configure(1,now=1000)
    class SlowProbes:
        def poll(self,target):
            clock[0]+=70
            return Observation(target,clock[0],ProbeResult(True),ProbeResult(True),ProbeResult(True,registers=(0,)*19))
    worker=MonitorWorker(module.db.engine,SlowProbes(),clock=lambda:clock[0],monotonic=lambda:clock[0])
    worker.run_cycle();module.db.session.expire_all()
    result=current_statuses(module.db.session,now=1071)
    assert worker.interval==1 and result['monitor']['interval_seconds']==71
    assert result['monitor']['alive'] and not result['inverters'][0]['stale']
