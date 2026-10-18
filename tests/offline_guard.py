"""Test-only irreversible guard: no sockets, DNS or subprocesses at all."""
import sys


class OfflineOperationBlocked(RuntimeError):
    pass


class OfflineGuard:
    def __call__(self, event, args):
        if event in ('socket.__new__','socket.connect','socket.bind','socket.sendto',
                     'socket.getaddrinfo','socket.gethostbyname','socket.gethostbyaddr',
                     'socket.gethostname','socket.getnameinfo','subprocess.Popen',
                     'os.system','os.exec','os.posix_spawn','os.fork','pty.spawn'):
            raise OfflineOperationBlocked('Offline test: network and subprocesses are forbidden')


def install_offline_guard():
    sys.addaudithook(OfflineGuard())
