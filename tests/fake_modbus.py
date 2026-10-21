"""In-memory test double; never creates sockets or accesses hardware."""
from dataclasses import dataclass, field

@dataclass
class Response:
    registers: list = field(default_factory=list)
    error: bool = False
    address: int = 0
    count: int = 0

    def isError(self):
        return self.error

class FakeModbusTcpClient:
    def __init__(self, host, *, port=502, timeout=3, retries=0):
        self.host, self.port, self.timeout = host, port, timeout
        self.retries = retries
        self.online = True
        self.closed = False
        self.calls = []
        self.registers = {46: 50, 47: 9, 1000: 16, 1001: 32767}
        self.input_registers = {address: 0 for address in (*range(20, 36), 52, 53, 54)}
        self.input_registers[52] = 75
        self.read_error = False
        self.write_error = False

    def connect(self):
        self.calls.append(('connect',))
        return self.online

    def close(self):
        self.closed = True

    def write_register(self, address, value, *, device_id=1):
        self.calls.append(('write', address, value, device_id))
        if not self.write_error:
            self.registers[address] = value
        return Response([value], error=self.write_error, address=address)

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self.calls.append(('read', address, count, device_id))
        return Response([self.registers.get(address+i, 0) for i in range(count)],
                        error=self.read_error)

    def write_registers(self, address, values, *, device_id=1):
        self.calls.append(('write_multiple', address, list(values), device_id))
        if not self.write_error:
            self.registers.update({address+i: value for i, value in enumerate(values)})
        return Response(error=self.write_error, address=address, count=len(values))

    def read_input_registers(self, address, *, count=1, device_id=1):
        self.calls.append(('read_input', address, count, device_id))
        return Response([self.input_registers.get(address+i, 0) for i in range(count)], error=self.read_error)
