"""Process-wide outbound guard. Install only in the standalone demo process."""
import socket
import sys


class DemoSafetyError(RuntimeError):
    pass


class DemoNetworkGuard:
    def __init__(self, host='127.0.0.1', port=5000):
        if host not in ('127.0.0.1', '0.0.0.0') or port != 5000:
            raise ValueError('Demo listener must be 127.0.0.1:5000 or 0.0.0.0:5000')
        self.host, self.port = host, port

    def __call__(self, event, args):
        if event in ('socket.connect', 'socket.sendto', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.gethostname', 'socket.getnameinfo'):
            raise DemoSafetyError('Demo: outbound connections and DNS are disabled')
        if event == 'socket.__new__':
            _, family, kind, protocol = args
            if family != socket.AF_INET or kind != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP):
                raise DemoSafetyError('Demo: only inbound HTTP TCP sockets are allowed')
        if event == 'socket.bind':
            if args[1] != (self.host, self.port):
                raise DemoSafetyError('Demo: only the explicit HTTP listener may bind')
        if event == 'socket.getaddrinfo':
            host, port, family, kind, protocol = args
            if (host != self.host or port != self.port or family != socket.AF_INET or
                kind != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP)):
                raise DemoSafetyError('Demo: DNS resolution is disabled')
        if event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork', 'pty.spawn'):
            raise DemoSafetyError('Demo: launching subprocesses is disabled')


def disable_device_transports():
    """Disable constructors/probes too; the audit guard is a second boundary."""
    import modbus_utils
    import monitoring
    import pymodbus.client

    def blocked(*args, **kwargs):
        raise DemoSafetyError('Demo: all device probes and Modbus operations are disabled')

    modbus_utils.ModbusTcpClient = blocked
    pymodbus.client.ModbusTcpClient = blocked
    for name in ('icmp', 'tcp', 'modbus', 'poll'):
        setattr(monitoring.NetworkProbes, name, blocked)


def install_demo_guard(host='127.0.0.1'):
    # Python audit hooks cannot be unregistered: there is no runtime disable switch.
    sys.addaudithook(DemoNetworkGuard(host))
    disable_device_transports()
