"""Read-only discovery boundaries. No ARP raw sockets, root, DNS or Modbus writes."""
import ipaddress
import json
import re
import socket
import subprocess
from dataclasses import dataclass

from deploy.http_guard import PRIVATE, discover, inventory, IP
from register_tools import RegisterRequest, read_registers

MAC_PREFIX = '00:25:ca:2c:'
MAX_HOSTS = 1024
WORKERS = 4


def private_network(raw):
    try:
        network = ipaddress.IPv4Network(raw, strict=True)
    except (TypeError, ValueError):
        raise ValueError('Потрібен коректний IPv4 CIDR із адресою мережі.') from None
    if not any(network.subnet_of(parent) for parent in PRIVATE) or not 22 <= network.prefixlen <= 30:
        raise ValueError('Дозволено лише RFC1918 CIDR /22–/30; максимум 1022 вузли в діапазоні.')
    return network


def local_inventory():
    """Reads netlink via ip only; no packets sent to devices."""
    addresses, routes = inventory()
    return discover(addresses, routes)['lan']


def route_to(ip):
    data = json.loads(subprocess.run([IP, '-json', '-4', 'route', 'get', str(ip)],
        check=True, text=True, capture_output=True, timeout=2).stdout)
    if len(data) != 1:
        raise ValueError('Неоднозначний маршрут.')
    return data[0]


@dataclass(frozen=True)
class Scope:
    cidr: str
    interface: str
    mode: str


def host_addresses(cidr, lans):
    own = {entry[2] for entry in lans}
    return [str(ip) for ip in private_network(cidr).hosts() if str(ip) not in own]


def routed_proof(cidr, interface, route, routes=None):
    # A default gateway alone does not prove a private routed network exists.
    routes = inventory()[1] if routes is None else routes
    network = private_network(cidr)
    for entry in routes:
        try:
            prefix = ipaddress.IPv4Network(entry.get('dst', ''))
            if (any(prefix.subnet_of(parent) for parent in PRIVATE) and network.subnet_of(prefix)
                    and entry.get('dev') == interface and entry.get('gateway') == route.get('gateway')
                    and entry.get('type', 'unicast') == 'unicast'
                    and not any(key in entry for key in ('via', 'nexthops', 'nhid', 'encap'))
                    and 'linkdown' not in entry.get('flags', [])):
                return True
        except ValueError:
            continue
    raise ValueError('Немає явного приватного маршруту до всього CIDR; default route недостатньо.')


def checked_scope(cidr, lans=None, lookup=None, routes=None):
    network = private_network(cidr)
    lans = local_inventory() if lans is None else lans
    lookup = route_to if lookup is None else lookup
    sample = host_addresses(str(network), lans)[0]
    matches = [entry for entry in lans if network.subnet_of(ipaddress.IPv4Network(entry[1]))]
    if len(matches) > 1:
        raise ValueError('Неоднозначний локальний сегмент.')
    if matches:
        interface, _, _ = matches[0]
        mode = 'local'
    else:
        if any(network.overlaps(ipaddress.IPv4Network(entry[1])) for entry in lans):
            raise ValueError('Діапазон частково перекриває локальний сегмент.')
        route = lookup(sample)
        interface = route.get('dev')
        mode = 'routed'
    scope = Scope(str(network), interface, mode)
    check_destination(sample, scope, lans=lans, lookup=lookup, routes=routes)
    return scope


def check_destination(ip, scope, lans=None, lookup=None, routes=None):
    """Rechecked immediately before every connection, not only at approval time.

    Only physical UP LAN devices recognized by the existing HTTP LAN guard;
    overlay/VPN/CGNAT/default-public gateways and route changes fail closed.
    """
    network = private_network(scope.cidr)
    address = ipaddress.IPv4Address(ip)
    if address not in network or address in (network.network_address, network.broadcast_address):
        raise ValueError('Адреса поза погодженим діапазоном.')
    lans = local_inventory() if lans is None else lans
    if str(address) in {entry[2] for entry in lans}:
        raise ValueError('Сканування самого сервера заборонене.')
    entries = [entry for entry in lans if entry[0] == scope.interface]
    if not entries:
        raise ValueError('Немає підтвердженого фізичного LAN-інтерфейсу.')
    route = (route_to if lookup is None else lookup)(address)
    if (route.get('dev') != scope.interface or route.get('type', 'unicast') != 'unicast'
            or any(key in route for key in ('via', 'nexthops', 'nhid', 'encap'))
            or 'linkdown' in route.get('flags', [])
            or route.get('prefsrc', route.get('src')) not in {entry[2] for entry in entries}):
        raise ValueError('Маршрут змінився або веде через службовий/VPN-інтерфейс.')
    direct = [entry for entry in entries if address in ipaddress.IPv4Network(entry[1])]
    if scope.mode == 'local':
        if not direct or route.get('gateway'):
            raise ValueError('Ціль більше не належить локальному L2-сегменту.')
    elif scope.mode == 'routed':
        gateway = route.get('gateway')
        if direct or not gateway or not any(ipaddress.IPv4Address(gateway) in ipaddress.IPv4Network(entry[1])
                                           and gateway not in (entry[2], str(ipaddress.IPv4Network(entry[1]).network_address),
                                                               str(ipaddress.IPv4Network(entry[1]).broadcast_address)) for entry in entries):
            raise ValueError('Потрібен маршрутизований приватний діапазон через локальний шлюз.')
        routed_proof(scope.cidr, scope.interface, route, routes)
    else:
        raise ValueError('Невідомий тип мережі.')
    return True


def neighbour(ip, interface):
    data = json.loads(subprocess.run([IP, '-json', '-4', 'neigh', 'show', 'to', str(ip), 'dev', interface],
        check=True, text=True, capture_output=True, timeout=2).stdout)
    valid = [row.get('lladdr', '').lower() for row in data if row.get('dst') == ip and
             row.get('dev', interface) == interface and not set(row.get('state', [])) & {'FAILED', 'INCOMPLETE'}]
    return next((mac for mac in valid if re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac)), None)


def tcp_open(ip):
    try:
        with socket.create_connection((ip, 502), timeout=.4):
            return True
    except OSError:
        return False


def matches_profile(words):
    """Plausibility, never a unique device/model identity. Stopped values may be 0."""
    if len(words) != 19 or all(value == 0 for value in words) or all(value == 65535 for value in words):
        return False
    # Existing documented FC04 offsets: AC currents/voltages/frequency, limit.
    return (words[0] != 65535 and all(value <= 6000 for value in words[4:7])
            and (words[7] == 0 or 4500 <= words[7] <= 6500)
            and 0 <= words[16] <= 100 and words[18] != 65535)


def probe(ip, scope, unit, boundary=check_destination, tcp=tcp_open, mac_lookup=neighbour, reader=read_registers):
    """Only FC04 20–35 and 52–54; global write flags cannot change this path."""
    if type(unit) is not int or not 1 <= unit <= 247:
        raise ValueError('Unit ID має бути в межах 1–247.')
    boundary(ip, scope)
    opened = tcp(ip)
    mac = mac_lookup(ip, scope.interface) if scope.mode == 'local' else None
    prefix = bool(mac and mac.startswith(MAC_PREFIX))
    if scope.mode == 'local' and mac and not prefix:
        return None
    if not opened and not prefix:
        return None
    result = dict(ip=ip, mac=mac, cidr=scope.cidr, port=502, device_id=unit,
                  tcp_ok=opened, modbus_ok=False, confidence='possible', reason='TCP 502 недоступний.')
    if not opened:
        return result
    if scope.mode == 'local' and not prefix:
        result['reason'] = 'MAC недоступна; локальну MAC-фільтрацію не підтверджено. FC04 не виконано.'
        return result
    words = []
    for address, count in ((20, 16), (52, 3)):
        boundary(ip, scope)
        reply = reader(RegisterRequest(1, ip, 502, unit, 4, address, count, timeout=1))
        if reply['outcome'] != 'read':
            result['reason'] = 'FC04 не підтверджено: '+reply['message']
            return result
        words.extend(reply['registers'])
    result['modbus_ok'] = True
    if matches_profile(words):
        result.update(confidence='profile', reason='Відповідає очікуваному профілю FC04; унікальну модель не визначено.')
    else:
        result['reason'] = 'Є відповідь FC04, але значення не відповідають очікуваному профілю.'
    return result
