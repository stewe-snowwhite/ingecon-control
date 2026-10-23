"""Fleet budget Telegram handlers. Fake API and no physical operations."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CommandAudit, Inverter, TelegramCommand, User
import power_budget as budget
from tests.test_inverter_controls import authorized
from tests.test_telegram import asyncio_waker_only, tg


@pytest.fixture
def ready(tg,authorized):
    with Session(tg.engine) as session,session.begin():
        for inv in session.scalars(select(Inverter)):inv.model='ingecon_sun_100tl'
    tg.link()
    return tg,authorized


def buttons(message):return [button for row in message.reply_markup.inline_keyboard for button in row]


def test_global_kw_button_is_inside_control_not_the_main_menu(ready):
    tg,clients=ready
    tg.feed('/start')
    assert [key.text for key in buttons(tg.api.messages[-1])]==['Статус','Сповіщення','Інвертори','Групи','Керування']
    tg.feed('/control')
    assert any(key.callback_data=='menu:power' for key in buttons(tg.api.messages[-1]))
    tg.feed(data='menu:power')
    assert any(key.callback_data=='budget:input' for key in buttons(tg.api.messages[-1]))
    assert clients==[]


def test_numeric_input_confirmation_persistence_and_disable(ready):
    tg,clients=ready
    tg.feed(data='budget:input')
    with Session(tg.engine) as session:
        row=session.scalar(select(TelegramCommand).where(TelegramCommand.state=='budget_input'))
        prompt=row.payload['prompt_id']
    tg.feed('150,5',reply_to=prompt)
    preview=tg.api.messages[-1]
    assert '150,5 кВт' in preview.text and '#3 Mock 3' in preview.text
    assert budget.load() is None and clients==[]
    tg.feed(data=buttons(preview)[0].callback_data)
    assert budget.load()['enabled'] and budget.load()['target_w']==150500 and clients==[]
    tg.feed(data=buttons(preview)[0].callback_data)
    assert 'вже використано' in tg.api.messages[-1].text
    tg.feed('/poweroff')
    preview=tg.api.messages[-1]
    assert 'Останні ліміти залишаться' in preview.text
    tg.feed(data=buttons(preview)[0].callback_data)
    assert budget.load()['enabled'] is False and clients==[]


def test_custom_nominal_and_cancel_preview(ready):
    tg,clients=ready
    tg.feed('/power 150 110')
    preview=tg.api.messages[-1]
    assert '110 кВт' in preview.text
    tg.feed(data=buttons(preview)[1].callback_data)
    assert 'Скасовано' in tg.api.messages[-1].text and not budget.load() and clients==[]


@pytest.mark.parametrize('text',['0','-1','NaN','1e3','400'])
def test_invalid_value_never_configures_or_writes(ready,text):
    tg,clients=ready
    tg.feed(data='budget:input')
    tg.feed(text)
    assert not budget.load() and clients==[]


@pytest.mark.parametrize('role',['Viewer','Operator'])
def test_only_admin_can_enable_or_disable(ready,role):
    tg,clients=ready
    with Session(tg.engine) as session,session.begin():session.get(User,1).role=role
    tg.feed('/power 150')
    assert 'Administrator' in tg.api.messages[-1].text
    tg.feed(data='budget:input')
    assert 'Administrator' in tg.api.messages[-1].text
    assert not budget.load() and clients==[]


def test_input_context_cancellation_and_no_confusion_with_percent(ready):
    tg,clients=ready
    tg.feed(data='budget:input')
    tg.feed('/cancel')
    tg.feed('150')
    assert 'Спочатку виберіть' in tg.api.messages[-1].text
    tg.feed(data='budget:input')
    tg.feed(data='input:limit:i:1')
    tg.feed('37')
    assert 'ЛІМІТ 37%' in tg.api.messages[-1].text and not budget.load() and clients==[]


def test_other_user_and_wrong_reply_cannot_submit(ready):
    tg,clients=ready
    tg.feed(data='budget:input')
    tg.feed('150',uid=2002)
    assert 'Спочатку' in tg.api.messages[-1].text
    tg.feed('150',reply_to=99999)
    assert 'інший запит' in tg.api.messages[-1].text
    assert not budget.load() and clients==[]


@pytest.mark.parametrize('change,expected',[('supersede','superseded'),('unlink','cancelled'),
    ('expire','expired'),('recover','unknown')])
def test_budget_audit_follows_shared_intent_lifecycle(ready,change,expected):
    tg,clients=ready
    tg.feed('/power 150')
    intent=buttons(tg.api.messages[-1])[0].callback_data.split(':')[-1]
    if change=='supersede':tg.feed(data='act:limit:i:1:37')
    elif change=='unlink':tg.store.unlink(1)
    elif change=='expire':tg.clock[0]+=120;tg.store.cleanup()
    else:
        with Session(tg.engine) as session,session.begin():session.get(TelegramCommand,intent).state='processing'
        tg.store.recover()
    with Session(tg.engine) as session:
        assert session.scalar(select(CommandAudit.result).where(CommandAudit.request_id=='budget:'+intent))==expected
    assert not budget.load() and clients==[]
