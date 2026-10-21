"""Explicit per-device FC06 command maps; no network or implicit Ingeteam map."""
import hashlib
import ipaddress
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
import inverter_controls


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Некоректне ціле значення в конфігурації керування Telegram.')
    return value


@dataclass(frozen=True)
class ControlTarget:
    inverter_id: int
    ip: str
    model: str
    port: int
    device_id: int
    start_address: int
    start_value: int
    stop_address: int
    stop_value: int


@dataclass(frozen=True)
class TelegramControlPolicy:
    targets: tuple = ()
    fingerprint: str = 'disabled'

    @property
    def enabled(self):
        return bool(self.targets) or inverter_controls.enabled()

    def target(self, inverter):
        for target in self.targets:
            if target.inverter_id == inverter.id and target.ip == inverter.ip_address:
                return target
        raise ValueError('Інвертор або його IP не дозволено конфігурацією керування Telegram.')

    def validate(self, inverters, action):
        if inverter_controls.enabled():
            if action not in ('on','off','limit','limit_max') or any(inv.model != 'ingecon_sun_100tl' for inv in inverters):
                raise ValueError('Керування дозволене лише для INGECON SUN 100TL.')
            return [None for inv in inverters]
        if not self.enabled or action not in ('on', 'off'):
            raise ValueError('Telegram дозволяє лише Start/Stop явно налаштованих інверторів.')
        # Validate the entire group before the first write.
        return [self.target(inverter) for inverter in inverters]


def policy_from_dict(data):
    if not isinstance(data, dict) or set(data) != {'enabled', 'inverters'} or type(data['enabled']) is not bool:
        raise ValueError('Потрібні enabled (boolean) та inverters у конфігурації Telegram.')
    rows = data['inverters']
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('Некоректний список дозволених інверторів Telegram.')
    targets = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            'id', 'ip', 'model', 'port', 'device_id', 'register_map_confirmed', 'start', 'stop'
        } or row['register_map_confirmed'] is not True:
            raise ValueError('Для кожного інвертора потрібна явно підтверджена карта команд.')
        if not isinstance(row['ip'], str) or not isinstance(row['model'], str) or not 1 <= len(row['model'].strip()) <= 100:
            raise ValueError('Потрібні IP та модель інвертора.')
        address = ipaddress.ip_address(row['ip'])
        if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
            raise ValueError('Недозволена IP-адреса в конфігурації керування.')
        values = []
        for name in ('start', 'stop'):
            command = row[name]
            if not isinstance(command, dict) or set(command) != {'address', 'value'}:
                raise ValueError('Команда повинна містити address і value для FC06.')
            values.extend((integer(command['address'], 0, 65535), integer(command['value'], 0, 65535)))
        target = ControlTarget(integer(row['id'], 1, 2**63-1), str(address), row['model'].strip(),
            integer(row['port'], 1, 65535), integer(row['device_id'], 1, 255), *values)
        if any(old.inverter_id == target.inverter_id or
               (old.ip, old.port, old.device_id) == (target.ip, target.port, target.device_id) for old in targets):
            raise ValueError('Дубль ID або Modbus-пристрою в конфігурації Telegram.')
        targets.append(target)
    if data['enabled'] and not targets:
        raise ValueError('Не можна ввімкнути керування без дозволених пристроїв.')
    if not data['enabled']:
        return TelegramControlPolicy()
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return TelegramControlPolicy(tuple(targets), fingerprint)


def load_control_policy(path=None):
    if path is None:
        path = os.environ.get('INGECON_TELEGRAM_CONTROL_CONFIG')
    if not path:
        return TelegramControlPolicy()
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_size > 32768):
                raise ValueError
            def unique_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            return policy_from_dict(json.loads(stream.read(32769), object_pairs_hook=unique_keys))
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Некоректний файл керування Telegram: потрібен перевірений JSON до 32 KiB, власник — користувач служби, права 0600.') from None
