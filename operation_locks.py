"""Linux advisory locks shared by all application Modbus callers.

Keep lock files: deleting an inode while another process holds it breaks locking.
All processes must use the same INGECON_LOCK_DIR and service user.
"""
import fcntl
import hashlib
import ipaddress
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path


class OperationBusy(RuntimeError):
    pass


@contextmanager
def resource_lock(key):
    directory = Path(os.environ.get('INGECON_LOCK_DIR', Path(__file__).parent / 'instance' / 'locks'))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(key.encode()).hexdigest() + '.lock'
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OperationBusy('Operation already running') from error
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def endpoint_key(host, port):
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        host = host.strip().lower()
    # Lock a gateway across all unit IDs as well as duplicate inverter rows.
    return f'modbus:{host}:{port}'


def serialized_modbus(port_position, busy_result):
    def decorate(function):
        @wraps(function)
        def wrapped(host, *args, **kwargs):
            port = kwargs.get('port', args[port_position-1] if len(args) >= port_position else 502)
            try:
                with resource_lock(endpoint_key(host, port)):
                    return function(host, *args, **kwargs)
            except OperationBusy:
                return busy_result()
        return wrapped
    return decorate
