"""Explicit cookie policy and a private, persistent Flask signing key."""
import os
import stat
from pathlib import Path


def cookie_secure_from_env():
    raw = os.environ.get('INGECON_COOKIE_SECURE', 'true').strip().lower()
    if raw not in ('true', 'false'):
        raise RuntimeError('INGECON_COOKIE_SECURE must be explicitly true or false.')
    return raw == 'true'


def read_secret_file(path):
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_size > 4096):
                raise ValueError
            secret = stream.read(4097).strip()
            if len(secret) < 32:
                raise ValueError
            return secret
    except (OSError, ValueError):
        raise RuntimeError('Flask secret file must be private, owned by the service user, and contain at least 32 characters.') from None


def secret_from_env():
    value = os.environ.get('INGECON_SECRET_KEY')
    path = os.environ.get('INGECON_SECRET_KEY_FILE')
    if value and path:
        raise RuntimeError('Configure either INGECON_SECRET_KEY or INGECON_SECRET_KEY_FILE.')
    return read_secret_file(path) if path else value
