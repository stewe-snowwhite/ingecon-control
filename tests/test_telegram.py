import asyncio
import hashlib
import io
import logging
import re
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from commands import ControlBlocked, execute_devices
from models import (db, User, Inverter, MonitorConfig, MonitorHeartbeat, InverterStatus, StateEvent,
    TelegramLinkCode, TelegramLink, TelegramCommand, CommandAudit, TelegramDelivery, TelegramUpdate, TELEGRAM_TABLES)
from monitoring import target_for
from telegram_bot import TokenRedactor, check_schema, load_token, prepare_database, run_polling
from telegram_store import TelegramAccess, TelegramStore
from telegram_ui import build_dispatcher, notification_batch
from tests.fake_telegram import FakeTelegramSession
from tests.helpers import post

ORIGINAL_SOCKET_INIT = socket.socket.__init__
FAKE_TOKEN = '123456:'+'test_only_synthetic_token_'*2


@pytest.fixture(autouse=True)
def asyncio_waker_only(monkeypatch):
    # asyncio uses an AF_UNIX socketpair for its internal waker. Permit wrapping
    # only those existing descriptors; IP sockets/connect/DNS remain forbidden.
    def initializer(self,family=socket.AF_INET,type=socket.SOCK_STREAM,proto=0,fileno=None):
        if family!=socket.AF_UNIX or fileno is None:
            raise AssertionError('Only asyncio local socketpair descriptors are allowed')
        return ORIGINAL_SOCKET_INIT(self,family,type,proto,fileno)
    monkeypatch.setattr(socket.socket,'__init__',initializer)


@pytest.fixture
def tg(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'telegram.sqlite'))
    db.metadata.create_all(engine)
    now = [100000.0]
    with Session(engine) as session,session.begin():
        user = User(username='admin',role='Administrator')
        user.set_password('synthetic-test-password')
        session.add(user)
        for number in range(1,4):
            inv = Inverter(id=number,name=f'Mock {number}',group='A' if number<3 else 'B',
                           ip_address=f'192.0.2.{number}',password='private-device-sentinel')
            session.add(inv)
            session.flush()
            config = MonitorConfig(inverter_id=number,read_address=10)
            session.add(config)
            session.flush()
            session.add(InverterStatus(inverter_id=number,target_key=target_for(inv,config).key,
                                      state='Online',checked_at=now[0],icmp_ok=False,tcp_ok=True,modbus_ok=True))
        session.add(MonitorHeartbeat(id=1,owner='mock',started_at=now[0],last_seen=now[0],
                                    interval_seconds=30,stopped=False))
    store = TelegramStore(engine,clock=lambda:now[0])
    api = FakeTelegramSession()
    bot = Bot(FAKE_TOKEN,session=api)
    dispatcher = build_dispatcher(store)
    counter = [0]
    def link(uid=1001,user_id=1):
        return store.redeem(uid,store.issue_code(user_id))
    def feed(text=None,data=None,uid=1001,update_id=None,chat_type='private',chat_id=None):
        counter[0] += 1
        sender = dict(id=uid,is_bot=False,first_name='Mock')
        message = dict(message_id=counter[0],date=100000,chat=dict(id=chat_id or uid,type=chat_type),from_user=sender)
        if data is not None:
            raw = dict(callback_query=dict(id=str(counter[0]),from_user=sender,chat_instance='mock',data=data,
                message=dict(message,text='button')))
        else:
            raw = dict(message=dict(message,text=text))
        update = Update.model_validate(dict(update_id=update_id if update_id is not None else counter[0],**raw))
        return asyncio.run(dispatcher.feed_update(bot,update))
    yield SimpleNamespace(engine=engine,store=store,clock=now,api=api,bot=bot,link=link,feed=feed,dispatcher=dispatcher)
    asyncio.run(bot.session.close())
    engine.dispose()


def event(tg,state='Offline',inverter_id=1,at=None):
    with Session(tg.engine) as session,session.begin():
        row = StateEvent(inverter_id=inverter_id,inverter_name='Mock <b>name</b>',target_key='mock',
            previous_state='Online',state=state,occurred_at=tg.clock[0] if at is None else at)
        session.add(row)
        session.flush()
        return row.id


def test_pair_code_is_hashed_one_time_and_private(tg):
    code = tg.store.issue_code(1)
    with Session(tg.engine) as session:
        row = session.get(TelegramLinkCode,1)
        assert row.digest==hashlib.sha256(code.encode()).hexdigest() and code not in row.digest
    tg.feed('/start '+code)
    assert 'Прив’язано: admin' in tg.api.messages[-1].text
    assert tg.api.messages[-1].reply_markup
    with pytest.raises(TelegramAccess):
        tg.store.redeem(1002,code)
    with pytest.raises(TelegramAccess):
        tg.store.issue_code(1)


def test_pair_code_replacement_expiry_and_rate_limit(tg):
    old = tg.store.issue_code(1)
    code = tg.store.issue_code(1)
    with pytest.raises(TelegramAccess): tg.store.redeem(1001,old)
    tg.clock[0] += 301
    with pytest.raises(TelegramAccess): tg.store.redeem(1001,code)
    code = tg.store.issue_code(1)
    for _ in range(4):
        with pytest.raises(TelegramAccess): tg.store.redeem(1001,'invalid')
    with pytest.raises(TelegramAccess): tg.store.redeem(1001,code)
    tg.clock[0] += 301
    code = tg.store.issue_code(1)
    assert tg.store.redeem(1001,code)[0]=='admin'


def test_pair_code_atomic_claim_two_users(tg):
    code = tg.store.issue_code(1)
    def attempt(uid):
        try:
            tg.store.redeem(uid,code)
            return True
        except TelegramAccess:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt,(1001,1002)))==1


@pytest.mark.parametrize('command',['start','status','inverters','groups','history','help'])
def test_commands_mock_telegram_api_and_db_only(tg,modbus,command):
    tg.link()
    tg.feed('/'+command)
    assert tg.api.messages and tg.api.messages[-1].chat_id==1001
    assert tg.api.messages[-1].parse_mode is None
    assert 'private-device-sentinel' not in str(tg.api.messages)
    assert modbus[1]==[]
    if command == 'help':
        assert 'Налаштування → Telegram → Створити код' in tg.api.messages[-1].text


def test_stale_status_and_history_use_cached_data(tg):
    tg.link()
    event(tg)
    tg.feed('/status')
    assert 'Online: 3' in tg.api.messages[-1].text
    tg.clock[0] += 1000
    tg.feed('/status')
    assert 'Unknown: 3' in tg.api.messages[-1].text and 'застарілі' in tg.api.messages[-1].text
    tg.feed('/history')
    assert 'Online → Offline' in tg.api.messages[-1].text and 'UTC' in tg.api.messages[-1].text


@pytest.mark.parametrize('command',['status','inverters','groups','history','control','notifications'])
def test_unlinked_users_get_no_inventory(tg,command):
    tg.feed('/'+command)
    assert 'Спочатку' in tg.api.messages[-1].text
    assert 'Mock 1' not in tg.api.messages[-1].text


@pytest.mark.parametrize('kind',['group','supergroup','channel','wrong-private'])
def test_private_chat_only(tg,kind):
    code = tg.store.issue_code(1)
    tg.feed('/start '+code,chat_type='private' if kind=='wrong-private' else kind,chat_id=2002)
    assert tg.api.calls==[]
    assert tg.store.redeem(1001,code)[0]=='admin'


@pytest.mark.parametrize('change',['inactive','deleted','password','explicit-version-revoke'])
def test_live_authorization_rechecked(tg,change):
    tg.link()
    with Session(tg.engine) as session,session.begin():
        user = session.get(User,1)
        if change=='inactive': user.active=False
        elif change=='deleted': session.delete(user)
        elif change=='password': user.set_password('changed-test-password')
        else: user.session_version += 1
    tg.feed('/inverters')
    assert 'Спочатку' in tg.api.messages[-1].text


def test_duplicate_updates_persist_across_store_restart(tg):
    tg.link()
    tg.feed('/status',update_id=50)
    sent = len(tg.api.messages)
    tg.feed('/status',update_id=50)
    assert len(tg.api.messages)==sent
    restarted = TelegramStore(tg.engine,clock=lambda:tg.clock[0])
    assert not restarted.accept_update(tg.bot.id,50,1001)
    assert restarted.accept_update(tg.bot.id+1,50,1001)


def test_pairing_survives_new_engine_dispatcher_and_bot_with_mock_api(tg,modbus):
    tg.link();tg.store.preferences(1001,'notifications')
    restarted_engine=create_engine(str(tg.engine.url))
    api=FakeTelegramSession();bot=Bot(FAKE_TOKEN,session=api)
    try:
        store=TelegramStore(restarted_engine,clock=lambda:tg.clock[0])
        store.recover();store.cleanup()
        assert store.identity(1001)==('admin','Administrator')
        update=Update.model_validate(dict(update_id=501,message=dict(message_id=501,date=100000,
            chat=dict(id=1001,type='private'),from_user=dict(id=1001,is_bot=False,first_name='Mock'),text='/status')))
        asyncio.run(build_dispatcher(store).feed_update(bot,update))
        assert 'Online: 3' in api.messages[-1].text
        assert store.web_link(1).notifications and modbus[1]==[]
    finally:
        asyncio.run(bot.session.close());restarted_engine.dispose()


@pytest.mark.parametrize('role',['Administrator','Operator','Viewer'])
def test_role_menu_and_two_phase_control_always_blocked(tg,modbus,role):
    with Session(tg.engine) as session,session.begin(): session.get(User,1).role=role
    tg.link()
    tg.feed('/start')
    markup = tg.api.messages[-1].reply_markup.model_dump_json()
    assert ('menu:control' in markup)==(role!='Viewer')
    tg.feed(data='act:on:g:1')
    if role=='Viewer':
        assert 'Viewer' in tg.api.messages[-1].text
        with Session(tg.engine) as session:
            assert session.scalar(select(CommandAudit)).result=='denied'
        return
    assert 'Підтвердьте START' in tg.api.messages[-1].text
    assert '#1 Mock 1' in tg.api.messages[-1].text and '#2 Mock 2' in tg.api.messages[-1].text
    with Session(tg.engine) as session:
        intent = session.scalar(select(TelegramCommand)).id
        assert session.scalar(select(CommandAudit)).result=='awaiting'
    assert modbus[1]==[]
    tg.feed(data='confirm:'+intent)
    assert 'Заблоковано' in tg.api.messages[-1].text
    with Session(tg.engine) as session:
        audit = session.scalar(select(CommandAudit))
        assert (audit.telegram_id,audit.user_id,audit.username,audit.action,audit.result)==(1001,1,'admin','on','blocked')
        assert len(audit.details)==2 and audit.occurred_at==tg.clock[0]
    tg.feed(data='confirm:'+intent)
    assert 'використано' in tg.api.messages[-1].text and modbus[1]==[]


@pytest.mark.parametrize('change,expected',[('expire','expired'),('role','invalidated'),('ip','invalidated'),
    ('name','invalidated'),('new-group-member','invalidated'),('delete','invalidated'),('cancel','cancelled')])
def test_confirmation_rejects_changes_expiry_and_cancel(tg,modbus,change,expected):
    tg.link()
    intent,_ = tg.store.prepare(1001,'off','g',1)
    with Session(tg.engine) as session,session.begin():
        if change=='role': session.get(User,1).role='Viewer'
        elif change=='ip': session.get(Inverter,1).ip_address='192.0.2.99'
        elif change=='name': session.get(Inverter,1).name='Changed'
        elif change=='new-group-member': session.get(Inverter,3).group='A'
        elif change=='delete': session.delete(session.get(Inverter,1))
    if change=='expire': tg.clock[0] += 121
    assert tg.store.confirm(1001,intent,cancel=change=='cancel')[0]==expected
    assert modbus[1]==[]


def test_confirmation_cannot_be_stolen_and_unlink_revokes(tg):
    tg.link()
    intent,_ = tg.store.prepare(1001,'on','i',1)
    with pytest.raises(TelegramAccess): tg.store.confirm(1002,intent)
    tg.store.unlink(1)
    with pytest.raises(TelegramAccess): tg.store.confirm(1001,intent)
    with Session(tg.engine) as session: assert session.get(TelegramCommand,intent).state=='cancelled'


def test_shared_service_cannot_enable_telegram_writes(tg,modbus):
    with Session(tg.engine) as session:
        with pytest.raises(ControlBlocked,match='окремого дозволу'):
            execute_devices(session.get(User,1),'on',[session.get(Inverter,1)],source='telegram',writes_enabled=True)
    assert modbus[1]==[]


def test_notification_opt_in_dedup_and_restart(tg):
    tg.link()
    event(tg)
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert not tg.api.messages
    tg.store.preferences(1001,'notifications')
    event_id = event(tg)
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert len(tg.api.messages)==1 and f'#{event_id}' in tg.api.messages[0].text
    asyncio.run(notification_batch(TelegramStore(tg.engine,clock=lambda:tg.clock[0]),tg.bot))
    assert len(tg.api.messages)==1
    with Session(tg.engine) as session:
        delivery = session.scalar(select(TelegramDelivery))
        assert delivery.state=='sent' and delivery.message_id is not None
        assert session.get(StateEvent,event_id).notified_at is None  # Global legacy ack is not per recipient.


@pytest.mark.parametrize('state',['Online','Offline','Degraded'])
def test_notification_state_toggles(tg,state):
    tg.link()
    tg.store.preferences(1001,'notifications')
    tg.store.preferences(1001,state.lower())
    event(tg,state)
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert not tg.api.messages
    tg.store.preferences(1001,state.lower())
    event(tg,state)
    asyncio.run(notification_batch(tg.store,tg.bot))
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert len(tg.api.messages)==1


def test_notification_groups_unknown_and_old_backlog_filtered(tg):
    event(tg)  # Before binding.
    tg.link()
    tg.store.preferences(1001,'notifications')
    tg.store.preferences(1001,'group',3)
    event(tg,'Unknown',3)
    event(tg,'Offline',1)
    event(tg,'Offline',3,at=tg.clock[0]-90000)
    event(tg,'Degraded',3)
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert len(tg.api.messages)==1 and 'Degraded' in tg.api.messages[0].text


@pytest.mark.parametrize('revoke',['unlink','inactive','preferences'])
def test_queued_delivery_rechecks_permissions(tg,revoke):
    tg.link()
    tg.store.preferences(1001,'notifications')
    event(tg)
    tg.store.fanout()
    if revoke=='unlink': tg.store.unlink(1)
    elif revoke=='preferences': tg.store.preferences(1001,'notifications')
    else:
        with Session(tg.engine) as session,session.begin(): session.get(User,1).active=False
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert not tg.api.messages


@pytest.mark.parametrize('failure,expected', [('timeout','unknown'),('forbidden','failed'),('retry','pending')])
def test_notification_transport_failures(tg,failure,expected):
    tg.link()
    tg.store.preferences(1001,'notifications')
    event(tg)
    method = SendMessage(chat_id=1001,text='mock')
    error = (TelegramNetworkError(method=method,message='timeout') if failure=='timeout' else
             TelegramForbiddenError(method=method,message='forbidden') if failure=='forbidden' else
             TelegramRetryAfter(method=method,message='rate',retry_after=30))
    tg.api.failures.append(error)
    asyncio.run(notification_batch(tg.store,tg.bot))
    with Session(tg.engine) as session:
        assert session.scalar(select(TelegramDelivery)).state==expected
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert len(tg.api.messages)==1  # No ambiguous retry / early retry.
    if failure=='retry':
        tg.clock[0] += 31
        asyncio.run(notification_batch(tg.store,tg.bot))
        with Session(tg.engine) as session: assert session.scalar(select(TelegramDelivery)).state=='sent'


def test_notification_interrupted_send_not_resent(tg):
    tg.link()
    tg.store.preferences(1001,'notifications')
    event(tg)
    tg.store.fanout()
    assert tg.store.claim_delivery()
    tg.store.recover()
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert not tg.api.messages
    with Session(tg.engine) as session: assert session.scalar(select(TelegramDelivery)).state=='unknown'


@pytest.mark.parametrize('role',['Administrator','Operator','Viewer'])
def test_web_pairing_csrf_roles_and_revoke(web,role):
    module,client = web
    user = module.db.session.get(User,1)
    user.role=role
    module.db.session.commit()
    assert module.app.test_client().get('/telegram').status_code==401
    assert client.get('/telegram').status_code==200
    assert client.post('/telegram',data={'action':'code'}).status_code==400
    response = post(client,'/telegram',{'action':'code'})
    code = re.search(r'/start ([A-Za-z0-9_-]{32})',response.get_data(as_text=True)).group(1)
    assert response.headers['Cache-Control']=='no-store'
    assert code not in client.get('/telegram').get_data(as_text=True)
    store = TelegramStore(module.db.engine)
    store.redeem(1001,code)
    assert '1001' in client.get('/telegram').get_data(as_text=True)
    assert post(client,'/telegram',{'action':'unlink'}).status_code==302
    with pytest.raises(TelegramAccess): store.identity(1001)


def test_web_no_automatic_migration(web):
    module,client = web
    module.db.session.remove()
    db.metadata.drop_all(module.db.engine,tables=TELEGRAM_TABLES)
    assert client.get('/telegram').status_code==503
    assert 'telegram_link' not in inspect(module.db.engine).get_table_names()


def test_prepare_schema_makes_backup_preserving_old_records(tg,tmp_path):
    db.metadata.drop_all(tg.engine,tables=TELEGRAM_TABLES)
    backup = tmp_path/'backup.sqlite'
    prepare_database(tmp_path/'telegram.sqlite',backup)
    assert backup.stat().st_mode&0o777==0o600
    # SQLite backup is a consistent logical snapshot, including WAL; its file
    # header need not be byte-identical to the source.
    with sqlite3.connect(backup) as connection:
        assert connection.execute('SELECT username FROM user').fetchall()==[('admin',)]
        assert connection.execute('SELECT count(*) FROM inverter').fetchone()[0]==3
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='telegram_link'").fetchall()
    check_schema(tg.engine)
    with Session(tg.engine) as session:
        assert session.get(User,1).username=='admin' and session.get(Inverter,1).password=='private-device-sentinel'
    with pytest.raises(FileExistsError): prepare_database(tmp_path/'telegram.sqlite',backup)


def test_token_environment_file_permissions_and_redaction(tmp_path,monkeypatch):
    monkeypatch.delenv('INGECON_TELEGRAM_TOKEN',raising=False)
    with pytest.raises(ValueError): load_token()
    path = tmp_path/'bot.key'
    path.write_text(FAKE_TOKEN)
    path.chmod(0o600)
    assert load_token(path)==FAKE_TOKEN
    path.chmod(0o644)
    with pytest.raises(ValueError): load_token(path)
    monkeypatch.setenv('INGECON_TELEGRAM_TOKEN',FAKE_TOKEN)
    assert load_token()==FAKE_TOKEN
    with pytest.raises(ValueError): load_token(path)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(TokenRedactor(FAKE_TOKEN))
    logger = logging.Logger('synthetic')
    logger.addHandler(handler)
    try:
        raise ValueError('https://api.telegram.org/bot'+FAKE_TOKEN)
    except ValueError:
        logger.error('Token %s',FAKE_TOKEN,exc_info=True)
    assert FAKE_TOKEN not in stream.getvalue() and '[REDACTED]' in stream.getvalue()


@pytest.mark.parametrize('token',['bad','abc:secret','123456:with spaces',''])
def test_invalid_token_is_generic_and_never_echoed(monkeypatch,token):
    monkeypatch.setenv('INGECON_TELEGRAM_TOKEN',token)
    with pytest.raises(ValueError,match='Відсутній або некоректний'):
        load_token()


def test_controls_preferences_and_menu_callbacks(tg):
    tg.link()
    for data in ('menu:status','list:i:0','list:g:0','menu:history','menu:help',
                 'menu:control','target:i:1','target:g:1','menu:notifications',
                 'pref:notifications','pref:offline','list:ng:0','pref:group:3','pref:all'):
        before = len(tg.api.messages)
        tg.feed(data=data)
        assert len(tg.api.messages)==before+1
    assert tg.store.preferences(1001)['notifications']
    assert not tg.store.preferences(1001)['offline']
    assert tg.store.preferences(1001)['group_filter'] is None


@pytest.mark.parametrize('data',['act:on:i:999','act:write:i:1','act:on:bad:1','act:on:i:-1',
    'confirm:forged','list:i:-1','list:i:not-a-number','pref:role','pref:group:999'])
def test_forged_callback_data_never_controls_or_leaks(tg,modbus,data):
    tg.link()
    tg.feed(data=data)
    assert tg.api.messages and 'private-device-sentinel' not in tg.api.messages[-1].text
    assert modbus[1]==[]


def test_pagination_and_large_group_limit(tg):
    tg.link()
    with Session(tg.engine) as session,session.begin():
        for index in range(4,15):
            session.add(Inverter(id=index,name=f'Mock {index}',group='A',ip_address=f'192.0.2.{index}'))
    tg.feed('/inverters')
    assert 'list:i:1' in tg.api.messages[-1].reply_markup.model_dump_json()
    tg.feed(data='list:i:1')
    assert '#9 Mock 9' in tg.api.messages[-1].text
    tg.feed(data='act:on:g:1')
    assert 'Максимум 10' in tg.api.messages[-1].text


def test_preview_superseded_cleanup_and_unlink_audited(tg):
    tg.link()
    first,_ = tg.store.prepare(1001,'on','i',1)
    second,_ = tg.store.prepare(1001,'off','i',1)
    with Session(tg.engine) as session:
        assert session.scalar(select(CommandAudit).where(CommandAudit.request_id=='tg:'+first)).result=='superseded'
    tg.clock[0] += 121
    tg.store.cleanup()
    with Session(tg.engine) as session:
        assert session.scalar(select(CommandAudit).where(CommandAudit.request_id=='tg:'+second)).result=='expired'
    third,_ = tg.store.prepare(1001,'on','i',1)
    tg.store.unlink(1)
    with Session(tg.engine) as session:
        assert session.scalar(select(CommandAudit).where(CommandAudit.request_id=='tg:'+third)).result=='cancelled'


def test_notifications_have_independent_recipient_dedup(tg):
    with Session(tg.engine) as session,session.begin():
        user = User(username='viewer',role='Viewer')
        user.set_password('synthetic-viewer-password')
        session.add(user)
    tg.link()
    tg.link(1002,2)
    for uid in (1001,1002): tg.store.preferences(uid,'notifications')
    event(tg)
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert {message.chat_id for message in tg.api.messages}=={1001,1002}
    asyncio.run(notification_batch(tg.store,tg.bot))
    assert len(tg.api.messages)==2


def test_polling_wiring_mock_api_and_graceful_close(tg,monkeypatch):
    import telegram_bot
    seen = {}
    class MockDispatcher:
        async def start_polling(self,bot,**kwargs):
            seen.update(kwargs)
            await asyncio.sleep(0)
    monkeypatch.setattr(telegram_bot,'AiohttpSession',lambda **kwargs:tg.api)
    monkeypatch.setattr(telegram_bot,'Bot',lambda *args,**kwargs:tg.bot)
    monkeypatch.setattr(telegram_bot,'build_dispatcher',lambda store:MockDispatcher())
    asyncio.run(run_polling(tg.store,FAKE_TOKEN))
    assert seen['handle_as_tasks'] is False and seen['polling_timeout']==25
    assert seen['allowed_updates']==['message','callback_query'] and tg.api.closed
    assert [type(call).__name__ for call in tg.api.calls]==['GetWebhookInfo','SetMyCommands']


def test_schema_missing_and_demo_database_fail_before_polling(tg):
    with tg.engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE demo_metadata (key TEXT, value TEXT)')
    with pytest.raises(ValueError,match='Архівна тестова БД'): check_schema(tg.engine)
    with tg.engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE demo_metadata')
        TelegramLinkCode.__table__.drop(connection)
    with pytest.raises(ValueError,match='Схема Telegram'): check_schema(tg.engine)


def test_no_database_connection_held_during_telegram_api(tg,monkeypatch):
    original = tg.api.make_request
    async def checked_request(bot,method,timeout=None):
        assert tg.engine.pool.checkedout()==0
        return await original(bot,method,timeout)
    monkeypatch.setattr(tg.api,'make_request',checked_request)
    tg.link()
    tg.feed('/status')
    tg.feed(data='act:off:i:1')
    tg.store.preferences(1001,'notifications')
    event(tg)
    asyncio.run(notification_batch(tg.store,tg.bot))


def test_backup_captures_wal_data(tg,tmp_path):
    db.metadata.drop_all(tg.engine,tables=TELEGRAM_TABLES)
    with tg.engine.connect() as connection:
        assert connection.exec_driver_sql('PRAGMA journal_mode=WAL').scalar()=='wal'
        connection.commit()
        with Session(tg.engine) as session,session.begin(): session.get(Inverter,1).name='Committed WAL data'
        prepare_database(tmp_path/'telegram.sqlite',tmp_path/'wal-backup.sqlite')
        with sqlite3.connect(tmp_path/'wal-backup.sqlite') as backup:
            assert backup.execute('SELECT name FROM inverter WHERE id=1').fetchone()[0]=='Committed WAL data'
