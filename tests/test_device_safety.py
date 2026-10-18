"""Production transport denial and test-only offline guard, without device I/O."""
import pytest

import modbus_utils
import monitoring
import pymodbus.client
from device_safety import DeviceTransportBlocked, disable_device_transports
from tests.offline_guard import OfflineGuard, OfflineOperationBlocked


@pytest.fixture
def blocked(monkeypatch):
    # Register all changed attributes before the production helper overwrites
    # them, so pytest restores its mock clients and probe methods afterwards.
    for module,name in [(modbus_utils,'ModbusTcpClient'),(pymodbus.client,'ModbusTcpClient')]:
        monkeypatch.setattr(module,name,getattr(module,name))
    for name in ('icmp','tcp','modbus','poll'):
        monkeypatch.setattr(monitoring.NetworkProbes,name,getattr(monitoring.NetworkProbes,name))
    disable_device_transports()


@pytest.mark.parametrize('method',['icmp','tcp','modbus','poll'])
def test_web_and_bot_cannot_probe_devices(blocked,method):
    with pytest.raises(DeviceTransportBlocked):
        getattr(monitoring.NetworkProbes(),method)(None)


def test_both_device_constructors_are_blocked(blocked):
    for factory in (modbus_utils.ModbusTcpClient,pymodbus.client.ModbusTcpClient):
        with pytest.raises(DeviceTransportBlocked): factory('192.0.2.1')


@pytest.mark.parametrize('operation',['modbus_turn_on','modbus_turn_off','modbus_limit_power'])
def test_explicit_flag_cannot_bypass_transport_denial(blocked,operation):
    assert not getattr(modbus_utils,operation)('192.0.2.1',allow_write=True).confirmed


@pytest.mark.parametrize('event',['socket.__new__','socket.connect','socket.bind','socket.sendto',
    'socket.getaddrinfo','socket.gethostbyname','socket.gethostbyaddr','socket.gethostname',
    'socket.getnameinfo','subprocess.Popen','os.system','os.exec','os.posix_spawn','os.fork','pty.spawn'])
def test_offline_test_guard_blocks_before_syscall(event):
    with pytest.raises(OfflineOperationBlocked): OfflineGuard()(event,())
