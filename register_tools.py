"""Strict register request validation and bounded reads; no test-panel write API."""
import ipaddress
from dataclasses import asdict, dataclass

import modbus_utils
from operation_locks import OperationBusy, endpoint_key, resource_lock

FUNCTIONS = {3: 'FC03 · Holding Registers', 4: 'FC04 · Input Registers',
             6: 'FC06 · один Holding Register (запис заблоковано)',
             16: 'FC16 · кілька Holding Registers (запис заблоковано)'}


@dataclass(frozen=True)
class RegisterRequest:
    inverter_id: int
    ip: str
    port: int
    device_id: int
    function: int
    address: int
    count: int
    values: tuple = ()
    timeout: int = 2

    def validate(self):
        for value, low, high in ((self.inverter_id, 1, 2**63-1), (self.port, 1, 65535),
            (self.device_id, 1, 247), (self.address, 0, 65535), (self.count, 1, 125), (self.timeout, 1, 10)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Некоректні параметри запиту Modbus.')
        if type(self.function) is not int or self.function not in FUNCTIONS:
            raise ValueError('Дозволені лише FC03, FC04, FC06 та FC16.')
        ip = ipaddress.ip_address(self.ip)
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip.is_reserved:
            raise ValueError('Недозволена IP-адреса.')
        if self.address+self.count > 65536:
            raise ValueError('Діапазон виходить за межі регістрів.')
        if not isinstance(self.values, tuple) or any(type(value) is not int or not 0 <= value <= 65535 for value in self.values):
            raise ValueError('Значення регістрів: цілі числа 0–65535.')
        if self.function in (3, 4) and self.values:
            raise ValueError('Для читання поле значення має бути порожнім.')
        if self.function == 6 and (self.count != 1 or len(self.values) != 1):
            raise ValueError('FC06 потребує рівно один регістр і одне значення.')
        if self.function == 16 and (self.count > 123 or len(self.values) != self.count):
            raise ValueError('FC16: 1–123 значення, кількість має збігатися.')
        return self

    def payload(self):
        result = asdict(self)
        result['values'] = list(self.values)
        return result


def read_registers(request):
    request.validate()
    if request.function not in (3, 4):
        # This panel intentionally never delegates to any PyModbus write method.
        raise ValueError('Тестова панель не виконує Modbus-записи.')
    client = None
    try:
        with resource_lock(endpoint_key(request.ip, request.port)):
            try:
                client = modbus_utils.ModbusTcpClient(host=request.ip, port=request.port,
                    timeout=request.timeout, retries=0)
                if not client.connect():
                    return dict(outcome='unconfirmed', message='Немає підключення.', registers=[])
                method = client.read_holding_registers if request.function == 3 else client.read_input_registers
                reply = method(request.address, count=request.count, device_id=request.device_id)
                values = getattr(reply, 'registers', None)
                if reply is None or reply.isError() or not isinstance(values, (list, tuple)) or len(values) != request.count or any(
                    type(value) is not int or not 0 <= value <= 65535 for value in values):
                    return dict(outcome='unconfirmed', message='Некоректна або відхилена відповідь.', registers=[])
                return dict(outcome='read', message='Читання підтверджено.', registers=list(values))
            finally:
                if client is not None: client.close()
    except OperationBusy:
        return dict(outcome='busy', message='Інша Modbus-операція вже виконується.', registers=[])
    except Exception:
        return dict(outcome='unconfirmed', message='Читання не підтверджено; автоматичного повторення немає.', registers=[])
