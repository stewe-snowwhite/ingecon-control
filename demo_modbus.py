"""A synthetic FC03-only Modbus TCP device. This is NOT an Ingeteam map."""
import socket
import socketserver
import struct
import sys
import time

from demo_monitor_store import HOST, PORT, DEVICE

MAGIC, VERSION, COUNT = 0xD008, 1, 8


def registers(scenario, load):
    power = round(5000*load/100) if scenario in ('normal','recovery') else 0
    return [MAGIC,VERSION,power,2300,round(power/230*10),5000,
            850 if scenario=='fault' else round(350+load*1.5),
            1 if scenario=='fault' else 2 if scenario=='stopped' else 0]


def decode(values):
    if (len(values)!=COUNT or any(type(v) is not int or not 0<=v<=65535 for v in values) or
            values[:2] not in ([MAGIC,VERSION],(MAGIC,VERSION)) or values[7] not in (0,1,2)):
        raise ValueError('Not a valid TASK-08 test register response')
    return {'power_w':values[2],'voltage_v':values[3]/10,'current_a':values[4]/10,
            'frequency_hz':values[5]/100,'temperature_c':values[6]/10,
            'equipment':('Running','Alarm','Stopped')[values[7]]}


def response_pdu(pdu, values, unit=DEVICE):
    function = pdu[0] if pdu else 0
    if function != 3:
        return bytes([function|0x80,1])  # All writes: Illegal Function, no changes.
    if len(pdu)!=5:
        return bytes([0x83,3])
    address,count = struct.unpack('>HH',pdu[1:])
    if unit!=DEVICE or not 1<=count<=COUNT or address+count>COUNT:
        return bytes([0x83,2])
    return bytes([3,count*2])+struct.pack('>'+('H'*count),*values[address:address+count])


def receive(connection, count):
    data = b''
    while len(data)<count:
        part = connection.recv(count-len(data))
        if not part:
            return None
        data += part
    return data


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        try:
            for _ in range(16):
                header = receive(self.request,6)
                if header is None: return
                transaction,protocol,length = struct.unpack('>HHH',header)
                if protocol!=0 or not 2<=length<=254: return
                body = receive(self.request,length)
                if body is None: return
                scenario,load = self.server.store.settings()
                if scenario=='offline':
                    time.sleep(1)  # TCP alive; no Modbus response, genuine timeout.
                    return
                pdu = response_pdu(body[1:],registers(scenario,load),body[0])
                self.request.sendall(struct.pack('>HHHB',transaction,0,len(pdu)+1,body[0])+pdu)
        except (OSError,TimeoutError):
            return


class Simulator(socketserver.TCPServer):
    allow_reuse_address = True
    def __init__(self, store):
        self.store = store
        super().__init__((HOST,PORT),Handler)


class LoopbackGuard:
    """Process-wide guard; never install in Flask or the Telegram process."""
    def __init__(self, simulator=False):
        self.simulator = simulator

    def __call__(self, event, args):
        if event=='socket.__new__':
            _,family,kind,protocol = args
            if family!=socket.AF_INET or kind!=socket.SOCK_STREAM or protocol not in (0,socket.IPPROTO_TCP):
                raise RuntimeError('TASK-08 allows only the loopback test TCP transport')
        if event=='socket.connect' and (self.simulator or args[1]!=(HOST,PORT)):
            raise RuntimeError('Physical/remote connections are forbidden in TASK-08')
        if event=='socket.bind' and (not self.simulator or args[1]!=(HOST,PORT)):
            raise RuntimeError('Only the fixed loopback simulator may bind')
        if event=='socket.getaddrinfo' and (args[0]!=HOST or args[1]!=PORT):
            raise RuntimeError('TASK-08 DNS is disabled')
        if event in ('socket.sendto','socket.gethostbyname','socket.gethostbyaddr','socket.getnameinfo',
                     'subprocess.Popen','os.system','os.exec','os.posix_spawn','os.fork'):
            raise RuntimeError('TASK-08 remote traffic and subprocesses are forbidden')


def install_guard(*, simulator=False):
    sys.addaudithook(LoopbackGuard(simulator))
