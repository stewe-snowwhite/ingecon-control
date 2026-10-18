import json
import re
from dataclasses import asdict, replace

import pytest
from sqlalchemy import select

from models import CommandAudit, Inverter, User, db
from modbus_config import (ConfigurationConflict, ModbusConfiguration, configuration_path,
                           load_configuration, save_configuration, validate_configuration)
from input_registers import INPUT_REGISTERS
from monitoring import NetworkProbes, Target
from tests.helpers import post


ROOT = '/settings/modbus'


def seed():
    inv = Inverter(name='Registered mock', group='A', ip_address='192.0.2.1', password='private-device-secret')
    db.session.add(inv)
    db.session.commit()
    return inv


def control_data(**changes):
    data = dict(inverter_id='1', action='on', value='50')
    data.update(changes)
    return data


def register_data(**changes):
    data = dict(inverter_id='1', ip='192.0.2.1', port='502', device_id='1',
                function='3', address='1001', count='1', values='')
    data.update(changes)
    return data


def confirm(client, path, data):
    response = post(client, path, data)
    assert response.status_code == 200, response.text
    match = re.search(r'name="confirmation_token" value="([^"]+)"', response.text)
    assert match
    return post(client, path, dict(data, confirmation_token=match.group(1)))


def test_defaults_preserve_requested_historical_map():
    config = load_configuration()
    assert config.command('on') == (1000, 6)
    assert config.command('off') == (1000, 5)
    assert config.command('limit', 37) == (46, 37)
    assert (config.limit_read_address, config.limit_reason_address) == (1001, 47)
    assert not configuration_path().exists()


def test_config_atomic_revision_and_stale_edit(tmp_path):
    current = load_configuration()
    updated = save_configuration(replace(current, start_address=1234), current.fingerprint)
    assert updated.revision == 1 and load_configuration() == updated
    assert configuration_path().stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConfigurationConflict): save_configuration(current, current.fingerprint)
    assert load_configuration() == updated
    assert list(tmp_path.glob('.modbus-*')) == []


@pytest.mark.parametrize('change', [dict(port=0), dict(port=65536), dict(device_id=0), dict(device_id=248),
    dict(start_address=-1), dict(stop_value=65536), dict(timeout=0), dict(timeout=11),
    dict(start_value=True), dict(limit_address=1.5), dict(writes_enabled=True), dict(verified=True)])
def test_invalid_config_rejected(change):
    data = asdict(ModbusConfiguration())
    data.update(change)
    with pytest.raises(ValueError): validate_configuration(data)


def test_private_file_validation_no_fallback(tmp_path):
    path = configuration_path()
    path.write_text(json.dumps(asdict(ModbusConfiguration())))
    path.chmod(0o644)
    with pytest.raises(ValueError): load_configuration()
    path.chmod(0o600)
    path.write_text('{"port":502,"port":1502}')
    with pytest.raises(ValueError): load_configuration()
    path.unlink()  # Synthetic tmp_path only.
    path.symlink_to(tmp_path/'other.json')
    with pytest.raises(OSError): load_configuration()


@pytest.mark.parametrize('role,code', [(None,401), ('Viewer',403), ('Operator',403), ('Administrator',200)])
@pytest.mark.parametrize('path', [ROOT, ROOT+'/registers'])
def test_settings_and_testing_are_admin_only(web, role, code, path):
    module, client = web
    seed()
    if role is None: client = module.app.test_client()
    else:
        db.session.get(User, 1).role = role
        db.session.commit()
    assert client.get(path).status_code == code
    response = post(client, path, {})
    assert response.status_code == (400 if role == 'Administrator' else code)


def test_settings_save_global_configuration_and_audit(web):
    _, client = web
    config = load_configuration()
    values = {key: str(value) for key, value in asdict(config).items() if key != 'revision'}
    values.update(start_address='1234', device_id='7', config_fingerprint=config.fingerprint)
    response = post(client, ROOT, values)
    assert response.status_code == 302
    assert load_configuration().start_address == 1234 and load_configuration().device_id == 7
    audit = db.session.scalar(select(CommandAudit))
    assert (audit.action, audit.result, audit.username) == ('modbus_config','saved','admin')
    assert audit.details['after']['start_address'] == 1234
    # No HTTP field can activate commands or mark the map verified.
    values['writes_enabled'] = 'true'
    assert post(client, ROOT, values).status_code == 400
    assert load_configuration().revision == 1
    values.pop('writes_enabled')
    assert post(client, ROOT, values).status_code == 400
    assert load_configuration().revision == 1


@pytest.mark.parametrize('path', [ROOT, ROOT+'/control', ROOT+'/registers'])
def test_csrf_is_required(web, path):
    _, client = web
    seed()
    assert client.post(path, data=control_data()).status_code == 400
    assert not configuration_path().exists()
    assert CommandAudit.query.count() == 0


@pytest.mark.parametrize('action,value,register,word', [('on','50',1000,6), ('off','50',1000,5),
    ('limit','1',46,1), ('limit','100',46,100), ('limit_max','50',46,100)])
def test_control_preview_and_confirmation_never_write(web, modbus, action, value, register, word):
    module, client = web
    seed()
    # Even an explicitly enabled app and mock transport cannot bypass this panel.
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    response = confirm(client, ROOT+'/control', control_data(action=action, value=value))
    assert response.status_code == 200 and 'заблоковано на backend' in response.text
    assert modbus[1] == []
    audit = db.session.scalar(select(CommandAudit))
    assert audit.result == 'blocked'
    assert audit.details['request']['operation']['address'] == register
    assert audit.details['request']['operation']['values'] == [word]
    assert 'private-device-secret' not in str(audit.details) + response.text


@pytest.mark.parametrize('value', ['0','101','1.5','-1','True','1e2',''])
def test_power_percent_is_strict(web, modbus, value):
    _, client = web
    seed()
    assert post(client, ROOT+'/control', control_data(action='limit', value=value)).status_code == 400
    assert modbus[1] == [] and CommandAudit.query.count() == 0


@pytest.mark.parametrize('function,values,count', [('6','6','1'), ('16','5,6','2')])
def test_testing_cannot_write_even_with_all_other_flags_enabled(web, modbus, function, values, count):
    module, client = web
    seed()
    module.app.config.update(MODBUS_WRITES_ENABLED=True, REGISTER_READS_ENABLED=True)
    response = confirm(client, ROOT+'/registers', register_data(function=function, values=values, count=count))
    assert response.status_code == 200 and 'заблоковано на backend' in response.text
    assert modbus[1] == []


@pytest.mark.parametrize('function,method', [('3','read'), ('4','read_input')])
def test_explicit_read_only_after_confirmation(web, modbus, function, method):
    module, client = web
    seed()
    module.app.config['REGISTER_READS_ENABLED'] = True
    response = confirm(client, ROOT+'/registers', register_data(function=function, address='52'))
    assert response.status_code == 200 and 'Читання підтверджено' in response.text
    assert modbus[1][0].calls == [('connect',),(method,52,1,1)]
    assert modbus[1][0].closed and modbus[1][0].retries == 0
    assert not any(call[0] == 'write' for call in modbus[1][0].calls)


def test_default_reading_is_off_and_api_does_not_probe(web, modbus):
    _, client = web
    seed()
    assert client.get(ROOT).status_code == 200
    assert client.get(ROOT+'/registers').status_code == 200
    response = confirm(client, ROOT+'/registers', register_data())
    assert 'читання вимкнене' in response.text
    assert modbus[1] == []


@pytest.mark.parametrize('changes', [dict(function='23'), dict(function='5'), dict(function='22'),
    dict(address='65536'), dict(address='65535',count='2'), dict(port='0'), dict(device_id='248'),
    dict(ip='127.0.0.1'), dict(ip='192.0.2.99'), dict(ip='example.com'), dict(count='0'),
    dict(count='126'), dict(function='6',values='65536'), dict(function='6',values='6',count='2'),
    dict(function='16',values='1',count='2'), dict(values='1'), dict(values='nan'), dict(writes_enabled='true')])
def test_raw_parameter_validation(web, modbus, changes):
    _, client = web
    seed()
    assert post(client, ROOT+'/registers', register_data(**changes)).status_code == 400
    assert modbus[1] == [] and CommandAudit.query.count() == 0


def test_confirmation_tampering_replay_and_config_changes(web, modbus):
    _, client = web
    seed()
    data = register_data(function='6', values='6', address='1000')
    response = post(client, ROOT+'/registers', data)
    signed = re.search(r'name="confirmation_token" value="([^"]+)"', response.text).group(1)
    assert post(client, ROOT+'/registers', dict(data, address='46', confirmation_token=signed)).status_code == 400
    assert post(client, ROOT+'/registers', dict(data, confirmation_token=signed)).status_code == 200
    assert post(client, ROOT+'/registers', dict(data, confirmation_token=signed)).status_code == 400
    response = post(client, ROOT+'/registers', data)
    signed = re.search(r'name="confirmation_token" value="([^"]+)"', response.text).group(1)
    config = load_configuration()
    save_configuration(replace(config, start_value=9), config.fingerprint)
    assert post(client, ROOT+'/registers', dict(data, confirmation_token=signed)).status_code == 400
    assert modbus[1] == []


def test_backend_interlock_covers_legacy_generic_and_telegram_paths(modbus, monkeypatch):
    import modbus_safety
    from commands import ControlBlocked, execute_devices
    from telegram_control import policy_from_dict
    from types import SimpleNamespace
    module, clients = modbus
    monkeypatch.setattr(modbus_safety, 'PHYSICAL_WRITES_LOCKED', True)
    for method in (module.modbus_turn_on, module.modbus_turn_off, module.modbus_limit_power):
        assert not method('192.0.2.1', allow_write=True).confirmed
    assert not module.modbus_control('192.0.2.1', address=1000, value=6, device_id=1, allow_write=True).confirmed
    from tests.test_telegram_controls import configuration
    policy = policy_from_dict(configuration())
    user = SimpleNamespace(active=True, role='Administrator')
    inv = SimpleNamespace(id=1, name='Mock', ip_address='192.0.2.1')
    with pytest.raises(ControlBlocked, match='backend'):
        execute_devices(user, 'on', [inv], source='telegram', writes_enabled=True, telegram_policy=policy)
    assert clients == []


def test_legacy_read_limits_use_configured_addresses(modbus):
    module, clients = modbus
    assert module.read_legacy_limits('192.0.2.1') == (32767, 9)
    assert clients[0].calls == [('connect',),('read',1001,1,1),('read',47,1,1)]


def test_one_global_configuration_is_used_for_all_inverters(modbus):
    from commands import execute_devices
    from types import SimpleNamespace
    config = load_configuration()
    save_configuration(replace(config, start_address=1234, start_value=99, port=1502, device_id=7, timeout=4), config.fingerprint)
    targets = [SimpleNamespace(id=n, name=f'Mock {n}', ip_address=f'192.0.2.{n}') for n in (1,2)]
    actor = SimpleNamespace(active=True, role='Administrator')
    results = execute_devices(actor, 'on', targets, writes_enabled=True)
    assert len(results) == 2
    assert len(modbus[1]) == 2
    for client in modbus[1]:
        assert client.port == 1502 and client.timeout == 4 and ('write',1234,99,7) in client.calls


def test_status_and_limit_readers_use_global_transport_and_addresses(modbus):
    module, clients = modbus
    config = load_configuration()
    save_configuration(replace(config, start_address=2000, limit_read_address=3000,
        limit_reason_address=3001, port=1502, device_id=7), config.fingerprint)
    assert module.read_command_status('192.0.2.1') == (0, 0)
    assert clients[0].port == 1502
    assert clients[0].calls == [('connect',),('read',2000,1,7),('read',3000,1,7)]
    assert module.read_legacy_limits('192.0.2.1') == (0, 0)
    assert clients[1].calls == [('connect',),('read',3000,2,7)]


def test_backend_write_lock_is_default_and_not_environment_configurable(monkeypatch):
    import modbus_utils
    import modbus_safety
    monkeypatch.setenv('MODBUS_WRITES_ENABLED', 'true')
    monkeypatch.setenv('INGECON_MODBUS_WRITES_ENABLED', 'true')
    assert modbus_safety.writes_locked()
    def forbidden(*args, **kwargs): raise AssertionError('Client construction must be unreachable')
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', forbidden)
    assert not modbus_utils.modbus_turn_on('192.0.2.1', allow_write=True).confirmed
    assert not modbus_utils.modbus_control('192.0.2.1', address=46, value=50, device_id=1, allow_write=True).confirmed


def test_input_map_is_separate_and_monitor_uses_only_two_fc04_blocks(modbus):
    module, clients = modbus
    assert [row.address for row in INPUT_REGISTERS] == list(range(20,36))+[52,53,54]
    target = Target(1, '192.0.2.1', read_address=20, read_count=16, profile='ingecon_100tl_input')
    target.validate()
    result = NetworkProbes().modbus(target)
    assert result.ok and len(result.registers) == 19 and result.registers[16] == 75
    assert clients[0].calls == [('connect',),('read_input',20,16,1),('read_input',52,3,1)]
    assert clients[0].closed
    current = {row.address: row for row in INPUT_REGISTERS}
    assert current[29].decode(65534) == -20
    assert current[27].decode(5000) == 50
    assert current[52].decode(65535) is None
