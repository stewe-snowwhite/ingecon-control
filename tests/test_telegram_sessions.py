"""Web logout revokes cookies while keeping valid persistent Telegram links."""
import time

import pytest
from flask import request
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from models import User, Inverter, StateEvent, TelegramLinkCode, TELEGRAM_TABLES
from telegram_store import TelegramAccess, TelegramStore
from tests.helpers import post


@pytest.mark.parametrize('role', ['Administrator','Operator','Viewer'])
def test_logout_preserves_link_and_preferences_but_revokes_copied_web_cookie(web,modbus,role):
    module,client=web
    user=module.db.session.get(User,1);user.role=role;module.db.session.commit()
    store=TelegramStore(module.db.engine)
    store.redeem(1001,store.issue_code(1))
    store.preferences(1001,'notifications');store.preferences(1001,'online')
    original=store.web_link(1)
    fields=('telegram_id','user_id','linked_at','notifications','online','offline','degraded','group_filter','last_event_id')
    before={field:getattr(original,field) for field in fields}
    copied=module.app.test_client();copied.set_cookie('session',client.get_cookie('session').value)
    previous=user.session_version
    assert post(client,'/logout').status_code==302
    assert client.get('/').status_code==401 and copied.get('/').status_code==401
    restarted=TelegramStore(module.db.engine)
    assert restarted.identity(1001)==('admin',role)
    link=restarted.web_link(1)
    assert {field:getattr(link,field) for field in fields}==before
    assert link.user_version==module.db.session.get(User,1).session_version==previous+1
    assert modbus[1]==[]


def test_switching_web_accounts_with_or_without_logout_preserves_both_telegram_users(web):
    module,client=web
    other=User(username='other',role='Viewer');other.set_password('other-test-password')
    module.db.session.add(other);module.db.session.commit()
    store=TelegramStore(module.db.engine)
    store.redeem(1001,store.issue_code(1));store.redeem(1002,store.issue_code(other.id))
    assert post(client,'/login',dict(username='other',password='other-test-password')).status_code==302
    assert store.identity(1001)==('admin','Administrator') and store.identity(1002)==('other','Viewer')
    assert post(client,'/logout').status_code==302
    assert post(client,'/login',dict(username='admin',password='test-password-long')).status_code==302
    assert post(client,'/logout').status_code==302
    restarted=TelegramStore(module.db.engine)
    assert restarted.identity(1001)==('admin','Administrator') and restarted.identity(1002)==('other','Viewer')


def test_pairing_code_keeps_original_expiry_across_logout_and_store_restart(web):
    module,client=web
    clock=[1000]
    store=TelegramStore(module.db.engine,clock=lambda:clock[0])
    code=store.issue_code(1)
    row=module.db.session.get(TelegramLinkCode,1)
    digest,expires=row.digest,row.expires_at
    assert post(client,'/logout').status_code==302
    module.db.session.expire_all();row=module.db.session.get(TelegramLinkCode,1)
    assert row.digest==digest and row.expires_at==expires==1300
    clock[0]=1299
    assert TelegramStore(module.db.engine,clock=lambda:clock[0]).redeem(1001,code)==('admin','Administrator')


def test_expired_code_is_not_extended_by_logout(web):
    module,client=web
    store=TelegramStore(module.db.engine,clock=lambda:1000)
    code=store.issue_code(1)
    assert post(client,'/logout').status_code==302
    with pytest.raises(TelegramAccess):TelegramStore(module.db.engine,clock=lambda:1300).redeem(1001,code)


def test_password_revoked_pairing_is_not_revived_by_login_or_logout(web):
    module,client=web
    store=TelegramStore(module.db.engine)
    store.redeem(1001,store.issue_code(1))
    old=store.web_link(1).user_version
    user=module.db.session.get(User,1);user.set_password('replacement-test-password');module.db.session.commit()
    assert post(client,'/login',dict(username='admin',password='replacement-test-password')).status_code==302
    page=client.get('/settings/telegram').text
    assert 'доступ Telegram уже відкликано' in page
    assert 'Оновлення застосунку' in page
    assert post(client,'/logout').status_code==302
    assert store.web_link(1).user_version==old
    with pytest.raises(TelegramAccess):store.identity(1001)


def test_password_revoked_code_is_not_revived_by_logout(web):
    module,client=web
    store=TelegramStore(module.db.engine)
    code=store.issue_code(1)
    user=module.db.session.get(User,1);user.set_password('replacement-test-password');module.db.session.commit()
    assert post(client,'/login',dict(username='admin',password='replacement-test-password')).status_code==302
    assert post(client,'/logout').status_code==302
    with pytest.raises(TelegramAccess):store.redeem(1001,code)


def test_password_change_between_authentication_and_logout_cannot_reauthorize_telegram(web):
    module,client=web
    store=TelegramStore(module.db.engine)
    store.redeem(1001,store.issue_code(1))
    original=store.web_link(1).user_version
    def change_credentials():
        if request.endpoint=='logout':
            user=module.db.session.get(User,1)
            user.set_password('concurrent-test-password');module.db.session.commit()
    module.app.before_request_funcs[None].append(change_credentials)
    assert post(client,'/logout').status_code==401
    assert store.web_link(1).user_version==original
    with pytest.raises(TelegramAccess):store.identity(1001)


def test_logout_rolls_back_cookie_rotation_and_pairing_update_together(web,monkeypatch):
    module,client=web
    store=TelegramStore(module.db.engine);store.redeem(1001,store.issue_code(1))
    original=module.db.session.get(User,1).session_version
    preserve=TelegramStore.preserve_on_web_logout
    def fail(*args):
        preserve(*args)
        raise OperationalError('synthetic failure',{},Exception('offline mock'))
    monkeypatch.setattr(TelegramStore,'preserve_on_web_logout',staticmethod(fail))
    assert post(client,'/logout').status_code==503
    assert module.db.session.get(User,1).session_version==original
    assert store.identity(1001)==('admin','Administrator')
    assert store.web_link(1).user_version==original and client.get('/').status_code==200


def test_logout_works_without_prepared_telegram_tables_and_never_creates_them(web):
    module,client=web
    module.db.session.remove()
    module.db.metadata.drop_all(module.db.engine,tables=TELEGRAM_TABLES)
    assert post(client,'/logout').status_code==302
    tables=set(inspect(module.db.engine).get_table_names())
    assert not {table.name for table in TELEGRAM_TABLES} & tables


def test_long_web_inactivity_and_recovery_leave_valid_pairings_persistent(web):
    module,_=web
    store=TelegramStore(module.db.engine)
    store.redeem(1001,store.issue_code(1));store.preferences(1001,'notifications')
    restarted=TelegramStore(module.db.engine,clock=lambda:time.time()+40*86400)
    restarted.recover();restarted.cleanup()
    assert restarted.identity(1001)==('admin','Administrator')
    assert restarted.web_link(1).notifications


def test_web_logout_keeps_bot_identity_but_invalidates_pending_control_confirmation(web,modbus):
    module,client=web
    module.db.session.add(Inverter(name='Mock only',group='A',ip_address='192.0.2.1'))
    module.db.session.commit()
    store=TelegramStore(module.db.engine);store.redeem(1001,store.issue_code(1))
    intent,_=store.prepare(1001,'on','i',1)
    assert post(client,'/logout').status_code==302
    assert store.identity(1001)==('admin','Administrator')
    assert store.confirm(1001,intent)[0]=='invalidated'
    assert modbus[1]==[]


def test_pending_notifications_are_deliverable_after_web_logout(web):
    module,client=web
    store=TelegramStore(module.db.engine,clock=lambda:1000)
    store.redeem(1001,store.issue_code(1));store.preferences(1001,'notifications')
    module.db.session.add(StateEvent(inverter_id=1,inverter_name='Mock only',target_key='synthetic',
        previous_state='Online',state='Offline',occurred_at=1000))
    module.db.session.commit();store.fanout()
    assert post(client,'/logout').status_code==302
    delivery=TelegramStore(module.db.engine,clock=lambda:1000).claim_delivery()
    assert delivery and delivery[1]==1001 and delivery[2][3]=='Offline'


def test_identity_refreshes_cached_pairing_and_role_from_one_current_snapshot(web):
    module,client=web
    store=TelegramStore(module.db.engine);store.redeem(1001,store.issue_code(1))
    with store.sessions() as held:
        link,user=store.user(held,1001)
        previous=user.session_version
        assert post(client,'/logout').status_code==302
        account=module.db.session.get(User,1);account.role='Viewer';module.db.session.commit()
        link,user=store.user(held,1001)
        assert link.user_version==user.session_version==previous+1 and user.role=='Viewer'
