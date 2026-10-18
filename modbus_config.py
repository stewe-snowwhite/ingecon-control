"""One editable holding-register map for all devices; never enables writes."""
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from operation_locks import resource_lock


class ConfigurationConflict(ValueError):
    pass


@dataclass(frozen=True)
class ModbusConfiguration:
    revision: int = 0
    port: int = 502
    device_id: int = 1
    timeout: int = 2
    start_address: int = 1000
    start_value: int = 6
    stop_address: int = 1000
    stop_value: int = 5
    limit_address: int = 46
    limit_read_address: int = 1001
    limit_reason_address: int = 47

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def command(self, action, value=100):
        if action == 'on': return self.start_address, self.start_value
        if action == 'off': return self.stop_address, self.stop_value
        if action in ('limit', 'limit_max') and type(value) is int and 1 <= value <= 100:
            return self.limit_address, 100 if action == 'limit_max' else value
        raise ValueError('Невідома команда або відсоток поза межами 1–100.')


def validate_configuration(data):
    fields = set(asdict(ModbusConfiguration()))
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError('Некоректні поля глобальної конфігурації Modbus.')
    for name, value in data.items():
        low, high = (0, 65535)
        if name == 'revision': low, high = 0, 2**53-1
        elif name == 'port': low = 1
        elif name == 'device_id': low, high = 1, 247
        elif name == 'timeout': low, high = 1, 10
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Некоректний параметр Modbus: '+name)
    return ModbusConfiguration(**data)


def configuration_path():
    return Path(os.environ.get('INGECON_MODBUS_CONFIG_FILE',
        str(Path(__file__).parent/'instance'/'production'/'modbus.json')))


def load_configuration():
    path = configuration_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ModbusConfiguration()
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
            raise ValueError('Файл Modbus має бути приватним звичайним файлом користувача служби, до 8 KiB.')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('Повторне поле конфігурації Modbus.')
                result[key] = value
            return result
        return validate_configuration(json.loads(stream.read(8193), object_pairs_hook=unique))


def save_configuration(configuration, expected_fingerprint):
    configuration = validate_configuration(asdict(configuration))
    path = configuration_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with resource_lock('global-modbus-config:'+str(path.absolute())):
        current = load_configuration()
        if current.fingerprint != expected_fingerprint:
            raise ConfigurationConflict('Конфігурацію вже змінено. Оновіть сторінку.')
        configuration = validate_configuration(asdict(replace(configuration, revision=current.revision+1)))
        fd, temporary = tempfile.mkstemp(prefix='.modbus-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(asdict(configuration), stream, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    return configuration
