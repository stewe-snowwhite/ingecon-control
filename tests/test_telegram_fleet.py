"""Large Telegram batches: mock API/Modbus, exact previews and one execution."""
import asyncio
import re
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

import telegram_ui
import modbus_utils
from commands import MAX_CONTROL_TARGETS
from models import CommandAudit, Inverter, TelegramCommand, User
from telegram_store import TelegramAccess
from telegram_ui import keyboard, message_parts, send
from tests.test_telegram import asyncio_waker_only, tg
from tests.test_telegram_controls import enable


@pytest.fixture(autouse=True)
def no_real_pacing_delay(monkeypatch):
    monkeypatch.setattr(telegram_ui,'SEND_PART_DELAY',0)


def fleet(tg,count,long_names=False):
    with Session(tg.engine) as session,session.begin():
        for number in range(1,count+1):
            row=session.get(Inverter,number)
            if row is None:
                row=Inverter(id=number,ip_address=f'192.0.2.{number}')
                session.add(row)
            row.name=f'Mock {number}'+(' 🔆'*40 if long_names else '')
            row.group='A'


@pytest.mark.parametrize('text', [
    'Коротке повідомлення',
    ''.join(f'#{n} '+('🔆'*80)+'\n' for n in range(100)),
    '🔆'*10000,
    'Назва <b>без HTML</b>\n'*1000,
])
def test_message_parts_preserve_all_text_and_unicode_within_telegram_budget(text):
    parts=message_parts(text)
    assert ''.join(parts)==text
    assert all(part and len(part.encode('utf-16-le'))//2<=3500 for part in parts)


@pytest.mark.parametrize('budget', [0,1,True,3501])
def test_invalid_message_budget_is_rejected(budget):
    with pytest.raises(ValueError):message_parts('text',budget)


def test_multipart_send_paces_parts_and_attaches_buttons_only_to_last(tg,monkeypatch):
    sleeps=[]
    async def sleep(delay):sleeps.append(delay)
    monkeypatch.setattr(telegram_ui,'asyncio',SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(telegram_ui,'SEND_PART_DELAY',1)
    text='🔆'*6000
    markup=keyboard([[('Підтвердити','confirm:mock')]])
    reply=asyncio.run(send(tg.bot,1001,text,markup))
    messages=tg.api.messages
    assert len(messages)>1 and ''.join(message.text for message in messages)==text
    assert all(message.reply_markup is None for message in messages[:-1])
    assert messages[-1].reply_markup==markup and sleeps==[1]*(len(messages)-1)
    assert all(message.parse_mode is None and message.protect_content for message in messages)
    assert reply.message_id is not None


@pytest.mark.parametrize('kind,target', [('a',0),('g',1)])
def test_25_device_group_executes_serially_once_with_full_audit(tg,modbus,monkeypatch,kind,target):
    fleet(tg,25);enable(tg,ids=tuple(range(1,26)))
    original=modbus_utils.ModbusTcpClient
    active=[0];peak=[0]
    def factory(*args,**kwargs):
        client=original(*args,**kwargs)
        connect,close=client.connect,client.close
        def connected():
            active[0]+=1;peak[0]=max(peak[0],active[0]);return connect()
        def closed():
            close();active[0]-=1
        client.connect,client.close=connected,closed
        return client
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',factory)
    tg.feed(data=f'act:on:{kind}:{target}')
    preview=''.join(message.text for message in tg.api.messages)
    assert [int(value) for value in re.findall(r'^#(\d+) ',preview,re.M)]==list(range(1,26))
    assert modbus[1]==[]
    with Session(tg.engine) as session:intent=session.scalar(select(TelegramCommand.id))
    tg.feed(data='confirm:'+intent)
    assert len(modbus[1])==25 and peak[0]==1 and active[0]==0
    assert all(len([call for call in client.calls if call[0]=='write'])==1 for client in modbus[1])
    with Session(tg.engine) as session:
        audit=session.scalar(select(CommandAudit).where(CommandAudit.request_id=='tg:'+intent))
        assert audit.result=='confirmed' and len(audit.targets)==len(audit.details)==25
    tg.feed(data='confirm:'+intent)
    assert len(modbus[1])==25


def test_100_long_names_are_fully_reviewable_before_confirmation(tg,modbus):
    fleet(tg,MAX_CONTROL_TARGETS,long_names=True);tg.link()
    tg.feed(data='act:on:a:0')
    messages=tg.api.messages
    assert len(messages)>1
    assert all(len(message.text.encode('utf-16-le'))//2<=3500 for message in messages)
    assert [int(value) for value in re.findall(r'^#(\d+) ',''.join(message.text for message in messages),re.M)]==list(range(1,101))
    assert all(message.reply_markup is None for message in messages[:-1])
    assert 'confirm:' in messages[-1].reply_markup.model_dump_json()
    assert modbus[1]==[]
    with Session(tg.engine) as session:intent=session.scalar(select(TelegramCommand.id))
    assert tg.store.confirm(1001,intent,cancel=True)[0]=='cancelled'


def test_incomplete_preview_never_sends_confirmation_button_or_control_command(tg,modbus,monkeypatch):
    fleet(tg,100,long_names=True);tg.link()
    original=tg.api.make_request
    sent=[0]
    async def fail_second(bot,method,timeout=None):
        if isinstance(method,SendMessage):
            sent[0]+=1
            if sent[0]==2:raise TelegramNetworkError(method=method,message='mock connection loss')
        return await original(bot,method,timeout)
    monkeypatch.setattr(tg.api,'make_request',fail_second)
    tg.feed(data='act:on:a:0')
    assert sent[0]==2 and tg.api.messages and all(message.reply_markup is None for message in tg.api.messages)
    assert modbus[1]==[]


def test_25_device_batch_preflights_entire_fleet_before_first_write(tg,modbus):
    fleet(tg,25);enable(tg,ids=tuple(range(1,25)))
    intent,_=tg.store.prepare(1001,'on','a',0)
    assert tg.store.confirm(1001,intent)[0]=='blocked' and modbus[1]==[]


def test_partial_batch_keeps_each_device_result_and_does_not_retry_failed_write(tg,modbus,monkeypatch):
    fleet(tg,25);enable(tg,ids=tuple(range(1,26)))
    original=modbus_utils.ModbusTcpClient
    def factory(*args,**kwargs):
        client=original(*args,**kwargs)
        if client.host=='192.0.2.13':client.write_error=True
        return client
    monkeypatch.setattr(modbus_utils,'ModbusTcpClient',factory)
    intent,_=tg.store.prepare(1001,'off','a',0)
    result,details=tg.store.confirm(1001,intent)
    assert result=='partial' and len(details)==25
    assert details[12]['id']==13 and details[12]['outcome']=='unconfirmed'
    assert len(modbus[1])==25 and all(len([call for call in client.calls if call[0]=='write'])==1 for client in modbus[1])


@pytest.mark.parametrize('kind,target', [('a',0),('g',1)])
def test_shared_100_device_boundary_still_rejects_oversized_batches_before_io(tg,modbus,kind,target):
    fleet(tg,MAX_CONTROL_TARGETS+1);tg.link()
    with pytest.raises(TelegramAccess,match='100 пристроїв'):tg.store.prepare(1001,'on',kind,target)
    assert modbus[1]==[]


@pytest.mark.parametrize('change', ['membership','role','policy'])
def test_large_fleet_confirmation_rechecks_approval(tg,modbus,change):
    fleet(tg,25);enable(tg,ids=tuple(range(1,26)))
    intent,_=tg.store.prepare(1001,'on','a',0)
    if change=='policy':
        from telegram_control import TelegramControlPolicy
        tg.store.control_policy=TelegramControlPolicy()
    else:
        with Session(tg.engine) as session,session.begin():
            if change=='role':session.get(User,1).role='Viewer'
            else:session.add(Inverter(id=26,name='New member',group='Other',ip_address='192.0.2.26'))
    assert tg.store.confirm(1001,intent)[0]=='invalidated' and modbus[1]==[]
