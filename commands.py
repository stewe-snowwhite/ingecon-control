"""Shared role/validation boundary; writes require an explicit source policy."""
from modbus_utils import modbus_control
from modbus_config import load_configuration
import modbus_safety


class ControlBlocked(ValueError):
    pass


def block_reason(*, writes_enabled=False, source='web', telegram_policy=None):
    if source not in ('web', 'telegram'):
        return 'Невідоме джерело команди.'
    if source == 'telegram' and not (telegram_policy and telegram_policy.enabled):
        return 'Керування через Telegram заблоковано до окремого дозволу. Modbus-запис не виконувався.'
    if writes_enabled is not True:
        return 'Modbus-записи заблоковано.'
    if modbus_safety.writes_locked():
        return modbus_safety.WRITE_LOCK_REASON
    return None


def command_payload(user, action, value, inverters):
    return {'user': user.id, 'version': user.session_version, 'role': user.role,
            'action': action, 'value': value,
            'targets': [[inv.id, inv.name, inv.group, inv.ip_address] for inv in inverters]}


def execute_devices(user, action, inverters, value=100, *, writes_enabled=False, source='web', telegram_policy=None):
    if not user.active or user.role not in ('Administrator', 'Operator'):
        raise ControlBlocked('Недостатньо прав для керування.')
    if action not in ('on','off','limit','limit_max') or type(value) is not int or not 1 <= value <= 100:
        raise ValueError('Некоректна команда або відсоток потужності.')
    if not 1 <= len(inverters) <= 100 or len({inv.id for inv in inverters}) != len(inverters):
        raise ValueError('Некоректний список інверторів.')
    reason = block_reason(writes_enabled=writes_enabled, source=source, telegram_policy=telegram_policy)
    if reason:
        raise ControlBlocked(reason)
    profiles = None
    if source == 'telegram':
        try:
            profiles = telegram_policy.validate(inverters, action)
        except ValueError as error:
            raise ControlBlocked(str(error)) from error
    results = []
    config = load_configuration()
    address, register_value = config.command(action, value)
    for inv in inverters:
        result = modbus_control(inv.ip_address, port=config.port, device_id=config.device_id,
            address=address, value=register_value, allow_write=True, timeout=config.timeout)
        row = dict(id=inv.id, name=inv.name,
            outcome='register_confirmed' if result.confirmed else 'unconfirmed', message=result.message)
        if profiles is not None:
            row['command'] = dict(model='INGECON SUN 100TL (historical, unconfirmed)',
                port=config.port, device_id=config.device_id, address=address, value=register_value)
        results.append(row)
    return results
