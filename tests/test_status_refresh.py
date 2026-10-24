"""Status uses monitor-mediated FC04 reads; fake Telegram/Modbus, no sockets."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, User
from monitor import MonitorWorker
from monitoring import NetworkProbes, ProbeResult, target_for
import monitor_refresh
import modbus_utils
import telegram_ui
from tests.fake_modbus import FakeModbusTcpClient
from tests.test_telegram import asyncio_waker_only, tg
from tests.test_monitoring import storage


@pytest.fixture
def ready(tg,monkeypatch,request):
    count=getattr(request,'param',3)
    with Session(tg.engine) as session,session.begin():
        for number in range(1,count+1):
            inv=session.get(Inverter,number)
            if inv is None:
                inv=Inverter(id=number,name=f'Mock {number}',group='C',ip_address=f'192.0.2.{number}')
                session.add(inv)
            inv.model='ingecon_sun_100tl'
            config=session.get(MonitorConfig,number)
            if config is None:
                config=MonitorConfig(inverter_id=number,port=502,device_id=1,read_address=20,read_count=16,profile='ingecon_100tl_input')
                session.add(config)
            session.flush()
            status=session.get(InverterStatus,number)
            if status is None:
                status=InverterStatus(inverter_id=number,checked_at=tg.clock[0],state='Online',modbus_ok=True,tcp_ok=True)
                session.add(status)
            status.target_key=target_for(inv,config).key
            status.active_power_w=1000;status.successes=2
    tg.link();tg.clock[0]+=1
    clients=[]
    def factory(host,**kwargs):
        client=FakeModbusTcpClient(host,**kwargs)
        client.input_registers.update({20:2,29:int(host.split('.')[-1])*100,52:100,53:10})
        clients.append(client);return client
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',factory)
    monkeypatch.setattr('monitoring.time',SimpleNamespace(time=lambda:tg.clock[0]))
    probes=NetworkProbes()
    monkeypatch.setattr(probes,'icmp',lambda _:ProbeResult(False))
    monkeypatch.setattr(probes,'tcp',lambda _:ProbeResult(True))
    worker=MonitorWorker(tg.engine,probes,clock=lambda:tg.clock[0])
    return tg,worker,clients,count


@pytest.mark.parametrize('ready',[3,26],indirect=True)
@pytest.mark.parametrize('entry',['/status','menu:status'])
def test_status_waits_for_new_fc04_generation_for_entire_fleet(ready,monkeypatch,entry):
    tg,worker,clients,count=ready
    async def poll(_):
        assert tg.engine.pool.checkedout()==0
        with Session(tg.engine) as session:
            assert all(c.requested_at==tg.clock[0] for c in session.scalars(select(MonitorConfig)))
        tg.clock[0]+=1;worker.run_cycle()
    monkeypatch.setattr(telegram_ui,'status_sleep',poll)
    tg.feed(text=entry) if entry.startswith('/') else tg.feed(data=entry)
    messages=tg.api.messages
    assert messages[0].text==f'Оновлюю показники {count} інверторів…'
    assert f'перевірено {count}/{count}' in messages[-1].text
    expected=f'{sum(range(1,count+1)):.2f}'.replace('.',',')+' кВт'
    assert 'Поточна генерація: '+expected in messages[-1].text
    assert 'Остання відома' not in messages[-1].text
    assert telegram_ui.utc(tg.clock[0]) in messages[-1].text
    assert len(clients)==count
    assert all(client.calls==[('connect',),('read_input',20,16,1),('read_input',52,3,1)] and client.closed for client in clients)
    with Session(tg.engine) as session:assert all(c.requested_at is None for c in session.scalars(select(MonitorConfig)))


def test_timeout_does_not_call_old_generation_current(ready,monkeypatch):
    tg,_,clients,_=ready
    monkeypatch.setattr(telegram_ui,'STATUS_WAIT_SECONDS',0)
    tg.feed(data='menu:status')
    text=tg.api.messages[-1].text
    assert 'Свіже опитування ще не завершено (0/3)' in text
    assert 'Остання відома генерація: 3,00 кВт' in text and 'Поточна генерація' not in text
    assert 'Виміри:' in text and clients==[]


def test_partial_completion_is_explicit_and_does_not_fake_fleet_refresh(ready,monkeypatch):
    tg,worker,clients,_=ready
    monkeypatch.setattr(telegram_ui,'STATUS_WAIT_SECONDS',.05)
    called=[]
    async def partial(_):
        if not called:
            called.append(True);tg.clock[0]+=1
            with Session(tg.engine) as session:
                target=target_for(session.get(Inverter,1),session.get(MonitorConfig,1))
            worker._flush([worker.probes.poll(target)])
        await asyncio.sleep(.01)
    monkeypatch.setattr(telegram_ui,'status_sleep',partial)
    tg.feed('/status')
    assert '(1/3)' in tg.api.messages[-1].text and 'Остання відома генерація' in tg.api.messages[-1].text
    assert len(clients)==1


def test_failed_modbus_read_does_not_reuse_old_power(ready,monkeypatch):
    tg,worker,clients,_=ready
    original=modbus_utils.ModbusTcpClient
    def failed(*args,**kwargs):
        client=original(*args,**kwargs);client.read_error=True;return client
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',failed)
    async def poll(_):tg.clock[0]+=1;worker.run_cycle()
    monkeypatch.setattr(telegram_ui,'status_sleep',poll)
    tg.feed('/status')
    text=tg.api.messages[-1].text
    assert 'перевірено 3/3' in text and 'немає актуальних даних' in text
    assert '3,00 кВт' not in text and len(clients)==3


def test_monitor_stopped_returns_immediately_without_false_refresh(ready):
    tg,_,clients,_=ready
    with Session(tg.engine) as session,session.begin():session.get(MonitorHeartbeat,1).stopped=True
    tg.feed('/status')
    assert 'Монітор неактивний' in tg.api.messages[-1].text
    assert all(not message.text.startswith('Оновлюю') for message in tg.api.messages) and clients==[]
    with Session(tg.engine) as session:assert all(c.requested_at is None for c in session.scalars(select(MonitorConfig)))


@pytest.mark.parametrize('change',['unlink','edit'])
def test_authorization_and_inventory_rechecked_while_waiting(ready,monkeypatch,change):
    tg,_,clients,_=ready
    async def change_state(_):
        if change=='unlink':tg.store.unlink(1)
        else:
            with Session(tg.engine) as session,session.begin():session.get(Inverter,1).ip_address='192.0.2.100'
    monkeypatch.setattr(telegram_ui,'status_sleep',change_state)
    tg.feed('/status')
    text=tg.api.messages[-1].text
    assert ('Спочатку отримайте код' in text if change=='unlink' else 'змінилися під час оновлення' in text)
    assert 'Поточна генерація' not in text and clients==[]


def test_unlinked_status_cannot_queue_any_checks(tg):
    tg.feed(data='menu:status')
    with Session(tg.engine) as session:assert all(c.requested_at is None for c in session.scalars(select(MonitorConfig)))


def test_repeat_requests_share_markers_and_one_monitor_cycle(ready):
    tg,worker,clients,_=ready
    first,_=tg.store.request_status_refresh(1001)
    tg.clock[0]+=1
    second,_=tg.store.request_status_refresh(1001)
    with Session(tg.engine) as session:assert all(c.requested_at==second.requested_at for c in session.scalars(select(MonitorConfig)))
    tg.clock[0]+=1;worker.run_cycle()
    assert tg.store.status_refresh_progress(1001,first)['state']=='refreshed'
    assert tg.store.status_refresh_progress(1001,second)['state']=='refreshed' and len(clients)==3


def test_monitor_wakes_for_pending_request_before_30_seconds_with_global_bound(storage):
    with Session(storage) as session,session.begin():session.get(MonitorConfig,1).requested_at=1000
    clock=[0.0]
    class Stop:
        def is_set(self):return False
        def wait(self,delay):clock[0]+=delay
    worker=MonitorWorker(storage,probes=object(),clock=lambda:1000+clock[0],monotonic=lambda:clock[0],stop_event=Stop())
    worker._wait_for_next_cycle(0,0)
    assert clock[0]==monitor_refresh.MIN_REQUEST_INTERVAL and clock[0]<30


def test_missing_config_gets_only_same_safe_default_target(tg):
    tg.link();tg.clock[0]+=1
    with Session(tg.engine) as session,session.begin():session.delete(session.get(MonitorConfig,1))
    ticket,result=tg.store.request_status_refresh(1001)
    with Session(tg.engine) as session:
        inv=session.get(Inverter,1);config=session.get(MonitorConfig,1)
        assert config.port==502 and config.device_id==1 and config.read_address is None
        assert target_for(inv,config).key==ticket.targets[1]
    assert result['state']=='pending'
