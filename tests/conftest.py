import importlib
import logging
import socket
import sys

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
    monkeypatch.setattr(logging, 'basicConfig', lambda **kwargs: None)
    monkeypatch.setenv('INGECON_DATABASE_URI', 'sqlite:///:memory:')
    monkeypatch.setenv('INGECON_SECRET_KEY', 'test-only-random-looking-key-32-chars')
    monkeypatch.chdir(tmp_path)
    import pymodbus.client
    monkeypatch.setattr(pymodbus.client, 'ModbusTcpClient', FakeModbusTcpClient)
    import modbus_utils
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', FakeModbusTcpClient)

@pytest.fixture
def modbus(isolation, monkeypatch):
    import modbus_utils
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
