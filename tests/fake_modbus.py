"""In-memory test double; never creates sockets or accesses hardware."""
from dataclasses import dataclass, field

@dataclass
class Response:
    registers: list = field(default_factory=list)
    error: bool = False
    address: int = 0

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
