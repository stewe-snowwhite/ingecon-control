import inspect
import socket

import pytest
from pymodbus.client import ModbusTcpClient
from tests.fake_modbus import FakeModbusTcpClient, Response

@pytest.mark.parametrize('action,value', [('modbus_turn_on',6), ('modbus_turn_off',5)])
def test_commands_only_write_to_memory(modbus, action, value):
    module, clients = modbus
    result = getattr(module, action)('192.0.2.1', allow_write=True)
    assert result.confirmed
    assert clients[0].calls == [('connect',),('write',1000,value,1),('read',1000,1,1)]
    assert clients[0].closed and clients[0].retries == 0

@pytest.mark.parametrize('value', [1,37,100])
def test_limit_and_readback(modbus,value):
    module, clients=modbus
    assert module.modbus_limit_power('192.0.2.1',value,allow_write=True).confirmed
    assert clients[0].calls == [('connect',),('write',46,value,1),('read',46,1,1)]
    assert clients[0].closed

@pytest.mark.parametrize('value', [-5,0,101,'20',True,2.5,None])
def test_invalid_limit_never_constructs_client(modbus,value):
    module,clients=modbus
    assert not module.modbus_limit_power('192.0.2.1',value,allow_write=True).confirmed
    assert clients==[]

@pytest.mark.parametrize('action', ['modbus_turn_on','modbus_turn_off','modbus_limit_power'])
def test_writes_denied_by_default(modbus,action):
    module,clients=modbus
    assert not getattr(module,action)('192.0.2.1').confirmed
    assert clients==[]

def test_status_and_connect(modbus):
    module,clients=modbus
    assert module.check_modbus_connection('192.0.2.1',timeout=1)
    assert clients[0].closed and clients[0].timeout==1
    assert module.read_command_status('192.0.2.1')==(16,32767)
    assert clients[1].closed

@pytest.mark.parametrize('failure', ['offline','write_error','read_error','mismatch','exception','bad_echo'])
def test_failed_command_never_confirmed(modbus,monkeypatch,failure):
    module,_=modbus
    client=FakeModbusTcpClient('192.0.2.1')
    if failure=='offline': client.online=False
    if failure=='write_error': client.write_error=True
    if failure=='read_error': client.read_error=True
    if failure=='mismatch':
        monkeypatch.setattr(client,'read_holding_registers',lambda *a,**k: Response([99]))
    if failure=='bad_echo':
        monkeypatch.setattr(client,'write_register',lambda *a,**k: Response([6],address=99))
    if failure=='exception':
        def fail(*a,**k): raise RuntimeError('simulated')
        monkeypatch.setattr(client,'write_register',fail)
    monkeypatch.setattr(module,'ModbusTcpClient',lambda *a,**k: client)
    result=module.modbus_turn_on(client.host,allow_write=True)
    assert not result.confirmed
    assert client.closed
    assert len([c for c in client.calls if c[0]=='write']) <= 1

@pytest.mark.parametrize('registers,error', [([],False),([16],False),([16,32767],True)])
def test_bad_status_response(modbus,monkeypatch,registers,error):
    module,_=modbus
    client=FakeModbusTcpClient('192.0.2.1')
    monkeypatch.setattr(client,'read_holding_registers',lambda *a,**k: Response(registers,error))
    monkeypatch.setattr(module,'ModbusTcpClient',lambda *a,**k: client)
    assert module.read_command_status(client.host)==(None,None)
    assert client.closed

def test_connection_exception(modbus,monkeypatch):
    module,_=modbus
    def fail(*a,**k): raise RuntimeError('simulated')
    monkeypatch.setattr(module,'ModbusTcpClient',fail)
    assert not module.check_modbus_connection('192.0.2.1')
    assert module.read_command_status('192.0.2.1')==(None,None)

def test_network_guard():
    for call in (lambda: socket.create_connection(('192.0.2.1',502)),
                 lambda: socket.socket(),
                 lambda: socket.socket.connect(None,('192.0.2.1',502))):
        with pytest.raises(AssertionError,match='Network'): call()

def test_real_library_signature_matches_fake():
    for name in ('write_register','read_holding_registers'):
        real=inspect.signature(getattr(ModbusTcpClient,name)).parameters
        fake=inspect.signature(getattr(FakeModbusTcpClient,name)).parameters
        assert all(key in real and key in fake for key in ('address','device_id'))
    assert inspect.signature(ModbusTcpClient.read_holding_registers).parameters['count'].kind==inspect.Parameter.KEYWORD_ONLY
