"""Standalone proof: denied calls fail before a syscall/network transmission.

Run from the project root: .venv/bin/python -m tests.device_guard_probe
Never imported by pytest: Python audit hooks are irreversible.
"""
import os
import socket
import subprocess
import tempfile

from device_safety import DeviceTransportBlocked, disable_device_transports
from tests.offline_guard import OfflineOperationBlocked, install_offline_guard


def denied(operation):
    try:
        operation()
    except (OfflineOperationBlocked,DeviceTransportBlocked):
        return
    raise AssertionError('Unsafe test operation was not blocked')


def main():
    with tempfile.TemporaryDirectory(prefix='ingecon-device-guard-') as directory:
        os.environ['INGECON_LOCK_DIR'] = directory
        install_offline_guard()
        disable_device_transports()
        denied(lambda: socket.socket(socket.AF_INET,socket.SOCK_STREAM))
        denied(lambda: socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
        denied(lambda: socket.socket(socket.AF_INET,socket.SOCK_RAW))
        denied(lambda: socket.getaddrinfo('example.invalid',502))
        denied(lambda: socket.gethostbyaddr('192.0.2.1'))
        denied(lambda: socket.getnameinfo(('192.0.2.1',502),0))
        denied(lambda: subprocess.run(['ping','-c','1','192.0.2.1']))
        denied(lambda: os.system('ping -c 1 192.0.2.1'))
        import modbus_utils
        from monitoring import NetworkProbes
        denied(lambda: modbus_utils.ModbusTcpClient('192.0.2.1'))
        for name in ('icmp','tcp','modbus','poll'):
            denied(lambda: getattr(NetworkProbes(),name)(None))
        for operation in (modbus_utils.modbus_turn_on,modbus_utils.modbus_turn_off,
                          modbus_utils.modbus_limit_power):
            assert not operation('192.0.2.1',allow_write=True).confirmed
    print('Offline guard: socket, DNS, subprocess, ICMP/TCP/Modbus and write bypass attempts blocked.')


if __name__ == '__main__':
    main()
