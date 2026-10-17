"""Shared web/Telegram control boundary; only the web's explicit policy can write.

Telegram has no enable switch in TASK-06. All real writes remain unauthorized.
"""
from modbus_utils import modbus_limit_power, modbus_turn_off, modbus_turn_on


class ControlBlocked(ValueError):
    pass


def block_reason(*, writes_enabled=False, demo=False, source='web'):
    if source == 'telegram':
        return 'Керування через Telegram заблоковано до окремого дозволу. Modbus-запис не виконувався.'
    if demo:
        return 'У деморежимі всі Modbus-записи заборонено.'
    if writes_enabled is not True:
        return 'Modbus-записи заблоковано.'
    return None


def command_payload(user, action, value, inverters):
    return {'user': user.id, 'version': user.session_version, 'role': user.role,
            'action': action, 'value': value,
            'targets': [[inv.id, inv.name, inv.group, inv.ip_address] for inv in inverters]}


def execute_devices(user, action, inverters, value=100, *, writes_enabled=False, demo=False, source='web'):
    if not user.active or user.role not in ('Administrator', 'Operator'):
        raise ControlBlocked('Недостатньо прав для керування.')
    if action not in ('on','off','limit','limit_max') or type(value) is not int or not 1 <= value <= 100:
        raise ValueError('Некоректна команда або відсоток потужності.')
    if not 1 <= len(inverters) <= 100 or len({inv.id for inv in inverters}) != len(inverters):
        raise ValueError('Некоректний список інверторів.')
    reason = block_reason(writes_enabled=writes_enabled, demo=demo, source=source)
    if reason:
        raise ControlBlocked(reason)
    results = []
    for inv in inverters:
        if action == 'on':
            result = modbus_turn_on(inv.ip_address, allow_write=True)
        elif action == 'off':
            result = modbus_turn_off(inv.ip_address, allow_write=True)
        else:
            result = modbus_limit_power(inv.ip_address, value=value, allow_write=True)
        results.append(dict(id=inv.id, name=inv.name,
            outcome='register_confirmed' if result.confirmed else 'unconfirmed', message=result.message))
    return results
