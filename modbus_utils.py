"""Legacy register operations with writes denied by default.

Register maps are NOT validated for any model by these tests. A write reply and
readback confirm register contents only, not physical inverter operation.
"""
import logging
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from operation_locks import serialized_modbus, resource_lock, endpoint_key
import modbus_safety
from modbus_config import load_configuration

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


def _write_and_verify(ip, address, value, port, allow_write, device_id=1, timeout=3):
    if allow_write is not True:
        return CommandResult(False, 'Modbus-записи заблоковано.')
    if modbus_safety.writes_locked():
        return CommandResult(False, modbus_safety.WRITE_LOCK_REASON)
    client = None
    try:
        # No automatic write retries: a lost reply leaves an unknown result.
        client = ModbusTcpClient(host=ip, port=port, timeout=timeout, retries=0)
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
def modbus_control(ip, port=502, *, address, value, device_id, allow_write=False, timeout=3):
    """FC06 + FC03 readback for an explicitly verified, model-specific command."""
    if any(type(number) is not int or not low <= number <= high for number, low, high in (
        (address, 0, 65535), (value, 0, 65535), (device_id, 1, 247), (port, 1, 65535), (timeout, 1, 10)
    )):
        return CommandResult(False, 'Некоректна карта команди; запис не виконувався.')
    return _write_and_verify(ip, address, value, port, allow_write, device_id, timeout)


def modbus_turn_on(ip, port=None, *, allow_write=False):
    config = load_configuration()
    return modbus_control(ip, port=config.port if port is None else port, device_id=config.device_id,
                          address=config.start_address, value=config.start_value, allow_write=allow_write, timeout=config.timeout)


def modbus_turn_off(ip, port=None, *, allow_write=False):
    config = load_configuration()
    return modbus_control(ip, port=config.port if port is None else port, device_id=config.device_id,
                          address=config.stop_address, value=config.stop_value, allow_write=allow_write, timeout=config.timeout)


def modbus_limit_power(ip, value=50, port=None, *, allow_write=False):
    if type(value) is not int or not 1 <= value <= 100:
        return CommandResult(False, 'Відсоток має бути цілим числом від 1 до 100.')
    config = load_configuration()
    return modbus_control(ip, port=config.port if port is None else port, device_id=config.device_id,
                          address=config.limit_address, value=value, allow_write=allow_write, timeout=config.timeout)


def read_command_status(ip, port=None):
    config = load_configuration()
    return _read_legacy_words(ip, (config.start_address, config.limit_read_address), config,
                              config.port if port is None else port)


def read_legacy_limits(ip):
    """Raw historical HR limit/reason; no inferred scaling or physical state."""
    config = load_configuration()
    return _read_legacy_words(ip, (config.limit_read_address, config.limit_reason_address), config, config.port)


def _read_legacy_words(ip, addresses, config, port):
    client = None
    try:
        with resource_lock(endpoint_key(ip, port)):
            try:
                client = ModbusTcpClient(host=ip, port=port, timeout=config.timeout, retries=0)
                if not client.connect(): return None, None
                values = []
                blocks = [(addresses[0], 2)] if addresses[1] == addresses[0]+1 else [(address,1) for address in addresses]
                for address, count in blocks:
                    reply = client.read_holding_registers(address, count=count, device_id=config.device_id)
                    words = getattr(reply, 'registers', None)
                    if reply is None or reply.isError() or not isinstance(words, (list, tuple)) or len(words) != count or any(
                        type(word) is not int or not 0 <= word <= 65535 for word in words):
                        return None, None
                    values.extend(words)
                return tuple(values)
            finally:
                if client is not None: client.close()
    except Exception:
        log.warning('Historical Modbus limit read unconfirmed')
        return None, None
