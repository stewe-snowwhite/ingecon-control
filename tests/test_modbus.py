import socket
import inspect
import pytest
from pymodbus.client import ModbusTcpClient
from tests.fake_modbus import FakeModbusTcpClient

@pytest.mark.parametrize('action,value', [('modbus_turn_on',6), ('modbus_turn_off',5)])
def test_commands_only_write_to_memory(modbus, action, value):
    module, clients = modbus
    getattr(module, action)('192.0.2.1')
    assert ('write',1000,value,1) in clients[0].calls
    assert clients[0].closed

@pytest.mark.parametrize('value,expected', [(-5,0),(0,0),(37,37),(100,100),(105,100)])
def test_limit_clamping_and_readback(modbus, value, expected):
    module, clients = modbus
    module.modbus_limit_power('192.0.2.1',value)
    assert clients[0].calls == [('connect',),('write',46,expected,1),
                                ('read',46,1,1),('read',47,1,1)]
    assert clients[0].closed

def test_status_and_connect(modbus):
    module, clients = modbus
    assert module.check_modbus_connection('192.0.2.1',timeout=1)
    assert clients[0].closed and clients[0].timeout == 1
    assert module.read_command_status('192.0.2.1') == (16,32767)
    assert clients[1].closed

def test_offline_does_not_write(modbus, monkeypatch):
    module, _ = modbus
    client = FakeModbusTcpClient('192.0.2.1'); client.online = False
    monkeypatch.setattr(module,'ModbusTcpClient',lambda *a,**k: client)
    assert module.check_modbus_connection(client.host) is False
    assert module.read_command_status(client.host) == (None,None)
    module.modbus_turn_on(client.host)
    assert not any(call[0]=='write' for call in client.calls)

def test_write_error_does_not_read_back(modbus, monkeypatch):
    module, _ = modbus
    client = FakeModbusTcpClient('192.0.2.1'); client.write_error=True
    monkeypatch.setattr(module,'ModbusTcpClient',lambda *a,**k: client)
    module.modbus_limit_power(client.host,20)
    assert client.calls == [('connect',),('write',46,20,1)]
    assert client.closed

def test_network_guard():
    with pytest.raises(AssertionError,match='Network'):
        socket.create_connection(('192.0.2.1',502))
    with pytest.raises(AssertionError,match='Network'):
        socket.socket()
    with pytest.raises(AssertionError,match='Network'):
        socket.socket.connect(None, ('192.0.2.1',502))

def test_real_library_signature_matches_fake():
    # Signature inspection only; no client construction or connection.
    for name in ('write_register','read_holding_registers'):
        real=inspect.signature(getattr(ModbusTcpClient,name)).parameters
        fake=inspect.signature(getattr(FakeModbusTcpClient,name)).parameters
        for key in ('address','device_id'):
            assert key in real and key in fake
    assert inspect.signature(ModbusTcpClient.read_holding_registers).parameters['count'].kind == inspect.Parameter.KEYWORD_ONLY

@pytest.mark.parametrize('registers', [[], [16]])
def test_short_status_response(modbus, monkeypatch, registers):
    from tests.fake_modbus import Response
    module, _ = modbus
    client = FakeModbusTcpClient('192.0.2.1')
    monkeypatch.setattr(client, 'read_holding_registers', lambda *a, **k: Response(registers))
    monkeypatch.setattr(module, 'ModbusTcpClient', lambda *a, **k: client)
    assert module.read_command_status(client.host) == (None, None)
    assert client.closed

def test_connection_exception_is_reported_offline(modbus, monkeypatch):
    module, _ = modbus
    def fail(*args, **kwargs):
        raise RuntimeError('simulated transport error')
    monkeypatch.setattr(module, 'ModbusTcpClient', fail)
    assert module.check_modbus_connection('192.0.2.1') is False
    assert module.read_command_status('192.0.2.1') == (None, None)
