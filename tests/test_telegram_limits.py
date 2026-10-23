"""Manual Telegram percentages: synthetic SQLite and mock Telegram/Modbus only."""
import pytest
from aiogram.types import ForceReply
from sqlalchemy import select
from sqlalchemy.orm import Session

import inverter_controls
import telegram_ui
from models import CommandAudit, Inverter, MonitorConfig, TelegramCommand, User
from telegram_store import TelegramAccess, TelegramStore
from tests.test_inverter_controls import authorized
from tests.test_telegram import asyncio_waker_only, tg


@pytest.fixture
def ready(tg, authorized, monkeypatch):
    monkeypatch.setattr(telegram_ui,'SEND_PART_DELAY',0)
    with Session(tg.engine) as session,session.begin():
        for inv in session.scalars(select(Inverter)):
            inv.model='ingecon_sun_100tl'
    tg.link()
    return tg,authorized


def pending(tg):
    with Session(tg.engine) as session:
        return session.scalar(select(TelegramCommand).where(TelegramCommand.state.in_(('input','awaiting'))))


def buttons(message):
    return [button for row in message.reply_markup.inline_keyboard for button in row]


def prompt(tg,kind='i',target=1):
    tg.feed(data=f'input:limit:{kind}:{target}')
    row=pending(tg)
    assert row.state=='input' and isinstance(tg.api.messages[-1].reply_markup,ForceReply)
    return row.id,row.payload['input_scope']['prompt_id']


@pytest.mark.parametrize('callback',['target:i:1','target:g:1','list:a:0'])
def test_manual_and_maximum_buttons_replace_presets(ready,callback):
    tg,clients=ready
    tg.feed(data=callback)
    keys=buttons(tg.api.messages[-1])
    assert {'Ввести ліміт','Максимум 100%'}<={key.text for key in keys}
    assert not {'25%','50%','75%','100%'}&{key.text for key in keys}
    assert not any(key.callback_data.startswith('act:limit:') for key in keys)
    assert clients==[]


@pytest.mark.parametrize('role',['Administrator','Operator'])
@pytest.mark.parametrize('kind,target,ids',[('i',1,[1]),('g',1,[1,2]),('a',0,[1,2,3])])
@pytest.mark.parametrize('text,value',[('1',1),('37%',37),(' 83 % ',83),('100',100)])
def test_manual_value_requires_confirmation_and_uses_shared_transport(ready,role,kind,target,ids,text,value):
    tg,clients=ready
    with Session(tg.engine) as session,session.begin():
        session.get(User,1).role=role
        session.get(MonitorConfig,1).port=1502
        session.get(MonitorConfig,1).device_id=7
    intent,prompt_id=prompt(tg,kind,target)
    assert clients==[]
    tg.feed(text,reply_to=prompt_id)
    preview=tg.api.messages[-1]
    assert f'ЛІМІТ {value}%' in preview.text and '120 секунд' in preview.text
    assert [int(line.split()[0][1:]) for line in preview.text.splitlines() if line.startswith('#')]==ids
    assert buttons(preview)[0].callback_data=='confirm:'+intent
    assert clients==[]
    tg.feed(data='confirm:'+intent)
    assert len(clients)==len(ids)
    for index,client in enumerate(clients):
        device_id=7 if index==0 else 1
        assert client.calls==[('connect',),('write_multiple',1000,[3,value*32767//100],device_id)]
        assert client.port==(1502 if index==0 else 502) and client.closed
    tg.feed(data='confirm:'+intent)
    assert len(clients)==len(ids)
    with Session(tg.engine) as session:
        audit=session.scalar(select(CommandAudit).where(CommandAudit.request_id=='tg:'+intent))
        assert audit.result=='confirmed' and audit.telegram_id==1001 and audit.user_id==1
        assert [row[0] for row in audit.targets]==ids


@pytest.mark.parametrize('kind,target,number',[('i',1,1),('g',1,2),('a',0,3)])
def test_maximum_uses_existing_confirmed_100_percent_action(ready,kind,target,number):
    tg,clients=ready
    prompt(tg,kind,target)
    tg.feed(data=f'act:limit_max:{kind}:{target}')
    preview=tg.api.messages[-1]
    assert 'МАКСИМУМ 100%' in preview.text and clients==[]
    tg.feed(data=buttons(preview)[0].callback_data)
    assert len(clients)==number
    assert all(client.calls==[('connect',),('write_multiple',1000,[3,32767],1)] for client in clients)


@pytest.mark.parametrize('text',['0','101','-1','1.5','NaN','37abc','37%%','1 2','01','+1','١٢','０５'])
def test_invalid_input_is_not_consumed_and_does_not_extend_expiry(ready,text):
    tg,clients=ready
    intent,_=prompt(tg)
    tg.clock[0]+=10
    tg.feed(text)
    assert 'ціле число від 1 до 100' in tg.api.messages[-1].text
    row=pending(tg)
    assert row.id==intent and row.state=='input' and row.expires_at==100300
    assert clients==[]
    tg.feed('37')
    assert pending(tg).payload['value']==37 and clients==[]


@pytest.mark.parametrize('cancel',['/cancel','/inverters','menu:start','target:i:2','list:g:0'])
def test_cancel_and_navigation_discard_input(ready,cancel):
    tg,clients=ready
    intent,_=prompt(tg)
    tg.feed(cancel) if cancel.startswith('/') else tg.feed(data=cancel)
    tg.feed('37')
    assert 'Спочатку виберіть' in tg.api.messages[-1].text and clients==[]
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand,intent).state=='cancelled'


def test_input_is_one_time_bound_to_user_and_latest_prompt(ready):
    tg,clients=ready
    old,old_prompt=prompt(tg)
    intent,prompt_id=prompt(tg,'i',2)
    tg.feed('37',reply_to=old_prompt)
    assert 'інший запит' in tg.api.messages[-1].text
    assert pending(tg).id==intent
    tg.feed('37',uid=2002,reply_to=prompt_id)
    assert 'Спочатку отримайте код' in tg.api.messages[-1].text
    tg.feed('37',reply_to=prompt_id)
    assert '#2 Mock 2' in tg.api.messages[-1].text
    tg.feed('40',reply_to=prompt_id)
    assert 'Спочатку виберіть' in tg.api.messages[-1].text and clients==[]
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand,old).state=='cancelled'


def test_input_survives_restart_without_auto_execution(ready):
    tg,clients=ready
    intent,prompt_id=prompt(tg,'g',1)
    restarted=TelegramStore(tg.engine,clock=lambda:tg.clock[0])
    restarted.recover()
    actual,payload=restarted.submit_limit_input(1001,'42%',prompt_id)
    assert actual==intent and payload['value']==42 and [row[0] for row in payload['targets']]==[1,2]
    assert clients==[]
    with pytest.raises(TelegramAccess):restarted.submit_limit_input(1001,'42%',prompt_id)


@pytest.mark.parametrize('change',['expired','model','transport','group','all','policy'])
def test_changed_context_and_expired_input_cannot_be_confirmed(ready,change):
    tg,clients=ready
    intent,_=prompt(tg,'a' if change=='all' else 'g',0 if change=='all' else 1)
    if change=='expired':tg.clock[0]+=300
    elif change=='policy':inverter_controls.policy_path().write_text('{}')
    else:
        with Session(tg.engine) as session,session.begin():
            if change=='model':session.get(Inverter,1).model='unknown'
            elif change=='transport':session.get(MonitorConfig,1).port=1502
            elif change=='group':session.get(Inverter,3).group='A'
            elif change=='all':session.add(Inverter(id=4,name='New',model='ingecon_sun_100tl',ip_address='192.0.2.4',group='B'))
    tg.feed('37')
    assert not tg.api.messages[-1].reply_markup and clients==[]
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand,intent).state in ('expired','invalidated')


@pytest.mark.parametrize('change',['Viewer','inactive','unlink'])
def test_live_authorization_required_at_input_and_confirmation(ready,change):
    tg,clients=ready
    intent,_=prompt(tg)
    if change=='unlink':
        with tg.store.sessions.begin() as session:tg.store.revoke_user(session,1)
    else:
        with Session(tg.engine) as session,session.begin():
            user=session.get(User,1)
            if change=='Viewer':user.role='Viewer'
            else:user.active=False
    tg.feed('37')
    tg.feed(data='confirm:'+intent)
    assert clients==[]


def test_viewer_and_disabled_configuration_cannot_request_input(tg):
    tg.link()
    tg.feed(data='input:limit:i:1')
    assert 'заблоковане' in tg.api.messages[-1].text
    with Session(tg.engine) as session,session.begin():session.get(User,1).role='Viewer'
    tg.feed(data='target:i:1')
    assert not any('ліміт' in key.text.casefold() for key in buttons(tg.api.messages[-1]))
    tg.feed(data='input:limit:i:1')
    assert 'Viewer' in tg.api.messages[-1].text and pending(tg) is None


def test_cleanup_expires_input_and_audit(ready):
    tg,clients=ready
    intent,_=prompt(tg)
    tg.clock[0]+=300
    tg.store.cleanup()
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand,intent).state=='expired'
        assert session.scalar(select(CommandAudit.result).where(CommandAudit.request_id=='tg:'+intent))=='expired'
    assert clients==[]


def test_manual_all_includes_every_page_for_more_than_twenty_devices(ready):
    tg,clients=ready
    with Session(tg.engine) as session,session.begin():
        for number in range(4,26):
            session.add(Inverter(id=number,name=f'Mock {number}',model='ingecon_sun_100tl',ip_address=f'192.0.2.{number}',group='B'))
    intent,_=prompt(tg,'a',0)
    tg.feed('37')
    assert '(25 пристроїв)' in tg.api.messages[-1].text and '#25 Mock 25' in tg.api.messages[-1].text
    assert clients==[]
    tg.feed(data='confirm:'+intent)
    assert len(clients)==25


def test_cancel_preview_never_executes_limit(ready):
    tg,clients=ready
    intent,_=prompt(tg)
    tg.feed('37')
    tg.feed(data='cancel:'+intent)
    assert 'Скасовано' in tg.api.messages[-1].text and clients==[]
