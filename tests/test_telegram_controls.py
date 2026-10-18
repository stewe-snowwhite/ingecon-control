"""Enabled command paths use mock Telegram and in-memory Modbus registers only."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from commands import ControlBlocked, execute_devices
from models import CommandAudit, Inverter, TelegramCommand, User
from telegram_store import TelegramAccess
from telegram_control import load_control_policy, policy_from_dict
from tests.test_telegram import asyncio_waker_only, tg


def configuration(ids=(1, 2)):
    # Arbitrary mock registers, explicitly NOT an Ingeteam register map.
    return dict(enabled=True, inverters=[dict(id=number, ip=f'192.0.2.{number}',
        model='mock-test-model', port=1502, device_id=7, register_map_confirmed=True,
        start=dict(address=2000, value=42), stop=dict(address=2001, value=43)) for number in ids])


def enable(tg, ids=(1, 2)):
    tg.store.control_policy = policy_from_dict(configuration(ids))
    tg.link()


@pytest.mark.parametrize('action,address,value', [('on',2000,42), ('off',2001,43)])
@pytest.mark.parametrize('role', ['Administrator', 'Operator'])
def test_confirmed_commands_use_explicit_map_and_do_not_repeat(tg, modbus, action, address, value, role):
    with Session(tg.engine) as session, session.begin():
        session.get(User, 1).role = role
    enable(tg)
    intent, _ = tg.store.prepare(1001, action, 'g', 1)
    assert modbus[1] == []
    result, details = tg.store.confirm(1001, intent)
    assert result == 'confirmed' and len(details) == 2
    assert all(row['outcome'] == 'register_confirmed' for row in details)
    assert details[0]['command'] == dict(model='mock-test-model', port=1502, device_id=7, address=address, value=value)
    assert len(modbus[1]) == 2
    for client in modbus[1]:
        assert (client.port, client.retries) == (1502, 0)
        assert ('write', address, value, 7) in client.calls
        assert ('read', address, 1, 7) in client.calls
        assert client.closed
    with pytest.raises(TelegramAccess, match='використано'):
        tg.store.confirm(1001, intent)
    assert len(modbus[1]) == 2
    with Session(tg.engine) as session:
        audit = session.scalar(select(CommandAudit))
        assert audit.result == 'confirmed' and audit.details == details
        assert audit.telegram_id == 1001 and audit.username == 'admin'


def test_viewer_cannot_execute_even_when_policy_enabled(tg, modbus):
    with Session(tg.engine) as session, session.begin(): session.get(User, 1).role = 'Viewer'
    enable(tg)
    with pytest.raises(TelegramAccess, match='Viewer'):
        tg.store.prepare(1001, 'on', 'i', 1)
    assert modbus[1] == []


def test_whole_group_preflight_blocks_unlisted_target(tg, modbus):
    enable(tg, (1,))
    intent, _ = tg.store.prepare(1001, 'on', 'g', 1)
    assert tg.store.confirm(1001, intent)[0] == 'blocked'
    assert modbus[1] == []


def test_ip_change_and_policy_change_invalidate_intent(tg, modbus):
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'on', 'i', 1)
    changed = configuration()
    changed['inverters'][0]['start']['value'] = 99
    tg.store.control_policy = policy_from_dict(changed)
    assert tg.store.confirm(1001, intent)[0] == 'invalidated'
    with Session(tg.engine) as session, session.begin(): session.get(Inverter, 1).ip_address = '192.0.2.99'
    intent, _ = tg.store.prepare(1001, 'off', 'i', 1)
    assert tg.store.confirm(1001, intent)[0] == 'blocked'
    assert modbus[1] == []


def test_intent_created_while_disabled_cannot_be_enabled_later(tg, modbus):
    tg.link()
    intent, _ = tg.store.prepare(1001, 'on', 'i', 1)
    tg.store.control_policy = policy_from_dict(configuration())
    assert tg.store.confirm(1001, intent)[0] == 'invalidated'
    assert modbus[1] == []


def test_enabled_callback_ui_has_real_results_not_physical_success(tg, modbus):
    enable(tg)
    tg.feed('/control')
    assert 'дозволені' in tg.api.messages[-1].text
    tg.feed(data='act:on:i:1')
    assert 'реальний Modbus-запис' in tg.api.messages[-1].text
    assert modbus[1] == []
    with Session(tg.engine) as session: intent = session.scalar(select(TelegramCommand.id))
    tg.feed(data='confirm:'+intent)
    assert 'фізичний стан інверторів не підтверджено' in tg.api.messages[-1].text
    assert '#1 Mock 1:' in tg.api.messages[-1].text
    assert len(modbus[1]) == 1


def test_command_io_does_not_run_on_telegram_event_loop_thread(tg, modbus, monkeypatch):
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'on', 'i', 1)
    event_loop_thread = threading.get_ident()
    seen = []
    original = tg.store.confirm
    def checked(*args, **kwargs):
        seen.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(tg.store, 'confirm', checked)
    tg.feed(data='confirm:'+intent)
    assert seen and seen[0] != event_loop_thread
    assert len(modbus[1]) == 1


def test_no_database_connection_or_write_lock_during_device_io(tg, modbus, monkeypatch):
    import telegram_store
    enable(tg)
    original = telegram_store.execute_devices
    def checked(*args, **kwargs):
        assert tg.engine.pool.checkedout() == 0
        with tg.engine.begin() as connection:
            assert connection.exec_driver_sql("UPDATE user SET username=username WHERE id=1").rowcount == 1
        return original(*args, **kwargs)
    monkeypatch.setattr(telegram_store, 'execute_devices', checked)
    intent, _ = tg.store.prepare(1001, 'on', 'i', 1)
    assert tg.store.confirm(1001, intent)[0] == 'confirmed'


def test_parallel_confirmations_claim_once(tg, modbus):
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'off', 'i', 1)
    def attempt(_):
        try: return tg.store.confirm(1001, intent)[0]
        except ValueError: return 'already_claimed'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['already_claimed', 'confirmed']
    assert len(modbus[1]) == 1


def test_unconfirmed_and_partial_results(tg, modbus, monkeypatch):
    import modbus_utils
    from tests.fake_modbus import FakeModbusTcpClient
    clients = []
    def factory(host, **kwargs):
        client = FakeModbusTcpClient(host, **kwargs)
        client.write_error = host.endswith('.2')
        clients.append(client)
        return client
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', factory)
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'off', 'g', 1)
    result, rows = tg.store.confirm(1001, intent)
    assert result == 'partial'
    assert [row['outcome'] for row in rows] == ['register_confirmed', 'unconfirmed']
    intent, _ = tg.store.prepare(1001, 'on', 'i', 2)
    assert tg.store.confirm(1001, intent)[0] == 'unconfirmed'
    assert len(clients) == 3


def test_crash_after_claim_is_unknown_and_never_replayed(tg, modbus, monkeypatch):
    import telegram_store
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'on', 'i', 1)
    def crash(*args, **kwargs): raise KeyboardInterrupt()
    monkeypatch.setattr(telegram_store, 'execute_devices', crash)
    with pytest.raises(KeyboardInterrupt): tg.store.confirm(1001, intent)
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand, intent).state == 'processing'
    tg.store.recover()
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand, intent).state == 'unknown'
        assert session.scalar(select(CommandAudit.result)) == 'unknown'
    with pytest.raises(ValueError): tg.store.confirm(1001, intent)
    assert modbus[1] == []


def test_final_database_failure_does_not_repeat_a_written_command(tg, modbus):
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'on', 'i', 1)
    original = tg.store.sessions
    class FailFinalCommit:
        calls = 0
        @contextmanager
        def begin(self):
            self.calls += 1
            with original.begin() as session:
                yield session
                if self.calls == 2:
                    raise OperationalError('synthetic', {}, Exception('synthetic'))
    tg.store.sessions = FailFinalCommit()
    try:
        with pytest.raises(OperationalError): tg.store.confirm(1001, intent)
    finally:
        tg.store.sessions = original
    assert len(modbus[1]) == 1
    with pytest.raises(TelegramAccess): tg.store.confirm(1001, intent)
    tg.store.recover()
    with Session(tg.engine) as session:
        assert session.get(TelegramCommand, intent).state == 'unknown'
    assert len(modbus[1]) == 1


@pytest.mark.parametrize('change,expected', [('expire','expired'), ('cancel','cancelled'), ('role','invalidated')])
def test_enabled_mode_preserves_expiry_cancel_and_role_checks(tg, modbus, change, expected):
    enable(tg)
    intent, _ = tg.store.prepare(1001, 'off', 'i', 1)
    if change == 'expire': tg.clock[0] += 121
    elif change == 'role':
        with Session(tg.engine) as session, session.begin(): session.get(User, 1).role = 'Viewer'
    assert tg.store.confirm(1001, intent, cancel=change == 'cancel')[0] == expected
    assert modbus[1] == []


def test_shared_boundary_still_requires_write_gate_and_rejects_power_limit(tg, modbus):
    policy = policy_from_dict(configuration())
    with Session(tg.engine) as session:
        user, target = session.get(User, 1), session.get(Inverter, 1)
        with pytest.raises(ControlBlocked):
            execute_devices(user, 'on', [target], source='telegram', telegram_policy=policy)
        with pytest.raises(ControlBlocked):
            execute_devices(user, 'limit', [target], source='telegram', telegram_policy=policy, writes_enabled=True)
    assert modbus[1] == []


@pytest.mark.parametrize('mutation', ['enabled_string','unconfirmed','bad_address','bad_value','bad_unit',
    'bad_port','bool_id','duplicate','missing_model','loopback','unknown_key','empty'])
def test_bad_control_policy_rejected(mutation):
    config = configuration()
    row = config['inverters'][0]
    if mutation == 'enabled_string': config['enabled'] = 'true'
    elif mutation == 'unconfirmed': row['register_map_confirmed'] = False
    elif mutation == 'bad_address': row['start']['address'] = -1
    elif mutation == 'bad_value': row['stop']['value'] = 65536
    elif mutation == 'bad_unit': row['device_id'] = 0
    elif mutation == 'bad_port': row['port'] = 65536
    elif mutation == 'bool_id': row['id'] = True
    elif mutation == 'duplicate': config['inverters'].append(copy.deepcopy(row))
    elif mutation == 'missing_model': row.pop('model')
    elif mutation == 'loopback': row['ip'] = '127.0.0.1'
    elif mutation == 'unknown_key': config['allow_all'] = True
    else: config['inverters'] = []
    with pytest.raises(ValueError): policy_from_dict(config)


def test_private_config_and_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv('INGECON_TELEGRAM_CONTROL_CONFIG', raising=False)
    assert not load_control_policy().enabled
    path = tmp_path/'control.json'
    path.write_text(json.dumps(configuration()))
    path.chmod(0o600)
    assert load_control_policy(path).enabled
    path.chmod(0o644)
    with pytest.raises(ValueError): load_control_policy(path)
    path.chmod(0o600)
    link = tmp_path/'link.json'
    link.symlink_to(path)
    with pytest.raises(ValueError): load_control_policy(link)
    path.write_text('{"enabled":false,"enabled":true,"inverters":[]}')
    with pytest.raises(ValueError): load_control_policy(path)


def test_control_mode_leaves_probes_blocked_but_keeps_mock_command_client(monkeypatch):
    import modbus_utils
    import monitoring
    from device_safety import DeviceTransportBlocked, disable_device_transports
    client = modbus_utils.ModbusTcpClient
    for name in ('icmp','tcp','modbus','poll'):
        monkeypatch.setattr(monitoring.NetworkProbes, name, getattr(monitoring.NetworkProbes, name))
    disable_device_transports(allow_control=True)
    assert modbus_utils.ModbusTcpClient is client
    with pytest.raises(DeviceTransportBlocked): monitoring.NetworkProbes().poll(None)


def test_offline_config_cli_does_not_open_database_or_load_token(tmp_path, monkeypatch, capsys):
    import telegram_bot
    path = tmp_path/'control.json'
    path.write_text(json.dumps(configuration()))
    path.chmod(0o600)
    def forbidden(*args, **kwargs): raise AssertionError('No token/DB/API access during validation')
    monkeypatch.setattr(telegram_bot, 'load_token', forbidden)
    monkeypatch.setattr(telegram_bot, 'open_engine', forbidden)
    monkeypatch.setattr(telegram_bot, 'Bot', forbidden)
    telegram_bot.main(['check-control-config', '--file', str(path)])
    assert 'без мережі' in capsys.readouterr().out
