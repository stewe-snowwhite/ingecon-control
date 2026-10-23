"""Shared, expiring read-only polling policy. No database DDL or inverter I/O."""
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from operation_locks import resource_lock

DEFAULT_INTERVAL = 30
OVERRIDE_DURATION = 30 * 60
MAX_INTERVAL = 86400


def policy_path():
    return Path(os.environ.get('INGECON_MONITOR_SCHEDULE_FILE',
        str(Path(__file__).parent/'instance/production/monitor-schedule.json')))


def interval_value(value):
    if type(value) is not int or not 1 <= value <= MAX_INTERVAL:
        raise ValueError('Інтервал має бути цілим числом від 1 до 86400 секунд.')
    return value


def _load():
    try:
        fd = os.open(policy_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('Налаштування опитування має бути приватним файлом користувача служби.')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('Повторне поле налаштування.')
                result[key] = value
            return result
        data = json.loads(stream.read(4097), object_pairs_hook=unique)
    if not isinstance(data, dict) or set(data) != {'version','interval_seconds','created_at','expires_at','revision'}:
        raise ValueError('Некоректне налаштування опитування.')
    if type(data['version']) is not int or data['version'] != 1:
        raise ValueError('Невідома версія налаштування.')
    interval_value(data['interval_seconds'])
    created, expires = data['created_at'], data['expires_at']
    if type(created) not in (int, float) or not math.isfinite(created): raise ValueError('Некоректний час.')
    if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or
                               not math.isclose(expires-created, OVERRIDE_DURATION, rel_tol=0, abs_tol=1e-6)):
        raise ValueError('Тимчасовий інтервал має діяти рівно 30 хвилин.')
    if expires is None and data['interval_seconds'] != DEFAULT_INTERVAL:
        raise ValueError('Постійний інтервал має бути 30 секунд.')
    if not isinstance(data['revision'], str) or len(data['revision']) != 32:
        raise ValueError('Некоректна версія налаштування.')
    return data


def current(*, now=None, default=DEFAULT_INTERVAL):
    now = time.time() if now is None else now
    error, record = None, None
    try:
        record = _load()
    except (OSError, ValueError, UnicodeError):
        error = 'monitor_schedule_invalid'
    active = bool(record and record['expires_at'] is not None and record['created_at'] <= now < record['expires_at'])
    fallback = DEFAULT_INTERVAL if record or error else default
    return dict(interval_seconds=record['interval_seconds'] if active else fallback,
                default_interval_seconds=DEFAULT_INTERVAL, expires_at=record['expires_at'] if active else None,
                temporary=active, revision=record['revision'] if record else None, error=error)


def configure(interval=None, *, now=None):
    if interval is not None: interval_value(interval)
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now): raise ValueError('Некоректний час.')
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with resource_lock('monitor-polling-schedule'):
        _load()  # Do not overwrite an insecure, corrupt or foreign file.
        record = dict(version=1, interval_seconds=DEFAULT_INTERVAL if interval is None else interval,
                      created_at=now, expires_at=now+OVERRIDE_DURATION if interval is not None else None,
                      revision=uuid.uuid4().hex)
        fd, temporary = tempfile.mkstemp(prefix='.monitor-schedule-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream, allow_nan=False)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    return current(now=now)
