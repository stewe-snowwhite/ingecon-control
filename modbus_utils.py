"""Legacy register operations with writes denied by default.

Register maps are NOT validated for any model by these tests. A write reply and
readback confirm register contents only, not physical inverter operation.
"""
import logging
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from operation_locks import serialized_modbus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    confirmed: bool
    message: str


@serialized_modbus(1, lambda: False)
def check_modbus_connection(ip_address, port=502, timeout=2):
    client = None
    try:
        client = ModbusTcpClient(host=ip_address, port=port, timeout=timeout)
        return bool(client.connect())
    except Exception:
        log.warning('Modbus connection failed')
        return False
    finally:
        if client is not None:
            client.close()


def _write_and_verify(ip, address, value, port, allow_write, device_id=1):
    if allow_write is not True:
        return CommandResult(False, 'Modbus-записи заблоковано.')
    client = None
    try:
        # No automatic write retries: a lost reply leaves an unknown result.
        client = ModbusTcpClient(host=ip, port=port, retries=0)
        if not client.connect():
            return CommandResult(False, 'Пристрій недоступний; запис не підтверджено.')
        reply = client.write_register(address, value, device_id=device_id)
        if reply is None or reply.isError():
            return CommandResult(False, 'Пристрій не підтвердив запис.')
        if getattr(reply, 'address', None) != address or getattr(reply, 'registers', None) != [value]:
            return CommandResult(False, 'Відповідь запису не відповідає команді.')
        readback = client.read_holding_registers(address, count=1, device_id=device_id)
        if readback is None or readback.isError() or readback.registers != [value]:
            return CommandResult(False, 'Значення регістра після запису не підтверджено.')
        return CommandResult(True, 'Підтверджено запис регістра; фізичний стан інвертора не підтверджено.')
    except Exception:
        log.warning('Modbus command result unknown', exc_info=False)
        return CommandResult(False, 'Результат запису невідомий; автоматичного повторення немає.')
    finally:
        if client is not None:
            client.close()


@serialized_modbus(1, lambda: CommandResult(False, 'Modbus-операція вже виконується.'))
def modbus_control(ip, port=502, *, address, value, device_id, allow_write=False):
    """FC06 + FC03 readback for an explicitly verified, model-specific command."""
    if any(type(number) is not int or not low <= number <= high for number, low, high in (
        (address, 0, 65535), (value, 0, 65535), (device_id, 1, 255), (port, 1, 65535)
    )):
        return CommandResult(False, 'Некоректна карта команди; запис не виконувався.')
    return _write_and_verify(ip, address, value, port, allow_write, device_id)


@serialized_modbus(1, lambda: CommandResult(False, 'Modbus-операція вже виконується.'))
def modbus_turn_on(ip, port=502, *, allow_write=False):
    return _write_and_verify(ip, 1000, 6, port, allow_write)


@serialized_modbus(1, lambda: CommandResult(False, 'Modbus-операція вже виконується.'))
def modbus_turn_off(ip, port=502, *, allow_write=False):
    return _write_and_verify(ip, 1000, 5, port, allow_write)


@serialized_modbus(2, lambda: CommandResult(False, 'Modbus-операція вже виконується.'))
def modbus_limit_power(ip, value=50, port=502, *, allow_write=False):
    if type(value) is not int or not 1 <= value <= 100:
        return CommandResult(False, 'Відсоток має бути цілим числом від 1 до 100.')
    return _write_and_verify(ip, 46, value, port, allow_write)


@serialized_modbus(1, lambda: (None, None))
def read_command_status(ip, port=502):
    client = None
    try:
        client = ModbusTcpClient(host=ip, port=port)
        if client.connect():
            reply = client.read_holding_registers(1000, count=2, device_id=1)
            if reply is not None and not reply.isError() and len(reply.registers) == 2:
                return tuple(reply.registers)
    except Exception:
        log.warning('Modbus status unavailable')
    finally:
        if client is not None:
            client.close()
    return None, None
