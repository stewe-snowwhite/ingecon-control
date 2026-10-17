"""Standalone proof: denied calls fail before a syscall/network transmission.

Run from the project root: .venv/bin/python -m tests.demo_guard_probe
Never imported by pytest: Python audit hooks are irreversible.
"""
import os
import socket
import subprocess
import tempfile

from demo_safety import DemoSafetyError, install_demo_guard


def denied(operation):
    try:
        operation()
    except DemoSafetyError:
        return
    raise AssertionError('Demo operation was not blocked')


def main():
    with tempfile.TemporaryDirectory(prefix='ingecon-demo-guard-') as directory:
        os.environ['INGECON_LOCK_DIR'] = directory
        install_demo_guard()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            denied(lambda: connection.connect(('192.0.2.1',502)))
            denied(lambda: connection.connect_ex(('127.0.0.1',5000)))
            denied(lambda: connection.bind(('0.0.0.0',5000)))
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
    print('Demo guard: socket, DNS, subprocess, ICMP/TCP/Modbus and write bypass attempts blocked.')


if __name__ == '__main__':
    main()
