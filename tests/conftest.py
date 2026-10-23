import importlib
import logging
import socket
import sys
import subprocess

import pytest
from flask import g
from tests.fake_modbus import FakeModbusTcpClient

@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    # Active before importing application modules and throughout every test.
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access is forbidden in tests")
    monkeypatch.setattr(socket.socket, '__init__', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setenv('INGECON_LOCK_DIR', str(tmp_path / 'locks'))
    monkeypatch.setattr(logging, 'basicConfig', lambda **kwargs: None)
    monkeypatch.setenv('INGECON_DATABASE_URI', 'sqlite:///:memory:')
    monkeypatch.setenv('INGECON_SECRET_KEY', 'test-only-random-looking-key-32-chars')
    monkeypatch.delenv('INGECON_SECRET_KEY_FILE', raising=False)
    monkeypatch.delenv('INGECON_COOKIE_SECURE', raising=False)
    monkeypatch.delenv('INGECON_TELEGRAM_CONTROL_CONFIG', raising=False)
    monkeypatch.setenv('INGECON_MODBUS_CONFIG_FILE', str(tmp_path/'modbus.json'))
    monkeypatch.setenv('INGECON_INVERTER_CONTROLS_FILE', str(tmp_path/'inverter-controls.json'))
    monkeypatch.setenv('INGECON_MONITOR_SCHEDULE_FILE', str(tmp_path/'monitor-schedule.json'))
    monkeypatch.setenv('INGECON_POWER_BUDGET_FILE', str(tmp_path/'power-budget.json'))
    monkeypatch.delenv('INGECON_REGISTER_READS_ENABLED', raising=False)
    monkeypatch.chdir(tmp_path)
    import discovery
    monkeypatch.setattr(discovery, 'local_inventory', lambda: [])
    import pymodbus.client
    monkeypatch.setattr(pymodbus.client, 'ModbusTcpClient', FakeModbusTcpClient)
    import modbus_utils
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', FakeModbusTcpClient)

@pytest.fixture
def modbus(isolation, monkeypatch):
    import modbus_utils
    import modbus_safety
    # Only this fixture permits writes, and only against the in-memory mock.
    monkeypatch.setattr(modbus_safety, 'PHYSICAL_WRITES_LOCKED', False)
    clients = []
    def factory(host, **kwargs):
        client = FakeModbusTcpClient(host, **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', factory)
    return modbus_utils, clients

@pytest.fixture
def web(isolation):
    # Fresh SQLAlchemy instance, never an engine pointed at the working database.
    sys.modules.pop('app', None)
    module = importlib.import_module('app')
    module.app.config.update(TESTING=True)
    # The fixture holds an outer DB context; emulate Flask's fresh request g.
    def reset_request_cache():
        g.pop('csrf_token', None)
        g.pop('csrf_valid', None)
    module.app.before_request_funcs[None].insert(0, reset_request_cache)
    with module.app.app_context():
        assert module.db.engine.url.database == ':memory:'
        module.db.create_all()
        user = module.User(username='admin', role='Administrator')
        user.set_password('test-password-long')
        module.db.session.add(user)
        module.db.session.commit()
        client = module.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = user.id
            sess['session_version'] = user.session_version
        yield module, client
        module.db.session.remove()
        module.db.drop_all()
    sys.modules.pop('app', None)
