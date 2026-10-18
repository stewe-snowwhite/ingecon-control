"""TASK-08: isolated SQLite, actual shared monitor, wire codec and mock Telegram."""
import asyncio
from contextlib import nullcontext
import os
from pathlib import Path
import sqlite3
import struct
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from demo_modbus import (MAGIC, VERSION, LoopbackGuard, Handler, decode, registers, response_pdu)
from demo_monitor import DemoProbes, DemoTarget, DemoWorker
from demo_monitor_store import (Base, DemoStore, Settings, Sample, Event, Subscription, Delivery,
                                HOST, PORT, event, load_value)
from demo_notifications import DemoNotificationBridge, DemoNotifications, subscribe
from models import db, User, Inverter, MonitorConfig, StateEvent, TelegramLink
from monitor import MonitorWorker
from monitoring import Target
from telegram_bot import check_schema
from telegram_ui import notification_batch
from tests.fake_modbus import Response
from tests.fake_telegram import FakeTelegramSession
from tests.helpers import post
from tests.test_telegram import asyncio_waker_only, FAKE_TOKEN


@pytest.fixture
def simulation(tmp_path,monkeypatch):
    import time
    now = [time.time()]
    monkeypatch.setattr(time,'time',lambda:now[0])
    store = DemoStore(tmp_path/'task08',initialize=True)
    clients = []
    class WireClient:
        def __init__(self,host,*,port,**kwargs):
            assert (host,port)==(HOST,PORT)
            self.calls,self.closed = [],False
            clients.append(self)
        def connect(self): return True
        def close(self): self.closed = True
        def read_holding_registers(self,address,*,count,device_id):
            self.calls.append(('read',address,count,device_id))
            scenario,load = store.settings()
            if scenario=='offline': raise TimeoutError
            pdu = response_pdu(struct.pack('>BHH',3,address,count),registers(scenario,load),device_id)
            return Response(list(struct.unpack('>'+('H'*count),pdu[2:])))
        def write_register(self,*args,**kwargs):
            raise AssertionError('No writes, even to the simulated device')
    probes = DemoProbes(timeout=0.1,client_factory=WireClient,connector=lambda *args,**kwargs:nullcontext())
    worker = DemoWorker(store,probes=probes,clock=lambda:now[0])
    def cycle(count=1):
        for _ in range(count):
            worker.run_cycle()
            now[0] += 5
        return store.snapshot(now=now[0])
    yield SimpleNamespace(store=store,worker=worker,cycle=cycle,now=now,clients=clients)
    store.close()


def test_shared_monitor_hysteresis_measurements_and_event_history(simulation,modbus):
    s = simulation
    assert s.cycle()['inverters'][0]['state']=='Unknown'
    data = s.cycle()
    assert data['inverters'][0]['state']=='Online' and data['equipment']=='Running'
    assert data['measurements']==dict(power_w=2500,voltage_v=230,current_a=10.9,frequency_hz=50,temperature_c=42.5)
    with s.store.sessions() as session:
        count = session.query(Event).count()
    s.cycle(3)
    with s.store.sessions() as session:
        assert session.query(Event).count()==count
        assert session.query(StateEvent).count()==1
    assert all(c.closed and c.calls==[('read',0,8,1)] for c in s.clients)
    assert modbus[1]==[]


def test_fault_loss_recovery_stop_and_load_follow_registers(simulation):
    s = simulation
    s.cycle(2)
    s.store.change('fault','75','admin')
    assert s.store.snapshot(now=s.now[0])['equipment']=='Running'  # No fake immediate status.
    fault = s.cycle()
    assert fault['equipment']=='Alarm' and fault['inverters'][0]['state']=='Online'
    assert fault['measurements']['temperature_c']==85
    s.store.change('offline','75','admin')
    states = [s.cycle()['inverters'][0]['state'] for _ in range(3)]
    assert states==['Degraded','Degraded','Offline']
    offline = s.store.snapshot(now=s.now[0])
    assert offline['measurements'] is None
    assert offline['inverters'][0]['checks']['tcp']['ok'] is True
    assert offline['inverters'][0]['checks']['modbus']['ok'] is False
    s.store.change('recovery','80','admin')
    assert s.cycle()['inverters'][0]['state']=='Degraded'
    restored = s.cycle()
    assert restored['inverters'][0]['state']=='Online' and restored['measurements']['power_w']==4000
    s.store.change('stopped','80','admin')
    stopped = s.cycle()
    assert stopped['inverters'][0]['state']=='Online' and stopped['equipment']=='Stopped'
    assert stopped['measurements']['power_w']==0


def test_stale_heartbeat_hides_live_values(simulation):
    s = simulation
    s.cycle(2)
    s.now[0] += 30
    data = s.store.snapshot(now=s.now[0])
    assert data['inverters'][0]['state']=='Unknown' and data['measurements'] is None
    assert not data['monitor']['alive']


def test_real_monitor_and_bot_reject_demo_database(simulation):
    with pytest.raises(RuntimeError,match='Demo database'):
        MonitorWorker(simulation.store.engine)._check_schema()
    with pytest.raises(ValueError,match='демо-БД'):
        check_schema(simulation.store.engine)
    simulation.worker._check_schema()
    assert not (set(Base.metadata.tables)&set(db.metadata.tables))
    assert 'user' not in inspect(simulation.store.engine).get_table_names()


def test_tampered_target_is_unknown_without_any_network_or_write(simulation,modbus):
    with simulation.store.sessions.begin() as session:
        session.get(Inverter,1).ip_address = '192.0.2.42'
    data = simulation.cycle()
    assert data['inverters'][0]['state']=='Unknown'
    assert not simulation.clients and not modbus[1]


@pytest.mark.parametrize('address',['192.0.2.1','::1','127.0.0.2'])
def test_demo_target_never_accepts_a_physical_or_other_endpoint(address):
    with pytest.raises(ValueError): DemoTarget(1,address,PORT,1,0,8,'demo_task08').validate()
    with pytest.raises(ValueError): Target(1,HOST,PORT,1,0,8,'demo_task08').validate()


@pytest.mark.parametrize('value',['-1','101','nan','inf','1e2','0x10','1.234','',None,True,'100.01'])
def test_invalid_load_rejected(value):
    with pytest.raises(ValueError): load_value(value)


@pytest.mark.parametrize('value',['0','50.25','100','100.00'])
def test_valid_load(value):
    assert load_value(value)==float(value)


@pytest.mark.parametrize('function',[1,2,4,5,6,15,16,22,23])
def test_simulator_rejects_all_unapproved_functions_and_writes(function):
    assert response_pdu(bytes([function])+b'\0'*4,registers('normal',50))==bytes([function|0x80,1])


def test_wire_handler_frames_read_response_without_socket(simulation):
    request = struct.pack('>HHHBBHH',71,0,6,1,3,0,8)
    class Connection:
        def __init__(self): self.buffer,self.sent = request,b''
        def settimeout(self,value): pass
        def recv(self,count):
            data,self.buffer = self.buffer[:count],self.buffer[count:]
            return data
        def sendall(self,data): self.sent += data
    connection = Connection()
    Handler(connection,('127.0.0.1',1),SimpleNamespace(store=simulation.store))
    tid,proto,length,unit = struct.unpack('>HHHB',connection.sent[:7])
    assert (tid,proto,length,unit)==(71,0,19,1)
    assert decode(list(struct.unpack('>8H',connection.sent[9:])))['power_w']==2500


@pytest.mark.parametrize('pdu',[b'\x03',struct.pack('>BHH',3,0,0),struct.pack('>BHH',3,7,2)])
def test_invalid_reads_rejected(pdu):
    assert response_pdu(pdu,registers('normal',50))[0]==0x83


@pytest.mark.parametrize('change',[(0,0),(1,99),(7,3),(2,-1),(2,1.5)])
def test_bad_test_map_rejected(change):
    values = registers('normal',50)
    values[change[0]]=change[1]
    with pytest.raises(ValueError): decode(values)


@pytest.mark.parametrize('address',[('192.0.2.42',502),(HOST,502),('8.8.8.8',443)])
def test_process_guard_blocks_any_non_simulator_destination(address):
    with pytest.raises(RuntimeError): LoopbackGuard()('socket.connect',(None,address))
    with pytest.raises(RuntimeError): LoopbackGuard(True)('socket.connect',(None,(HOST,PORT)))
    LoopbackGuard()('socket.connect',(None,(HOST,PORT)))
    LoopbackGuard(True)('socket.bind',(None,(HOST,PORT)))


@pytest.mark.parametrize('kind',['unmarked','hardlink','symlink','wrong-purpose','public-file'])
def test_storage_refuses_foreign_or_unsafe_files(tmp_path,simulation,kind):
    root = tmp_path/'foreign'
    root.mkdir(mode=0o700)
    path = root/'monitor.sqlite'
    if kind=='hardlink': os.link(simulation.store.path,path)
    elif kind=='symlink': path.symlink_to(simulation.store.path)
    else:
        path.write_bytes(simulation.store.path.read_bytes())
        path.chmod(0o600)
        if kind=='public-file': path.chmod(0o644)
        else:
            with sqlite3.connect(path) as connection:
                if kind=='unmarked': connection.execute('DROP TABLE demo_metadata')
                else: connection.execute("UPDATE demo_metadata SET value='legacy-demo'")
    before = path.read_bytes()
    with pytest.raises(ValueError): DemoStore(root,initialize=True)
    assert path.read_bytes()==before


@pytest.fixture
def demo_web(web,tmp_path):
    module,client = web
    module.app.config['MONITOR_DEMO_ROOT'] = tmp_path/'web-task08'
    return module,client


def test_web_admin_setup_html_csrf_commands_api_and_isolation(demo_web,modbus):
    module,client = demo_web
    module.db.session.add(Inverter(name='Physical sentinel',group='Real',ip_address='192.0.2.70'))
    module.db.session.commit()
    def production():
        return {name:module.db.session.execute(table.select()).all() for name,table in db.metadata.tables.items()}
    before = production()
    assert 'Демо' in client.get('/').get_data(as_text=True)
    assert client.get('/demo').status_code==200
    assert not Path(module.app.config['MONITOR_DEMO_ROOT']).exists()
    assert client.post('/demo/init').status_code==400
    assert post(client,'/demo/init').status_code==302
    assert client.get('/demo').status_code==200
    assert post(client,'/demo/scenario',dict(scenario='fault',load='87.25')).status_code==302
    data = client.get('/api/demo/status').json
    assert data['scenario']=='fault' and data['load']==87.25
    assert data['inverters'][0]['state']=='Unknown' and data['measurements'] is None
    assert data['name']=='DEMO-INVERTER-01'
    assert 'Physical sentinel' not in client.get('/demo').get_data(as_text=True)
    assert client.get('/api/statuses').json['counts']['total']==1
    assert all(e['kind']=='scenario' for e in client.get('/api/demo/status?type=scenario').json['events'])
    assert client.get('/api/demo/status?type=forged').status_code==400
    assert post(client,'/demo/scenario',dict(scenario='on',load='10')).status_code==400
    assert post(client,'/demo/scenario',dict(scenario='normal',load='NaN')).status_code==400
    assert production()==before and modbus[1]==[]


@pytest.mark.parametrize('role',['Viewer','Operator',None])
def test_demo_routes_enforce_admin_for_html_api_and_posts(demo_web,role):
    module,client = demo_web
    if role:
        db.session.get(User,1).role=role
        db.session.commit()
    else:
        with client.session_transaction() as session: session.clear()
    expected = 403 if role else 401
    for path in ('/demo','/api/demo/status'):
        assert client.get(path).status_code==expected
    for path in ('/demo/init','/demo/scenario','/demo/notifications'):
        assert post(client,path).status_code==expected
    assert not Path(module.app.config['MONITOR_DEMO_ROOT']).exists()


def test_demo_notification_opt_in_requires_confirmation_and_own_valid_link(demo_web):
    module,client = demo_web
    post(client,'/demo/init')
    assert post(client,'/demo/notifications',dict(action='enable')).status_code==400
    assert post(client,'/demo/notifications',dict(action='enable',confirm='demo-only')).status_code==400
    user = db.session.get(User,1)
    link = TelegramLink(telegram_id=1001,user_id=user.id,user_version=user.session_version,linked_at=0,last_event_id=0)
    db.session.add(link)
    db.session.commit()
    before = (link.notifications,link.last_event_id,link.user_version)
    assert post(client,'/demo/notifications',dict(action='enable',confirm='demo-only',telegram_id='999')).status_code==302
    with DemoStore(module.app.config['MONITOR_DEMO_ROOT']) as store:
        with store.sessions() as session:
            sub = session.get(Subscription,1)
            assert sub.telegram_id==1001 and sub.enabled
    assert (link.notifications,link.last_event_id,link.user_version)==before
    assert post(client,'/demo/notifications',dict(action='disable')).status_code==302
    assert client.get('/api/demo/status').json['notifications'] is False


@pytest.fixture
def demo_tg(simulation,web):
    module,_ = web
    user = db.session.get(User,1)
    link = TelegramLink(telegram_id=1001,user_id=user.id,user_version=user.session_version,linked_at=0,last_event_id=0)
    db.session.add(link)
    db.session.commit()
    api = FakeTelegramSession()
    bot = Bot(FAKE_TOKEN,session=api)
    notifier = DemoNotifications(simulation.store,db.engine,clock=lambda:simulation.now[0])
    return SimpleNamespace(sim=simulation,user=user,link=link,api=api,bot=bot,notifier=notifier,engine=db.engine)


def test_demo_telegram_observed_fault_offline_recovery_and_dedup(demo_tg):
    t = demo_tg
    t.sim.cycle(2)
    subscribe(t.sim.store,t.user,t.link,True,now=t.sim.now[0])
    for scenario,cycles in [('fault',1),('offline',3),('recovery',2)]:
        t.sim.store.change(scenario,'50','admin')
        t.sim.cycle(cycles)
        for _ in range(4): asyncio.run(notification_batch(t.notifier,t.bot))
    messages = t.api.messages
    assert any('Alarm' in m.text for m in messages)
    assert any('Offline' in m.text for m in messages)
    assert any('Online' in m.text for m in messages)
    assert all(m.text.startswith('[DEMO]') and m.chat_id==1001 for m in messages)
    count = len(messages)
    asyncio.run(notification_batch(t.notifier,t.bot))
    assert len(t.api.messages)==count
    with Session(t.engine) as session:
        assert session.query(StateEvent).count()==0
        assert not session.get(TelegramLink,1001).notifications


@pytest.mark.parametrize('revocation',['default','disable','logout','role','inactive','unlink','expired'])
def test_demo_notifications_require_separate_current_opt_in(demo_tg,revocation):
    t = demo_tg
    if revocation!='default': subscribe(t.sim.store,t.user,t.link,True,now=t.sim.now[0])
    t.sim.cycle(2)
    t.notifier.fanout()
    if revocation=='disable': subscribe(t.sim.store,t.user,t.link,False,now=t.sim.now[0])
    with Session(t.engine) as session,session.begin():
        user = session.get(User,1)
        if revocation=='logout': user.session_version+=1
        if revocation=='role': user.role='Operator'
        if revocation=='inactive': user.active=False
        if revocation=='unlink': session.delete(session.get(TelegramLink,1001))
    if revocation=='expired': t.sim.now[0]+=3601
    for _ in range(4): asyncio.run(notification_batch(t.notifier,t.bot))
    assert not t.api.messages


@pytest.mark.parametrize('failure',['network','forbidden','rate'])
def test_demo_delivery_failures_do_not_report_sent_or_change_production(demo_tg,failure):
    t = demo_tg
    with Session(t.engine) as session,session.begin():
        session.get(TelegramLink,1001).notifications = True
    subscribe(t.sim.store,t.user,t.link,True,now=t.sim.now[0])
    t.sim.cycle(2)
    method = SendMessage(chat_id=1001,text='mock')
    errors = {'network':TelegramNetworkError(method=method,message='timeout'),
              'forbidden':TelegramForbiddenError(method=method,message='blocked'),
              'rate':TelegramRetryAfter(method=method,message='slow down',retry_after=30)}
    t.api.failures.append(errors[failure])
    asyncio.run(notification_batch(t.notifier,t.bot))
    with t.sim.store.sessions() as session:
        row = session.scalar(select(Delivery))
        assert row.state=={'network':'unknown','forbidden':'failed','rate':'pending'}[failure]
        assert row.sent_at is None
    with Session(t.engine) as session:
        assert session.get(TelegramLink,1001).notifications is True


def test_bridge_does_not_create_demo_or_break_bot_when_missing_or_invalid(tmp_path,web):
    module,_ = web
    root = tmp_path/'absent'
    bridge = DemoNotificationBridge(db.engine,root)
    asyncio.run(bridge.deliver(Bot(FAKE_TOKEN,session=FakeTelegramSession())))
    assert not root.exists()
    root.mkdir(mode=0o700)
    (root/'monitor.sqlite').write_bytes(b'not-sqlite')
    (root/'monitor.sqlite').chmod(0o600)
    asyncio.run(bridge.deliver(Bot(FAKE_TOKEN,session=FakeTelegramSession())))
    assert bridge.failed and (root/'monitor.sqlite').read_bytes()==b'not-sqlite'


def test_default_bridge_is_isolated_from_real_runtime_files(tmp_path,web):
    bridge = DemoNotificationBridge(db.engine)
    assert bridge.directory==tmp_path/'monitor-demo'
    asyncio.run(bridge.deliver(Bot(FAKE_TOKEN,session=FakeTelegramSession())))
    assert not bridge.directory.exists()


def test_demo_enable_does_not_replay_old_events_and_recovery_does_not_retry_unknown(demo_tg):
    t = demo_tg
    t.sim.cycle(2)
    subscribe(t.sim.store,t.user,t.link,True,now=t.sim.now[0])
    asyncio.run(notification_batch(t.notifier,t.bot))
    assert not t.api.messages
    t.sim.store.change('fault','50','admin')
    t.sim.cycle()
    t.notifier.fanout()
    delivery_id,*_ = t.notifier.claim_delivery()
    t.notifier.recover()
    asyncio.run(notification_batch(t.notifier,t.bot))
    assert not t.api.messages
    with t.sim.store.sessions() as session:
        assert session.get(Delivery,delivery_id).state=='unknown'


def test_task05_cannot_control_task08_through_its_separate_login(web):
    module,_ = web
    isolated = module.create_app({'DEMO_MODE':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:'})
    assert 'demo_monitor_web' not in isolated.blueprints
    assert isolated.test_client().get('/demo').status_code==404


def test_shared_navigation_is_safe_for_running_app_without_new_routes(web):
    module,client = web
    # Old workers may load an updated shared template before an approved restart.
    module.app.config.pop('MONITOR_DEMO_AVAILABLE')
    response = client.get('/')
    assert response.status_code==200
    assert 'href="/demo"' not in response.get_data(as_text=True)


def test_parent_directory_alias_cannot_create_files_in_production(tmp_path,monkeypatch):
    import demo_monitor_store
    root = tmp_path/'project'
    production = root/'instance'/'production'
    sandbox = root/'instance'/'monitor-demo'
    production.mkdir(mode=0o700,parents=True)
    sandbox.mkdir(mode=0o700)
    sentinel = production/'preserved'
    sentinel.write_bytes(b'production-sentinel')
    monkeypatch.setattr(demo_monitor_store,'ROOT',root)
    with pytest.raises(ValueError,match='own private directory'):
        DemoStore(sandbox/'..'/'production',initialize=True)
    assert list(production.iterdir())==[sentinel] and sentinel.read_bytes()==b'production-sentinel'
