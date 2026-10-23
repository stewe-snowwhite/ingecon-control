import multiprocessing
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import modbus_utils
from models import (db, Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, StateEvent)
from monitor import MonitorWorker, database_url, parser
from monitoring import (NetworkProbes, Observation, ProbeResult, Target, apply_observation,
                        current_statuses, pending_notifications, target_for)
from operation_locks import OperationBusy, endpoint_key, resource_lock
from tests.fake_modbus import FakeModbusTcpClient, Response
from tests.helpers import post, confirmed_post


@pytest.fixture
def storage(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "monitor.sqlite"}')
    db.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        inv = Inverter(name='Mock', group='A', ip_address='192.0.2.1')
        session.add(inv)
        session.flush()
        session.add(MonitorConfig(inverter_id=inv.id, read_address=1000, read_count=2,
                                  profile='legacy_ingeteam', port=502, device_id=1))
    yield engine
    engine.dispose()


def observation(now, *, modbus=True, tcp=True, icmp=True, error=None, target=None):
    target = target or Target(1, '192.0.2.1', read_address=1000, read_count=2, profile='legacy_ingeteam')
    return Observation(target, now, ProbeResult(icmp, 2, None if icmp else 'icmp_timeout'),
                       ProbeResult(tcp, 3, None if tcp else 'tcp_unreachable'),
                       ProbeResult(modbus, 4, error or (None if modbus else 'modbus_timeout'), (16, 32767)))


def record(engine, data):
    with Session(engine) as session, session.begin():
        inv = session.get(Inverter, 1)
        status = apply_observation(session, inv, data, 90)
        state = status.state
    return state


def test_transitions_and_hysteresis(storage):
    states = [record(storage, observation(n*30, modbus=ok))
              for n,ok in enumerate([True,True,False,False,False,False,True,True,True])]
    assert states == ['Unknown','Online','Degraded','Degraded','Offline','Offline','Degraded','Online','Online']
    with Session(storage) as session:
        status = session.get(InverterStatus, 1)
        assert (status.successes, status.failures, status.last_success_at) == (2,0,240)
        assert status.latency_ms == 4
        events = session.scalars(select(StateEvent).order_by(StateEvent.id)).all()
        assert [event.state for event in events] == ['Online','Degraded','Offline','Degraded','Online']
        assert len(pending_notifications(session)) == 5


@pytest.mark.parametrize('icmp', [False,None])
def test_blocked_or_missing_icmp_does_not_prevent_online(storage, icmp):
    assert record(storage, observation(0,icmp=icmp)) == 'Unknown'
    assert record(storage, observation(30,icmp=icmp)) == 'Online'
    with Session(storage) as session:
        status = session.get(InverterStatus, 1)
        assert status.error_reason is None
        assert status.icmp_ok is icmp
        assert status.modbus_ok is True


def test_tcp_failure_with_valid_modbus_is_degraded_not_offline(storage):
    for now in (0,30,60,90):
        assert record(storage, observation(now,tcp=False)) == 'Degraded'
    with Session(storage) as session:
        status = session.get(InverterStatus,1)
        assert status.failures == 0 and status.last_success_at == 90


def test_unknown_configuration_and_busy_are_not_failures(storage):
    for now in (0,30,60):
        assert record(storage, observation(now,modbus=None,error='modbus_not_configured')) == 'Unknown'
    with Session(storage) as session:
        assert session.get(InverterStatus,1).failures == 0
        assert session.query(StateEvent).count() == 0
    assert record(storage, observation(90,modbus=None,error='modbus_busy')) == 'Unknown'


def test_stale_gap_resets_streaks(storage):
    record(storage,observation(0)); assert record(storage,observation(30)) == 'Online'
    assert record(storage,observation(150)) == 'Degraded'
    assert record(storage,observation(180)) == 'Online'
    record(storage,observation(210,modbus=False)); record(storage,observation(240,modbus=False))
    assert record(storage,observation(400,modbus=False)) == 'Degraded'
    assert record(storage,observation(430,modbus=False)) == 'Degraded'
    assert record(storage,observation(460,modbus=False)) == 'Offline'


def test_clock_rollback_resets_success_streak(storage):
    record(storage,observation(100)); record(storage,observation(130))
    assert record(storage,observation(90)) == 'Degraded'


def test_last_success_preserved_on_failures(storage):
    record(storage,observation(0)); record(storage,observation(30))
    record(storage,observation(60,modbus=False))
    with Session(storage) as session:
        status = session.get(InverterStatus,1)
        assert status.last_success_at == 30 and status.error_reason == 'modbus_timeout'


def test_unchanged_cycles_do_not_append_history(storage):
    record(storage,observation(0)); record(storage,observation(30))
    for now in range(60,600,30): record(storage,observation(now))
    with Session(storage) as session:
        assert session.query(InverterStatus).count() == 1
        assert session.query(StateEvent).count() == 1
        assert session.get(InverterStatus,1).checked_at == 570


@pytest.mark.parametrize('failure', ['offline','timeout','exception','invalid','read_error'])
def test_modbus_failures_and_close(monkeypatch,failure):
    fake = FakeModbusTcpClient('192.0.2.1')
    if failure == 'offline': fake.online = False
    if failure == 'read_error': fake.read_error = True
    if failure == 'invalid':
        monkeypatch.setattr(fake,'read_holding_registers',lambda *a,**k: Response([1]))
    if failure in ('timeout','exception'):
        def fail(*a,**k):
            if failure == 'timeout': raise TimeoutError
            from pymodbus.exceptions import ModbusException
            raise ModbusException('simulated')
        monkeypatch.setattr(fake,'read_holding_registers',fail)
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',lambda *a,**k: fake)
    result = NetworkProbes().modbus(Target(1,'192.0.2.1',read_address=1000,read_count=2))
    assert result.ok is False and result.error
    assert fake.closed
    assert not any(call[0]=='write' for call in fake.calls)


def test_unconfigured_modbus_never_constructs_client(modbus):
    result = NetworkProbes().modbus(Target(1,'192.0.2.1'))
    assert result.ok is None and result.error == 'modbus_not_configured'
    assert modbus[1] == []


def test_independent_probes_never_short_circuit(monkeypatch):
    probes = NetworkProbes()
    calls = []
    for name,ok in (('icmp',False),('tcp',False),('modbus',True)):
        def check(target, name=name, ok=ok):
            calls.append(name)
            return ProbeResult(ok)
        monkeypatch.setattr(probes,name,check)
    sample = probes.poll(Target(1,'192.0.2.1',read_address=1000))
    assert calls == ['icmp','tcp','modbus']
    assert sample.modbus.ok is True


@pytest.mark.parametrize('code,ok', [(0,True),(1,False),(2,None)])
def test_ping_is_mocked_and_bounded(monkeypatch,code,ok):
    monkeypatch.setattr('monitoring.shutil.which',lambda _: '/fake/ping')
    calls = []
    def runner(args,**kwargs):
        calls.append((args,kwargs))
        return SimpleNamespace(returncode=code)
    result = NetworkProbes(timeout=1,runner=runner).icmp(Target(1,'192.0.2.1'))
    assert result.ok is ok
    args, kwargs = calls[0]
    assert args[-2:] == ['--','192.0.2.1'] and kwargs['timeout'] == 1.2
    assert kwargs.get('shell',False) is False


def test_ping_timeout_and_missing_binary(monkeypatch):
    monkeypatch.setattr('monitoring.shutil.which',lambda _: '/fake/ping')
    def timeout(*a,**k): raise subprocess.TimeoutExpired('mock-ping',1)
    assert NetworkProbes(runner=timeout).icmp(Target(1,'192.0.2.1')).error == 'icmp_timeout'
    monkeypatch.setattr('monitoring.shutil.which',lambda _: None)
    assert NetworkProbes().icmp(Target(1,'192.0.2.1')).ok is None


@pytest.mark.parametrize('kind,reason', [(TimeoutError,'tcp_timeout'),(ConnectionRefusedError,'tcp_unreachable')])
def test_tcp_timeout_and_refusal(monkeypatch,kind,reason):
    def fail(*a,**k): raise kind
    monkeypatch.setattr('monitoring.socket.create_connection',fail)
    assert NetworkProbes().tcp(Target(1,'192.0.2.1')).error == reason


def test_tcp_socket_closed(monkeypatch):
    called = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): called.append('closed')
    def connect(address,timeout):
        called.append((address,timeout)); return Connection()
    monkeypatch.setattr('monitoring.socket.create_connection',connect)
    assert NetworkProbes(timeout=1).tcp(Target(1,'192.0.2.1')).ok
    assert called == [(('192.0.2.1',502),1),'closed']


@pytest.mark.parametrize('target', [Target(1,'invalid;command'), Target(1,'127.0.0.1'),
    Target(1,'192.0.2.1',port=0),Target(1,'192.0.2.1',device_id=0),
    Target(1,'192.0.2.1',read_address=65535,read_count=2),
    Target(1,'192.0.2.1',read_address=46,profile='legacy_ingeteam')])
def test_invalid_target_never_probes(target):
    # Socket/subprocess guards would raise if validation failed to stop polling.
    result = NetworkProbes().poll(target)
    assert result.modbus.ok is None and result.modbus.error == 'invalid_target_configuration'


def test_ping_execution_guard(monkeypatch):
    monkeypatch.setattr('monitoring.shutil.which',lambda _: '/fake/ping')
    with pytest.raises(AssertionError,match='Network'):
        NetworkProbes().icmp(Target(1,'192.0.2.1'))


class ScriptedProbes:
    def __init__(self, clock, outcomes):
        self.clock = clock
        self.outcomes = iter(outcomes)
        self.calls = []
    def poll(self,target):
        self.calls.append(target)
        return observation(self.clock[0],modbus=next(self.outcomes),target=target)


def test_worker_persists_heartbeat_status_and_transition_outbox(storage):
    clock = [1000]
    probes = ScriptedProbes(clock,[True,True,False,False,False,True,True])
    worker = MonitorWorker(storage,probes,clock=lambda:clock[0])
    for i in range(7):
        clock[0]=1000+i*30
        worker.run_cycle()
    with Session(storage) as session:
        assert session.get(MonitorHeartbeat,1).last_seen == 1180
        assert session.get(InverterStatus,1).state == 'Online'
        assert len(pending_notifications(session)) == 5
        assert current_statuses(session,now=1180)['inverters'][0]['power_limit_percent'] == 100.0


def test_api_freshness_rules(storage):
    clock=[1000]
    worker=MonitorWorker(storage,ScriptedProbes(clock,[True,True]),clock=lambda:clock[0])
    worker.run_cycle();clock[0]=1030;worker.run_cycle()
    with Session(storage) as session:
        assert current_statuses(session,now=1030)['inverters'][0]['state']=='Online'
        result=current_statuses(session,now=1121)
        assert result['inverters'][0]['state']=='Unknown' and result['inverters'][0]['stale']
        assert result['inverters'][0]['recorded_state']=='Online'
        assert result['monitor']['alive'] is False
        heartbeat=session.get(MonitorHeartbeat,1);heartbeat.last_seen=1121
        session.commit()
        assert current_statuses(session,now=1121)['inverters'][0]['reason']=='status_stale'
        heartbeat.stopped=True;session.commit()
        assert current_statuses(session,now=1121)['monitor']['reason']=='monitor_stopped'


def test_target_change_invalidates_old_status_and_in_flight_sample(storage):
    clock=[1000]
    probes=ScriptedProbes(clock,[True,True])
    worker=MonitorWorker(storage,probes,clock=lambda:clock[0]);worker.run_cycle();worker.run_cycle()
    old=observation(1000)
    with Session(storage) as session,session.begin():
        session.get(Inverter,1).ip_address='192.0.2.2'
    worker._flush([old])
    with Session(storage) as session:
        snapshot=current_statuses(session,now=1000)['inverters'][0]
        assert snapshot['state']=='Unknown' and snapshot['reason']=='target_changed'
        assert snapshot['last_success_at'] is None
        assert session.get(InverterStatus,1).target_key == old.target.key


def test_worker_once_stops_heartbeat_and_releases_singleton(storage):
    clock=[1000]
    probes=ScriptedProbes(clock,[True,True])
    worker=MonitorWorker(storage,probes,clock=lambda:clock[0])
    worker.run(once=True);worker.run(once=True)
    with Session(storage) as session:
        assert session.get(MonitorHeartbeat,1).stopped is True


def test_second_monitor_is_rejected_before_network(storage):
    probes=ScriptedProbes([1000],[])
    worker=MonitorWorker(storage,probes)
    from pathlib import Path
    with resource_lock('monitor:'+str(Path(storage.url.database).resolve())):
        with pytest.raises(OperationBusy): worker.run(once=True)
    assert probes.calls == []


def _child_lock(directory,key,output):
    import os
    os.environ['INGECON_LOCK_DIR']=directory
    try:
        with resource_lock(key): output.send('acquired')
    except OperationBusy:
        output.send('busy')


def test_cross_process_lock(tmp_path,monkeypatch):
    import os
    directory=str(tmp_path/'process-locks')
    monkeypatch.setenv('INGECON_LOCK_DIR',directory)
    key=endpoint_key('192.0.2.1',502)
    ctx=multiprocessing.get_context('fork')
    output,child_output=ctx.Pipe(duplex=False)
    with resource_lock(key):
        process=ctx.Process(target=_child_lock,args=(directory,key,child_output))
        process.start();process.join(5)
        assert process.exitcode==0 and output.poll(1) and output.recv()=='busy'
    with resource_lock(key): pass
    output.close()
    child_output.close()


def test_monitor_lock_blocks_legacy_helpers_without_client_construction(modbus):
    module,clients=modbus
    with resource_lock(endpoint_key('192.0.2.1',502)):
        assert module.read_command_status('192.0.2.1')==(None,None)
        assert not module.modbus_turn_on('192.0.2.1',allow_write=True).confirmed
        assert NetworkProbes().modbus(Target(1,'192.0.2.1',read_address=1000)).error=='modbus_busy'
    assert clients==[]


def test_monitor_database_paths_and_defaults():
    assert parser().parse_args([]).interval==30
    assert parser().parse_args(['--interval','60']).interval==60
    assert database_url('sqlite:///database.db').database.endswith('/instance/database.db')
    assert database_url('sqlite:////tmp/mock.sqlite').database=='/tmp/mock.sqlite'
    for uri in ('postgresql://localhost/db','sqlite:///:memory:'):
        with pytest.raises(ValueError): database_url(uri)


@pytest.mark.parametrize('interval', [0,-1,float('nan'),float('inf'),86401])
def test_bad_intervals(interval,storage):
    with pytest.raises(ValueError): MonitorWorker(storage,interval=interval)


@pytest.mark.parametrize('timeout', [0,-1,float('nan'),float('inf'),11])
def test_bad_timeouts(timeout):
    with pytest.raises(ValueError): NetworkProbes(timeout=timeout)


def test_dashboard_and_status_api_never_probe_and_preserve_roles(web,modbus):
    module,client=web
    inv=Inverter(name='Mock',group='A',ip_address='192.0.2.1',password='hidden-secret')
    module.db.session.add(inv);module.db.session.commit()
    assert client.get('/').status_code==200
    response=client.get('/api/statuses')
    assert response.status_code==200 and response.json['inverters'][0]['state']=='Unknown'
    assert 'hidden-secret' not in response.get_data(as_text=True)
    assert modbus[1]==[]
    assert module.app.test_client().get('/api/statuses').status_code==401
    user=module.db.session.get(module.User,1);user.role='Viewer';module.db.session.commit()
    assert client.get('/api/statuses').status_code==200
    assert post(client,'/test/1').status_code==403


def test_manual_check_only_queues_and_is_consumed(web,modbus):
    module,client=web
    inv=Inverter(name='Mock',group='A',ip_address='192.0.2.1')
    module.db.session.add(inv);module.db.session.commit()
    assert post(client,'/test/1').status_code==302
    assert modbus[1]==[]
    assert module.db.session.get(MonitorConfig,1).requested_at is not None
    clock=[time.time()+1]
    MonitorWorker(module.db.engine,ScriptedProbes(clock,[None]),clock=lambda:clock[0]).run_cycle()
    module.db.session.expire_all()
    assert module.db.session.get(MonitorConfig,1).requested_at is None


def test_delete_preserves_transition_history(web):
    module,client=web
    inv=Inverter(name='Mock',group='A',ip_address='192.0.2.1')
    module.db.session.add(inv);module.db.session.commit()
    record(module.db.engine,observation(0,modbus=False))
    assert confirmed_post(client,'/delete/1').status_code==200
    assert module.db.session.get(InverterStatus,1) is None
    assert module.db.session.scalar(select(StateEvent.id)) == 1


def test_cli_profile_is_explicit_and_per_inverter(web,modbus):
    module,_=web
    module.db.session.add(Inverter(name='Mock',group='A',ip_address='192.0.2.1'))
    module.db.session.commit()
    runner=module.app.test_cli_runner()
    result=runner.invoke(args=['configure-monitor','1','--read-address','1000','--read-count','2','--profile','legacy_ingeteam'])
    assert result.exit_code==0
    assert module.db.session.get(MonitorConfig,1).profile=='legacy_ingeteam'
    assert modbus[1]==[]
    result=runner.invoke(args=['configure-monitor','1','--read-address','46','--profile','legacy_ingeteam'])
    assert result.exit_code!=0


def test_concurrent_modbus_reads_for_same_endpoint_are_serialized(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    clients = []
    class SlowClient(FakeModbusTcpClient):
        def read_holding_registers(self, *args, **kwargs):
            started.set()
            assert release.wait(3)
            return super().read_holding_registers(*args, **kwargs)
    def factory(*args, **kwargs):
        client = SlowClient(*args, **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',factory)
    target = Target(1,'192.0.2.1',read_address=1000,read_count=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(NetworkProbes().modbus,target)
        try:
            assert started.wait(3)
            # A duplicate inventory row shares the endpoint lock across unit IDs.
            second = pool.submit(NetworkProbes().modbus,Target(2,'192.0.2.1',device_id=2,read_address=1000,read_count=2))
            assert second.result(timeout=3).error == 'modbus_busy'
        finally:
            release.set()
        assert first.result(timeout=3).ok is True
    assert len(clients) == 1 and clients[0].closed


def test_additive_schema_preserves_task02_data_and_login(tmp_path):
    from app import create_app
    from models import User, LoginThrottle
    path = tmp_path / 'task02-copy.sqlite'
    engine = create_engine(f'sqlite:///{path}')
    db.metadata.create_all(engine,tables=[Inverter.__table__,User.__table__,LoginThrottle.__table__])
    with Session(engine) as session,session.begin():
        user = User(username='existing',role='Viewer')
        user.set_password('existing-password-long')
        session.add(user)
        session.add(Inverter(name='Existing',group='Original',ip_address='192.0.2.1',password='synthetic-device-secret'))
        session.add(LoginThrottle(key='synthetic-existing-bucket', attempts=2, expires_at=9999999999))
        old_hash = user.password_hash
    db.metadata.create_all(engine)
    with Session(engine) as session:
        assert session.scalar(select(User)).password_hash == old_hash
        assert session.scalar(select(Inverter)).password == 'synthetic-device-secret'
        throttle = session.get(LoginThrottle, 'synthetic-existing-bucket')
        assert (throttle.attempts, throttle.expires_at) == (2, 9999999999)
    engine.dispose()
    app = create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':f'sqlite:///{path}',
                      'SECRET_KEY':'test-only-new-schema-external-key-32'})
    client = app.test_client()
    assert post(client,'/login',{'username':'existing','password':'existing-password-long'}).status_code == 302
    assert client.get('/api/statuses').json['inverters'][0]['name'] == 'Existing'
    assert client.get('/add').status_code == 403


def test_changed_profile_drops_success_streak_and_old_last_success(storage):
    record(storage,observation(0));record(storage,observation(30))
    new_target=Target(1,'192.0.2.2',read_address=46)
    sample=Observation(new_target,60,ProbeResult(True),ProbeResult(True),ProbeResult(True,registers=(50,)))
    assert record(storage,sample)=='Unknown'
    with Session(storage) as session:
        status=session.get(InverterStatus,1)
        assert status.successes==1 and status.last_success_at==60
        assert status.power_limit_percent is None


def test_worker_skips_sample_for_deleted_inverter(storage):
    worker=MonitorWorker(storage,ScriptedProbes([1000],[]))
    with Session(storage) as session,session.begin():
        session.delete(session.get(MonitorConfig,1));session.delete(session.get(Inverter,1))
    worker._flush([observation(1000)])
    with Session(storage) as session:
        assert session.query(InverterStatus).count()==0
        assert session.query(StateEvent).count()==0


def test_batch_updates_do_not_hold_transaction_during_probes(storage):
    from sqlalchemy import event
    transactions = [0]
    @event.listens_for(storage,'begin')
    def begin(connection): transactions[0] += 1
    @event.listens_for(storage,'commit')
    def commit(connection): transactions[0] -= 1
    @event.listens_for(storage,'rollback')
    def rollback(connection): transactions[0] -= 1
    class Probes:
        def poll(self,target):
            assert transactions[0]==0
            return observation(1000,target=target)
    MonitorWorker(storage,Probes()).run_cycle()
    assert transactions[0]==0


def test_schema_missing_fails_before_network(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path / "empty.sqlite"}')
    probes=ScriptedProbes([1000],[])
    with pytest.raises(RuntimeError,match='schema missing'):
        MonitorWorker(engine,probes).run(once=True)
    assert probes.calls==[]
    engine.dispose()


def test_default_monitor_uses_only_reads(modbus,monkeypatch):
    monkeypatch.setattr('monitoring.shutil.which',lambda _: '/fake/ping')
    probes=NetworkProbes(runner=lambda *a,**k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(probes,'tcp',lambda target: ProbeResult(True))
    sample=probes.poll(Target(1,'192.0.2.1',read_address=1000,read_count=2))
    assert sample.modbus.ok is True
    assert modbus[1][0].calls==[('connect',),('read',1000,2,1)]


def test_notification_acknowledgement_is_idempotent(storage):
    from monitoring import acknowledge_notification
    record(storage,observation(0,modbus=False))
    with Session(storage) as session,session.begin():
        event=pending_notifications(session)[0]
        assert acknowledge_notification(session,event.id,now=1000)
        assert not acknowledge_notification(session,event.id,now=1001)
        assert pending_notifications(session)==[]
    with Session(storage) as session:
        assert session.query(StateEvent).count()==1


def test_dashboard_renders_cached_measurements_without_polling(web, modbus):
    module, client = web
    module.db.session.add(Inverter(name='Cached', group='A', ip_address='192.0.2.1'))
    module.db.session.commit()
    module.db.session.add(MonitorConfig(inverter_id=1, read_address=1000, read_count=2,
                                       profile='legacy_ingeteam', port=502, device_id=1))
    module.db.session.commit()
    clock = [time.time()-2]
    worker = MonitorWorker(module.db.engine, ScriptedProbes(clock,[True,True]), clock=lambda:clock[0])
    worker.run_cycle(); clock[0] += 1; worker.run_cycle()
    module.db.session.expire_all()
    html = client.get('/').get_data(as_text=True)
    assert 'Online' in html and '100%' in html
    assert 'Остання успішна (UTC)' in html and 'Затримка Modbus (мс)' in html
    assert 'class="badge online">Монітор активний' in html and modbus[1] == []


def test_status_api_group_filter_is_db_only(web,modbus):
    module,client=web
    for group in ('A','B'):
        module.db.session.add(Inverter(name=group,group=group,ip_address='192.0.2.1'))
    module.db.session.commit()
    result=client.get('/api/statuses?group=A').json
    assert [row['group'] for row in result['inverters']]==['A']
    assert modbus[1]==[]


def test_tcp_does_not_overlap_with_modbus_endpoint_lock(monkeypatch):
    with resource_lock(endpoint_key('192.0.2.1',502)):
        result=NetworkProbes().tcp(Target(1,'192.0.2.1'))
    assert result.ok is None and result.error=='tcp_busy'


def test_delete_respects_foreign_keys_and_keeps_history(web):
    from sqlalchemy import text
    module,client=web
    module.db.session.add(Inverter(name='Mock',group='A',ip_address='192.0.2.1'))
    module.db.session.commit()
    module.db.session.add(MonitorConfig(inverter_id=1))
    module.db.session.commit()
    record(module.db.engine,observation(0,modbus=False))
    module.db.session.execute(text('PRAGMA foreign_keys=ON'))
    module.db.session.commit()
    assert module.db.session.scalar(text('PRAGMA foreign_keys')) == 1
    assert confirmed_post(client,'/delete/1').status_code == 200
    assert module.db.session.get(InverterStatus,1) is None
    assert module.db.session.get(MonitorConfig,1) is None
    assert module.db.session.scalar(select(StateEvent.id)) == 1

@pytest.mark.parametrize('poll_duration,expected_wait', [(0,30),(70,1)])
def test_worker_interval_and_overrun_without_sleep(storage,poll_duration,expected_wait):
    wall=[1000]
    monotonic=[0]
    waits=[]
    class Stop:
        stopped=False
        def is_set(self): return self.stopped
        def wait(self,delay):
            waits.append(delay)
            wall[0]+=delay; monotonic[0]+=delay
            self.stopped=sum(waits)>=expected_wait
    class Probes:
        def poll(self,target):
            wall[0]+=poll_duration;monotonic[0]+=poll_duration
            return observation(wall[0],target=target)
    worker=MonitorWorker(storage,Probes(),stop_event=Stop(),clock=lambda:wall[0],monotonic=lambda:monotonic[0])
    worker.run()
    assert sum(waits)==expected_wait and all(0 < delay <= 1 for delay in waits)
    with Session(storage) as session:
        assert session.get(MonitorHeartbeat,1).stopped
        assert session.get(MonitorHeartbeat,1).cycle_duration_seconds==poll_duration


def test_request_created_during_probe_remains_pending(storage):
    clock=[1000]
    class Probes:
        def poll(self,target):
            with Session(storage) as session,session.begin():
                session.get(MonitorConfig,1).requested_at=1010
            return observation(clock[0],target=target)
    MonitorWorker(storage,Probes(),clock=lambda:clock[0]).run_cycle()
    with Session(storage) as session:
        assert session.get(MonitorConfig,1).requested_at==1010
