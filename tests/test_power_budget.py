"""Fleet controller against in-memory SQLite and mock Modbus only."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from commands import ControlBlocked, execute_devices
from models import db, CommandAudit, Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, User
from monitoring import target_for
from operation_locks import resource_lock
import power_budget as budget
from power_controller import PowerController
from tests.helpers import post
from tests.test_inverter_controls import authorized


def row(ident,power=50000,cap=100,online=True):
    return dict(id=ident,active_power_w=power,power_limit_percent=cap,stale=not online,
                state='Online' if online else 'Offline',checks={'modbus':{'ok':online}})


def test_equal_400kw_on_twenty_100kw_units():
    plan=budget.allocation(400000,100000,[row(n) for n in range(1,21)])
    assert plan['percent']=={str(n):20 for n in range(1,21)}


def test_weak_units_release_capacity_but_sum_of_ceilings_stays_within_budget():
    rows=[row(1,power=10000)]+[row(n) for n in range(2,21)]
    plan=budget.allocation(400000,100000,rows)
    assert plan['percent']['1']==11
    assert sum(plan['percent'].values())==400
    assert set(plan['percent'][str(n)] for n in range(2,21))=={20,21}


def test_largest_remainder_approaches_target_without_exceeding_it():
    plan=budget.allocation(400500,100000,[row(n) for n in range(1,24)])
    assert sum(plan['percent'].values())==400
    assert max(plan['percent'].values())-min(plan['percent'].values())==1


def test_offline_unit_reserves_full_nominal_not_zero_or_old_percent():
    plan=budget.allocation(400000,100000,[row(1,cap=20,online=False)]+[row(n) for n in range(2,21)])
    assert plan['reserved_w']==100000 and plan['percent']['1']==100
    assert sum(plan['percent'].values())<=400
    with pytest.raises(ValueError):budget.allocation(50000,100000,[row(1,online=False),row(2)])


def test_low_sun_does_not_invent_generation_and_recovery_is_gradual():
    plan=budget.allocation(400000,100000,[row(n,power=0,cap=20) for n in range(1,21)])
    assert set(plan['percent'].values())=={1}
    recovery=budget.allocation(400000,100000,[row(n,power=1000,cap=1) for n in range(1,21)],plan['estimates'])
    assert set(recovery['percent'].values())=={2}


@pytest.mark.parametrize('text,value',[('400',400000),('400,5',400500),('400.125',400125),(' 1 ',1000)])
def test_kw_parser(text,value):assert budget.watts(text)==value


@pytest.mark.parametrize('text',['0','-1','NaN','inf','1e3','1.0001','100001','37%','True',''])
def test_invalid_kw(text):
    with pytest.raises(ValueError):budget.watts(text)


@pytest.fixture
def fleet(web,authorized):
    module,client=web
    clock=[10000.0]
    for number in range(1,21):
        inv=Inverter(id=number,name=f'Mock {number}',group='A',model='ingecon_sun_100tl',ip_address=f'192.0.2.{number}')
        db.session.add(inv);db.session.flush()
        config=MonitorConfig(inverter_id=number,port=502,device_id=number)
        db.session.add(config);db.session.flush()
        db.session.add(InverterStatus(inverter_id=number,target_key=target_for(inv,config).key,
            state='Online',checked_at=clock[0],last_success_at=clock[0],tcp_ok=True,modbus_ok=True,
            active_power_w=50000,power_limit_percent=100,operating_state_code=2))
    db.session.add(MonitorHeartbeat(id=1,owner='mock',started_at=clock[0],last_seen=clock[0],interval_seconds=30,stopped=False))
    db.session.commit()
    engine=db.engine
    service=budget.BudgetService(engine,clock=lambda:clock[0])
    def enable(target=400000):
        intent,_=service.preview(1,target)
        assert service.confirm(1,intent)=='configured'
        assert authorized==[]
    def observe(overrides=None):
        overrides=overrides or {}
        clock[0]+=30
        pending={int(r['id']):r['percent'] for r in budget.load()['state'].get('pending',[])}
        with Session(engine) as session,session.begin():
            session.get(MonitorHeartbeat,1).last_seen=clock[0]
            for status in session.scalars(select(InverterStatus)):
                status.checked_at=clock[0]
                if status.inverter_id in pending:
                    status.power_limit_percent=pending[status.inverter_id]
                    status.active_power_w=min(status.active_power_w,pending[status.inverter_id]*1000)
                for key,value in overrides.get(status.inverter_id,{}).items():setattr(status,key,value)
        db.session.expire_all()
    return SimpleNamespace(module=module,client=client,clock=clock,engine=engine,service=service,
        clients=authorized,enable=enable,observe=observe,
        worker=lambda armed=False:PowerController(engine,enable_writes=armed,clock=lambda:clock[0]))


def test_default_worker_and_preview_never_send_commands(fleet):
    intent,payload=fleet.service.preview(1,400000)
    assert not budget.load() and fleet.clients==[]
    assert fleet.service.confirm(1,intent)=='configured'
    assert fleet.worker().run_cycle()=='dry_run'
    assert budget.load()['state']['desired']=={str(n):20 for n in range(1,21)}
    assert fleet.clients==[]


def test_missing_power_reserves_budget_without_raising_that_inverters_limit(fleet):
    fleet.enable()
    with Session(fleet.engine) as session,session.begin():
        status=session.get(InverterStatus,1)
        status.active_power_w=None;status.power_limit_percent=20
    fleet.worker(True).run_cycle()
    assert len(fleet.clients)==19
    assert all(client.host!='192.0.2.1' for client in fleet.clients)
    assert budget.load()['state']['reserved_w']==100000


def test_reductions_readback_and_deduplication(fleet):
    fleet.enable();worker=fleet.worker(True)
    assert worker.run_cycle()=='awaiting_readback' and len(fleet.clients)==20
    assert all(client.calls==[('connect',),('write_multiple',1000,[3,6553],number)]
               for number,client in enumerate(fleet.clients,1))
    worker.run_cycle();assert len(fleet.clients)==20
    fleet.observe();assert worker.run_cycle()=='active' and len(fleet.clients)==20
    fleet.observe();worker.run_cycle();assert len(fleet.clients)==20
    with Session(fleet.engine) as session:
        assert len(session.scalars(select(CommandAudit).where(CommandAudit.source=='automation')).all())==20


def test_reduce_weak_unit_then_raise_others_only_after_readback(fleet):
    fleet.enable();worker=fleet.worker(True);worker.run_cycle();fleet.observe();worker.run_cycle()
    fleet.clients.clear()
    fleet.observe(**{})
    with Session(fleet.engine) as session,session.begin():session.get(InverterStatus,1).active_power_w=10000
    assert worker.run_cycle()=='awaiting_readback'
    assert len(fleet.clients)==1 and fleet.clients[0].calls[1][2]==[3,11*32767//100]
    worker.run_cycle();assert len(fleet.clients)==1
    fleet.observe();worker.run_cycle()
    assert len(fleet.clients)>1
    assert all(client.calls[1][2][1]<=21*32767//100 for client in fleet.clients[1:])


@pytest.mark.parametrize('change',['monitor','stale','model','owner','role','authorization'])
def test_stale_or_revoked_context_cannot_write(fleet,change):
    fleet.enable()
    with Session(fleet.engine) as session,session.begin():
        if change=='monitor':session.get(MonitorHeartbeat,1).stopped=True
        elif change=='stale':fleet.clock[0]+=100
        elif change=='model':session.get(Inverter,1).model='unknown'
        elif change=='owner':session.get(User,1).active=False
        elif change=='role':session.get(User,1).role='Operator'
        else:
            import inverter_controls
            inverter_controls.set_authorization(False)
    fleet.worker(True).run_cycle()
    assert fleet.clients==[] and budget.load()['state']['phase']=='paused'


def test_restart_waits_for_inflight_readback_and_does_not_replay(fleet):
    fleet.enable();fleet.worker(True).run_cycle()
    assert len(fleet.clients)==20
    restarted=fleet.worker(True)
    restarted.run_cycle();assert len(fleet.clients)==20
    fleet.observe();assert restarted.run_cycle()=='active' and len(fleet.clients)==20
    assert budget.status(now=fleet.clock[0]+91)['phase']=='stale'


def test_readback_during_request_is_not_confirmation_after_completion(fleet,monkeypatch):
    import power_controller
    execute=power_controller.execute_devices
    def delayed(user,action,inverters,value,**kwargs):
        result=execute(user,action,inverters,value,**kwargs)
        before=fleet.clock[0]
        fleet.clock[0]+=2
        with Session(fleet.engine) as session,session.begin():
            status=session.get(InverterStatus,inverters[0].id)
            status.checked_at=before+1
            status.power_limit_percent=value
            status.active_power_w=value*1000
        return result
    monkeypatch.setattr(power_controller,'execute_devices',delayed)
    fleet.enable();worker=fleet.worker(True);worker.run_cycle()
    assert len(fleet.clients)==20
    assert worker.run_cycle()=='awaiting_readback'
    assert len(budget.load()['state']['pending'])==20 and len(fleet.clients)==20


def test_unknown_result_and_mismatched_readback_require_manual_reconfirmation(fleet,monkeypatch):
    import power_controller
    calls=[]
    def unknown(*args,**kwargs):
        calls.append(True)
        return [dict(outcome='unconfirmed',message='mock timeout')]
    monkeypatch.setattr(power_controller,'execute_devices',unknown)
    fleet.enable();worker=fleet.worker(True);worker.run_cycle()
    assert len(calls)==1
    worker.run_cycle();assert len(calls)==1
    fleet.observe({1:{'power_limit_percent':100}})
    assert worker.run_cycle()=='paused'
    fleet.observe();worker.run_cycle();assert len(calls)==1


def test_manual_limits_blocked_until_explicit_disable_and_stop_remains_available(fleet):
    fleet.enable()
    user=db.session.get(User,1);inv=db.session.get(Inverter,1)
    for action in ('on','limit','limit_max'):
        with pytest.raises(ControlBlocked):execute_devices(user,action,[inv],37,writes_enabled=True)
    assert fleet.clients==[]
    execute_devices(user,'off',[inv],writes_enabled=True)
    assert fleet.clients[0].calls[1]==('write',1000,5,1)
    intent,_=fleet.service.preview(1)
    assert fleet.service.confirm(1,intent)=='configured'
    assert len(fleet.clients)==1
    execute_devices(user,'limit',[inv],37,writes_enabled=True)
    assert len(fleet.clients)==2


def test_preview_expiry_one_time_and_snapshot_changes(fleet):
    intent,_=fleet.service.preview(1,400000)
    fleet.clock[0]+=120
    assert fleet.service.confirm(1,intent)=='invalidated' and not budget.load()
    intent,_=fleet.service.preview(1,400000)
    with Session(fleet.engine) as session,session.begin():session.get(Inverter,1).ip_address='192.0.2.100'
    assert fleet.service.confirm(1,intent)=='invalidated'
    with pytest.raises(ValueError):fleet.service.confirm(1,intent)


@pytest.mark.parametrize('kind',['json','mode','oversized','symlink'])
def test_insecure_policy_is_not_overwritten_and_commands_fail_closed(fleet,kind,tmp_path):
    fleet.enable();path=budget.policy_path()
    if kind=='json':path.write_text('{bad')
    elif kind=='mode':path.chmod(0o644)
    elif kind=='oversized':path.write_text('x'*262145)
    else:
        saved=tmp_path/'saved.json';path.rename(saved);path.symlink_to(saved)
    before=path.read_bytes()
    with pytest.raises((ValueError,OSError)):budget.load()
    with pytest.raises((ValueError,OSError)):
        execute_devices(db.session.get(User,1),'limit',[db.session.get(Inverter,1)],37,writes_enabled=True)
    assert fleet.clients==[] and path.read_bytes()==before


def test_website_auth_csrf_confirmation_and_status(fleet):
    client=fleet.client
    assert client.get('/power-budget').status_code==200
    assert client.post('/power-budget/preview',data={'action':'enable','target_kw':'400'}).status_code==400
    response=post(client,'/power-budget/preview',dict(action='enable',target_kw='400',rated_kw='100',acknowledge_nominal='yes'))
    assert response.status_code==200 and '400 кВт' in response.text and '#20 Mock 20' in response.text
    import re
    intent=re.search('name="intent" value="([^"]+)"',response.text).group(1)
    assert not budget.load() and fleet.clients==[]
    assert post(client,'/power-budget/confirm',dict(intent=intent,decision='confirm')).status_code==302
    assert client.get('/power-budget/status').json['target_w']==400000
    assert post(client,'/power-budget/confirm',dict(intent=intent,decision='confirm')).status_code==400
    assert fleet.clients==[]


@pytest.mark.parametrize('role,status',[(None,401),('Viewer',403),('Operator',403)])
def test_web_configuration_requires_administrator(fleet,role,status):
    client=fleet.client
    if role is None:client=fleet.module.app.test_client()
    else:
        db.session.get(User,1).role=role;db.session.commit()
    assert post(client,'/power-budget/preview',dict(action='disable')).status_code==status
    assert not budget.load()


def test_long_polling_interval_is_rejected_while_automation_is_active(fleet):
    fleet.enable()
    assert post(fleet.client,'/monitor/polling',dict(intent='temporary',interval_seconds='60')).status_code==400
    assert post(fleet.client,'/monitor/polling',dict(intent='temporary',interval_seconds='1')).status_code==302
