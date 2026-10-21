"""Owner-authorized control paths. All sockets/DNS/subprocesses remain forbidden."""
import json
import re
from types import SimpleNamespace
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import inverter_controls as controls
import modbus_utils
from commands import ControlBlocked, execute_devices, transports_for
from models import CommandAudit, Inverter, MonitorConfig, User, db
from operation_locks import resource_lock, endpoint_key
from tests.helpers import post
from tests.fake_modbus import FakeModbusTcpClient, Response
from tests.test_telegram import asyncio_waker_only, tg


@pytest.fixture
def authorized(monkeypatch):
    path = controls.policy_path()
    path.write_text(json.dumps(controls.AUTHORIZATION))
    path.chmod(0o600)
    clients = []
    def factory(host, **kwargs):
        client = FakeModbusTcpClient(host, **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', factory)
    assert controls.enabled()
    return clients


def seed():
    inv = Inverter(name='Test 100TL', group='A', model='ingecon_sun_100tl', ip_address='192.0.2.1')
    db.session.add(inv)
    db.session.flush()
    db.session.add(MonitorConfig(inverter_id=inv.id, port=1502, device_id=7))
    db.session.commit()
    return inv


@pytest.mark.parametrize('action,percent,call', [
    ('on',100,('write',1000,6,7)), ('off',100,('write',1000,5,7)),
    ('limit',1,('write_multiple',1000,[3,327],7)),
    ('limit',20,('write_multiple',1000,[3,6553],7)),
    ('limit',60,('write_multiple',1000,[3,19660],7)),
    ('limit',100,('write_multiple',1000,[3,32767],7)),
    ('limit_max',25,('write_multiple',1000,[3,32767],7)),
])
def test_fixed_map_atomic_request_and_individual_transport(authorized, action, percent, call):
    inv = SimpleNamespace(model='ingecon_sun_100tl', ip_address='192.0.2.1')
    result = controls.execute(inv, dict(port=1502, device_id=7), action, percent, allow_write=True)
    assert result.confirmed and 'фізичний стан інвертора не підтверджено' in result.message
    client = authorized[0]
    assert client.calls == [('connect',),call]
    assert client.closed and client.retries == 0 and client.port == 1502
    assert 46 not in [item[1] for item in client.calls if item[0].startswith('write')]


@pytest.mark.parametrize('failure',['timeout','offline','exception','bad_address','bad_count','none'])
def test_ambiguous_reply_never_retried(authorized, monkeypatch, failure):
    fake = FakeModbusTcpClient('192.0.2.1')
    calls = []
    def write(*args, **kwargs):
        calls.append((args, kwargs))
        if failure in ('timeout','exception'): raise TimeoutError()
        if failure=='none': return None
        return Response(address=999 if failure=='bad_address' else 1000,
                        count=1 if failure=='bad_count' else 2)
    fake.online = failure != 'offline'
    monkeypatch.setattr(fake,'write_registers',write)
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',lambda **kwargs: fake)
    inv = SimpleNamespace(model='ingecon_sun_100tl', ip_address='192.0.2.1')
    assert not controls.execute(inv,dict(port=502,device_id=1),'limit',37,allow_write=True).confirmed
    assert len(calls) == (0 if failure=='offline' else 1) and fake.closed


def test_profile_cannot_unlock_legacy_or_raw_writes(authorized):
    assert not modbus_utils.modbus_turn_on('192.0.2.1',allow_write=True).confirmed
    assert not modbus_utils.modbus_limit_power('192.0.2.1',37,allow_write=True).confirmed
    assert not modbus_utils.modbus_control('192.0.2.1',address=46,value=37,device_id=1,allow_write=True).confirmed
    assert authorized == []


def test_endpoint_lock_blocks_controls(authorized):
    inv = SimpleNamespace(model='ingecon_sun_100tl', ip_address='192.0.2.1')
    with resource_lock(endpoint_key(inv.ip_address,502)):
        result = controls.execute(inv,dict(port=502,device_id=1),'off',allow_write=True)
    assert not result.confirmed and authorized == []


@pytest.mark.parametrize('value',[0,101,True,'37',1.5,None])
def test_invalid_percentage_never_creates_client(authorized,value):
    inv = SimpleNamespace(model='ingecon_sun_100tl', ip_address='192.0.2.1')
    with pytest.raises(ValueError): controls.execute(inv,dict(port=502,device_id=1),'limit',value,allow_write=True)
    assert authorized == []


def test_batch_validates_all_models_before_first_write(web, authorized):
    inv = seed()
    other = Inverter(name='Unknown', group='A', model='unknown', ip_address='192.0.2.2')
    db.session.add(other)
    db.session.commit()
    with pytest.raises(ValueError):
        execute_devices(db.session.get(User,1),'on',[inv,other],writes_enabled=True)
    assert authorized == []


@pytest.mark.parametrize('role',['Administrator','Operator'])
@pytest.mark.parametrize('action',['on','off','limit','limit_max'])
def test_web_confirmation_audit_and_replay(web,authorized,role,action):
    module, client = web
    seed()
    db.session.get(User,1).role = role
    db.session.commit()
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    data = dict(action=action,selected_ids='1',limit_value='37')
    headers = {'Accept':'application/json'}
    assert client.post('/control',data=data,headers=headers).status_code == 400
    preview = post(client,'/control',data,headers=headers)
    assert preview.status_code == 200 and preview.json['confirmation_required']
    assert authorized == []
    data['confirmation_token'] = preview.json['confirmation_token']
    result = post(client,'/control',data,headers=headers)
    assert result.status_code == 200, result.text
    assert result.json['results'][0]['outcome'] == 'register_confirmed'
    assert len(authorized) == 1 and authorized[0].port == 1502
    assert post(client,'/control',data,headers=headers).status_code == 400
    assert len(authorized) == 1
    audit = db.session.scalar(select(CommandAudit))
    assert (audit.source,audit.username,audit.result) == ('web','admin','finished')
    assert len(audit.request_id) <= 64
    assert audit.details[0]['command']['device_id'] == 7


@pytest.mark.parametrize('change',['model','port','unit','permission','map'])
def test_changed_target_or_authorization_invalidates_preview(web,authorized,change):
    module,client = web
    seed()
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    data = dict(action='on',selected_ids='1')
    headers = {'Accept':'application/json'}
    data['confirmation_token'] = post(client,'/control',data,headers=headers).json['confirmation_token']
    if change=='model': db.session.get(Inverter,1).model='unknown'
    if change=='port': db.session.get(MonitorConfig,1).port=502
    if change=='unit': db.session.get(MonitorConfig,1).device_id=2
    if change=='permission':
        controls.policy_path().write_text(json.dumps(dict(controls.AUTHORIZATION,enabled=False)))
    if change=='map':
        from modbus_config import load_configuration,save_configuration
        current=load_configuration()
        save_configuration(replace(current,start_value=5),current.fingerprint)
    db.session.commit()
    assert post(client,'/control',data,headers=headers).status_code in (400,403)
    assert authorized == []


def test_viewer_has_no_control_access_when_authorized(web,authorized):
    module,client=web
    seed()
    module.app.config['MODBUS_WRITES_ENABLED']=True
    db.session.get(User,1).role='Viewer'
    db.session.commit()
    assert post(client,'/control',dict(action='off',selected_ids='1')).status_code==403
    assert post(client,'/settings/modbus/control',dict(action='on',inverter_id='1',value='37')).status_code==403
    assert authorized==[]


def test_dedicated_control_executes_but_raw_panel_stays_locked(web,authorized):
    module,client=web
    seed()
    module.app.config['MODBUS_WRITES_ENABLED']=True
    for path,data in [('/settings/modbus/control',dict(inverter_id='1',action='limit',value='37')),
        ('/settings/modbus/registers',dict(inverter_id='1',ip='192.0.2.1',port='1502',device_id='7',function='16',address='1000',count='2',values='3,12123'))]:
        preview=post(client,path,data)
        assert preview.status_code==200, preview.text
        token=re.search(r'name="confirmation_token" value="([^"]+)"',preview.text).group(1)
        response=post(client,path,dict(data,confirmation_token=token))
        assert response.status_code==200
        assert post(client,path,dict(data,confirmation_token=token)).status_code==400
    assert len(authorized)==1
    assert ('write_multiple',1000,[3,12123],7) in authorized[0].calls


def test_telegram_global_authorization_limit_and_role_change(tg,authorized):
    with Session(tg.engine) as session, session.begin():
        for inv in session.scalars(select(Inverter)):
            inv.model='ingecon_sun_100tl'
        session.get(MonitorConfig,1).port=1502
        session.get(MonitorConfig,1).device_id=7
    tg.link()
    tg.feed('/limit 1 37')
    assert '37%' in tg.api.messages[-1].text and authorized==[]
    intent=tg.api.messages[-1].reply_markup.inline_keyboard[0][0].callback_data.split(':')[1]
    tg.feed(data='confirm:'+intent)
    assert len(authorized)==1 and authorized[0].port==1502
    assert ('write_multiple',1000,[3,12123],7) in authorized[0].calls
    with Session(tg.engine) as session:
        audit=session.scalar(select(CommandAudit))
        assert audit.telegram_id==1001 and audit.details[0]['command']['device_id']==7
    intent,_=tg.store.prepare(1001,'off','g',1)
    with Session(tg.engine) as session, session.begin(): session.get(User,1).role='Viewer'
    assert tg.store.confirm(1001,intent)[0]=='invalidated'
    assert len(authorized)==1


def test_default_closed_and_strict_private_authorization(monkeypatch, tmp_path):
    assert not controls.enabled()
    path=controls.policy_path()
    for data in [dict(controls.AUTHORIZATION,enabled='true'),dict(controls.AUTHORIZATION,scope='internet'),
                 dict(controls.AUTHORIZATION,acknowledge_unverified_firmware=1),{},None]:
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        with pytest.raises(ValueError): controls.enabled()
    path.write_text(json.dumps(controls.AUTHORIZATION))
    path.chmod(0o644)
    with pytest.raises(ValueError): controls.enabled()


def test_cli_requires_explicit_acknowledgment_and_disable_revokes(authorized):
    with pytest.raises(SystemExit): controls.main(['--enable-all-100tl'])
    controls.main(['--disable'])
    assert not controls.enabled() and controls.policy_path().stat().st_mode & 0o777 == 0o600
    inv=SimpleNamespace(model='ingecon_sun_100tl',ip_address='192.0.2.1')
    assert not controls.execute(inv,dict(port=502,device_id=1),'on',allow_write=True).confirmed
    assert authorized==[]
    controls.main(['--enable-all-100tl','--accept-unverified-firmware'])
    assert controls.enabled()


def test_old_release_check_cannot_report_all_writes_blocked_when_controls_enabled(authorized):
    from deploy.discovery_release import check
    with pytest.raises(ValueError,match='writes must remain blocked'):
        check()


@pytest.mark.parametrize('change',['unit','port','model','permission'])
def test_telegram_confirmation_binds_model_transport_and_authorization(tg,authorized,change):
    with Session(tg.engine) as session,session.begin(): session.get(Inverter,1).model='ingecon_sun_100tl'
    tg.link()
    intent,_=tg.store.prepare(1001,'on','i',1)
    with Session(tg.engine) as session,session.begin():
        if change=='unit': session.get(MonitorConfig,1).device_id=2
        if change=='port': session.get(MonitorConfig,1).port=1502
        if change=='model': session.get(Inverter,1).model='unknown'
        if change=='permission': controls.set_authorization(False)
    assert tg.store.confirm(1001,intent)[0]=='invalidated'
    assert authorized==[]
