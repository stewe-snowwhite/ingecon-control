"""Telegram generation and virtual all-equipment group; network is mocked."""
import math
import re

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import CommandAudit, Inverter, InverterStatus, MonitorConfig, TelegramCommand, User
from telegram_store import TelegramAccess
from telegram_ui import command_result_message, generation_text, power_text
from tests.test_telegram import asyncio_waker_only, tg
from tests.test_telegram_controls import enable


@pytest.mark.parametrize('watts,expected', [(62980,'62,98 кВт'),(0,'0,00 кВт'),(-20,'-0,02 кВт'),(None,'—')])
def test_power_uses_kw_including_zero_and_signed_values(watts,expected):
    assert power_text(watts)==expected


def test_generation_excludes_stale_missing_and_non_numeric_values_without_faking_zero():
    rows=[dict(active_power_w=62980,stale=False,checked_at=1000),
          dict(active_power_w=1000,stale=False,checked_at=995)]
    text=generation_text(dict(inverters=rows))
    assert '63,98 кВт' in text and 'часткові' not in text and 'Виміри:' in text
    assert '0,00 кВт' in generation_text(dict(inverters=[dict(active_power_w=0,stale=False,checked_at=1000)]))
    rows += [dict(active_power_w=90000,stale=True),dict(active_power_w=None,stale=False),
             dict(active_power_w=math.nan,stale=False),dict(active_power_w=True,stale=False)]
    assert '63,98 кВт (часткові дані 2/6)' in generation_text(dict(inverters=rows))
    assert 'немає актуальних даних' in generation_text(dict(inverters=rows[2:]))
    assert '0,00 кВт' not in generation_text(dict(inverters=rows[2:]))
    assert 'немає обладнання' in generation_text(dict(inverters=[]))


def set_power(tg,values):
    with Session(tg.engine) as session,session.begin():
        for inverter_id,watts in values.items():
            row=session.get(InverterStatus,inverter_id)
            row.active_power_w=watts


@pytest.mark.parametrize('entry', ['/status','menu:status'])
def test_status_command_and_button_show_current_generation_time_and_no_probes(tg,modbus,entry):
    tg.link();set_power(tg,{1:62980,2:1000,3:0})
    tg.feed(text=entry) if entry.startswith('/') else tg.feed(data=entry)
    text=tg.api.messages[-1].text
    assert 'Поточна генерація: 63,98 кВт' in text
    assert 'Станом на' in text and 'Виміри:' in text and 'UTC' in text
    assert modbus[1]==[]


def test_partial_power_and_stopped_monitor_are_reported_honestly(tg,modbus):
    tg.link();set_power(tg,{1:62980,2:None,3:None})
    tg.feed('/status')
    assert '62,98 кВт (часткові дані 1/3)' in tg.api.messages[-1].text
    tg.clock[0]+=1000;tg.feed('/status')
    text=tg.api.messages[-1].text
    assert 'немає актуальних даних' in text and '62,98' not in text and '0,00 кВт' not in text
    assert modbus[1]==[]


def test_success_is_short_and_failures_keep_the_actual_error():
    assert command_result_message(dict(outcome='register_confirmed',message='фізичний стан інвертора не підтверджено'))=='Запис команди підтверджено.'
    for outcome in ('unconfirmed','unknown','blocked'):
        assert command_result_message(dict(outcome=outcome,message='Результат невідомий.'))=='Результат невідомий.'


def test_all_is_virtual_covers_both_groups_and_shows_per_device_power(tg,modbus):
    tg.link();set_power(tg,{1:62980,2:1000,3:0})
    before=tg.store.inventory(1001,'g')
    tg.feed('/groups')
    assert tg.api.messages[-1].reply_markup.inline_keyboard[0][0].text=='Усі'
    assert tg.api.messages[-1].reply_markup.inline_keyboard[0][0].callback_data=='list:a:0'
    tg.feed(data='list:a:0')
    text=tg.api.messages[-1].text
    assert 'Група «Усі»' in text and 'Обладнання: 3' in text
    assert all(f'#{number} Mock {number}' in text for number in (1,2,3))
    assert '62,98 кВт' in text and '0,00 кВт' in text
    assert tg.store.inventory(1001,'g')==before and modbus[1]==[]


def test_all_pages_include_every_device_without_expanding_command_limit(tg,modbus):
    tg.link()
    with Session(tg.engine) as session,session.begin():
        session.add_all(Inverter(id=n,name=f'Mock {n}',group='C',ip_address=f'192.0.2.{n}') for n in range(4,26))
    found=[]
    for page in range(4):
        tg.feed(data=f'list:a:{page}')
        message=tg.api.messages[-1]
        found += [int(value) for value in re.findall(r'^#(\d+) ',message.text,re.M)]
        assert 'Обладнання: 25' in message.text
        assert 'максимум 10 цілей' in message.text
        assert 'act:on:a:0' not in message.reply_markup.model_dump_json()
    assert found==list(range(1,26))
    with pytest.raises(TelegramAccess,match='Максимум 10'):tg.store.prepare(1001,'on','a',0)
    assert modbus[1]==[]


def test_real_group_named_all_does_not_shadow_the_virtual_group(tg):
    tg.link()
    with Session(tg.engine) as session,session.begin():session.get(Inverter,1).group='Усі'
    tg.feed('/groups')
    markup=tg.api.messages[-1].reply_markup
    labels={button.callback_data:button.text for row in markup.inline_keyboard for button in row}
    assert labels['list:a:0']=='Усі'
    assert labels['target:g:1']=='Усі (збережена група)'
    tg.feed(data='pref:group:1')
    assert 'Група: Усі (збережена група)' in tg.api.messages[-1].text
    tg.feed(data='pref:all')
    assert 'Група: Усі' in tg.api.messages[-1].text and 'збережена група' not in tg.api.messages[-1].text


def test_empty_all_group_does_not_offer_or_prepare_a_noop_command(tg,modbus):
    tg.link()
    with Session(tg.engine) as session,session.begin():
        for model in (InverterStatus,MonitorConfig,Inverter):session.execute(delete(model))
    tg.feed('/groups');assert 'list:a:0' in tg.api.messages[-1].reply_markup.model_dump_json()
    tg.feed(data='list:a:0')
    assert 'немає обладнання' in tg.api.messages[-1].text
    assert 'act:on:a:0' not in tg.api.messages[-1].reply_markup.model_dump_json()
    with pytest.raises(TelegramAccess,match='порожня'):tg.store.prepare(1001,'on','a',0)
    assert modbus[1]==[]


def test_viewer_and_unlinked_user_cannot_control_all(tg,modbus):
    tg.feed(data='list:a:0');assert 'Спочатку' in tg.api.messages[-1].text
    with Session(tg.engine) as session,session.begin():
        session.get(User,1).role='Viewer'
        session.add_all(Inverter(id=n,name=f'Mock {n}',group='C',ip_address=f'192.0.2.{n}') for n in range(4,12))
    tg.link();tg.feed(data='list:a:0')
    assert 'act:on:a:0' not in tg.api.messages[-1].reply_markup.model_dump_json()
    assert 'максимум 10 цілей' not in tg.api.messages[-1].text
    tg.feed(data='act:on:a:0');assert 'Viewer' in tg.api.messages[-1].text
    assert modbus[1]==[]


@pytest.mark.parametrize('change', ['add','remove','edit'])
def test_all_confirmation_rechecks_exact_membership_and_target_details(tg,modbus,change):
    tg.link();intent,payload=tg.store.prepare(1001,'on','a',0)
    assert payload['target_scope']=='all' and [row[0] for row in payload['targets']]==[1,2,3]
    with Session(tg.engine) as session,session.begin():
        if change=='add':session.add(Inverter(id=4,name='New',group='Other',ip_address='192.0.2.4'))
        elif change=='remove':session.delete(session.get(Inverter,3))
        else:session.get(Inverter,3).group='Changed'
    assert tg.store.confirm(1001,intent)[0]=='invalidated'
    assert modbus[1]==[]


def test_all_cannot_bypass_per_device_authorization(tg,modbus):
    enable(tg,ids=(1,2))
    intent,_=tg.store.prepare(1001,'on','a',0)
    assert tg.store.confirm(1001,intent)[0]=='blocked' and modbus[1]==[]


def test_all_success_uses_shared_mock_commands_once_and_keeps_full_audit(tg,modbus):
    enable(tg,ids=(1,2,3))
    tg.feed(data='list:a:0');assert 'act:on:a:0' in tg.api.messages[-1].reply_markup.model_dump_json()
    tg.feed(data='act:on:a:0')
    assert all(f'#{number} Mock {number}' in tg.api.messages[-1].text for number in (1,2,3))
    assert modbus[1]==[]
    with Session(tg.engine) as session:intent=session.scalar(select(TelegramCommand.id))
    tg.feed(data='confirm:'+intent)
    text=tg.api.messages[-1].text
    assert 'Записи команд підтверджено.' in text and 'фізичний стан' not in text
    assert len(modbus[1])==3
    with Session(tg.engine) as session:
        audit=session.scalar(select(CommandAudit))
        assert audit.result=='confirmed' and len(audit.details)==3
    with pytest.raises(TelegramAccess):tg.store.confirm(1001,intent)
    assert len(modbus[1])==3


@pytest.mark.parametrize('target', [1,-1,False,'0'])
def test_invalid_all_scope_identifiers_are_rejected(tg,modbus,target):
    tg.link()
    with pytest.raises(TelegramAccess):tg.store.prepare(1001,'on','a',target)
    assert modbus[1]==[]
