"""Root-installed, stdlib-only HTTP guard. Never touches SSH, output or VPN rules.

--inspect is read-only. --run requires root and an existing ruleset backup made
by the documented activation procedure. The LAN trust policy assumes a trusted
router which does not SNAT forwarded Internet traffic to its own LAN address.
Use INGECON_HTTP_LAN=false on untrusted networks or when that is unknown.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

IP = '/usr/sbin/ip'
NFT = '/usr/sbin/nft'
TEMPLATE = Path('/etc/ingecon-control/http-guard.nft')
STATE = Path('/run/ingecon-http-guard/state.json')
MARKER = 'ingecon-control HTTP guard v1'
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
INTERFACE = re.compile(r'[A-Za-z0-9_.:-]{1,15}', re.ASCII)


def execute(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=5, **kwargs)


def inventory():
    # No probes, sockets, ping, DNS, or remote requests: ip only reads netlink.
    return (json.loads(execute([IP, '-details', '-json', 'address', 'show']).stdout),
            json.loads(execute([IP, '-json', '-4', 'route', 'show', 'table', 'main']).stdout))


def discover(addresses, routes, lan_enabled=True, physical=None):
    """Trust an exact on-link RFC1918 network on an UP physical Ethernet/WiFi NIC.

    Reject virtual/PPP/WAN/CGNAT, broad prefixes, gateways and conflicting
    assignments. DHCP scope-link routes are valid as well as kernel routes.
    """
    if not isinstance(addresses, list) or not isinstance(routes, list):
        raise ValueError('Invalid network inventory')
    if physical is None:
        physical = lambda name: (Path('/sys/class/net')/name/'device').exists()
    candidates, hosts = [], set()
    for link in addresses:
        name = link.get('ifname', '')
        if not INTERFACE.fullmatch(name) or 'UP' not in link.get('flags', []):
            continue
        if name == 'tailscale0':
            for addr in link.get('addr_info', []):
                try:
                    ip = ipaddress.ip_address(addr.get('local', ''))
                    if addr.get('scope') == 'global' and not ip.is_unspecified and not ip.is_multicast:
                        hosts.add(str(ip))
                except ValueError:
                    continue
            continue
        if (not lan_enabled or link.get('link_type') != 'ether' or
                link.get('operstate') != 'UP' or 'BROADCAST' not in link.get('flags', []) or
                'POINTOPOINT' in link.get('flags', []) or
                link.get('linkinfo', {}).get('info_kind') or not physical(name)):
            continue
        for addr in link.get('addr_info', []):
            try:
                ip = ipaddress.IPv4Address(addr.get('local', ''))
                prefix = addr['prefixlen']
                if type(prefix) is not int or not 16 <= prefix <= 30:
                    continue
                network = ipaddress.ip_network(f'{ip}/{prefix}', strict=False)
                if (addr.get('scope') != 'global' or addr.get('tentative') or addr.get('deprecated') or
                        ip in (network.network_address, network.broadcast_address) or
                        not any(network.subnet_of(private) for private in PRIVATE)):
                    continue
                matching = [r for r in routes if r.get('dst') == str(network)]
                # Multiple competing routes/interfaces are ambiguous; fail closed.
                if len(matching) != 1:
                    continue
                route = matching[0]
                if (route.get('dev') != name or route.get('scope') != 'link' or
                        route.get('type', 'unicast') != 'unicast' or
                        any(key in route for key in ('gateway', 'via', 'nexthops', 'nhid')) or
                        route.get('prefsrc', str(ip)) != str(ip) or 'linkdown' in route.get('flags', [])):
                    continue
                candidates.append((name, str(network), str(ip)))
            except (KeyError, TypeError, ValueError):
                continue
    # Overlapping LAN prefixes on separate interfaces are not trusted implicitly.
    lans = [entry for entry in candidates if not any(
        other[0] != entry[0] and ipaddress.ip_network(entry[1]).overlaps(ipaddress.ip_network(other[1]))
        for other in candidates)]
    hosts.update(entry[2] for entry in lans)
    return {'lan': [list(entry) for entry in sorted(set(lans))], 'hosts': sorted(hosts)}


def render_rules(snapshot, template):
    # Validate again at the command boundary; no external text enters nft syntax.
    entries = []
    for name, subnet, local in snapshot['lan']:
        network = ipaddress.IPv4Network(subnet)
        address = ipaddress.IPv4Address(local)
        if (not INTERFACE.fullmatch(name) or address not in network or
                not 16 <= network.prefixlen <= 30 or
                not any(network.subnet_of(private) for private in PRIVATE)):
            raise ValueError('Invalid LAN tuple')
        entries.append(f'"{name}" . {network} . {address}')
    return template.replace('# LAN_ELEMENTS', 'elements = { '+', '.join(entries)+' }' if entries else '')


def notify(message):
    target = os.environ.get('NOTIFY_SOCKET')
    if target:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect('\0'+target[1:] if target.startswith('@') else target)
            channel.sendall(message.encode())


def write_state(snapshot):
    content = json.dumps(snapshot, sort_keys=True)+'\n'
    if STATE.exists() and STATE.read_text() == content:
        return  # /run is RAM; avoid even unnecessary writes there.
    temporary = STATE.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    os.replace(temporary, STATE)


def check_ownership():
    listing = json.loads(execute([NFT, '-json', 'list', 'tables']).stdout)
    if any(item.get('table', {}).get('family') == 'inet' and
           item.get('table', {}).get('name') == 'ingecon_http_portal' for item in listing['nftables']):
        table = json.loads(execute([NFT, '-json', 'list', 'table', 'inet', 'ingecon_http_portal']).stdout)
        if not any(item.get('table', {}).get('comment') == MARKER for item in table['nftables']):
            raise RuntimeError('Existing HTTP table is not owned by this guard; refusing replacement')


def run():
    if os.geteuid() != 0:
        raise RuntimeError('Root/CAP_NET_ADMIN required; use the documented root procedure')
    raw = os.environ.get('INGECON_HTTP_LAN', 'true').lower()
    if raw not in ('true', 'false'):
        raise RuntimeError('INGECON_HTTP_LAN must be true or false')
    info = TEMPLATE.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Firewall template must be a root-owned regular file without writable group/other bits')
    template = TEMPLATE.read_text()
    check_ownership()
    # Install fail-closed baseline before attempting to discover any LAN.
    baseline = render_rules({'lan': []}, template)
    execute([NFT, '--check', '--file', '-'], input=baseline)
    execute([NFT, '--file', '-'], input=baseline)
    failures = 0
    discovery_failed = False
    while True:
        try:
            snapshot = discover(*inventory(), lan_enabled=raw == 'true')
            discovery_failed = False
        except (OSError, ValueError, subprocess.SubprocessError):
            snapshot = {'lan': [], 'hosts': []}
            if not discovery_failed:
                print('Network discovery unavailable: LAN HTTP disabled', file=sys.stderr)
            discovery_failed = True
        try:
            # Atomic own-table transaction; expiring entries fail closed if we die.
            execute([NFT, '--file', '-'], input=render_rules(snapshot, template))
            write_state(snapshot)
            failures = 0
        except (OSError, ValueError, subprocess.SubprocessError):
            failures += 1
            print('HTTP guard refresh failed; LAN leases will expire', file=sys.stderr)
            if failures >= 2:
                raise RuntimeError('Unable to maintain HTTP guard') from None
        if not failures:
            notify('READY=1\nWATCHDOG=1')
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--template', type=Path, default=TEMPLATE)
    args = parser.parse_args()
    if args.run:
        run()
    elif args.inspect:
        print(json.dumps(discover(*inventory(), lan_enabled=os.environ.get('INGECON_HTTP_LAN', 'true').lower() == 'true'), indent=2))
    elif args.render:
        print(render_rules(discover(*inventory(), lan_enabled=os.environ.get('INGECON_HTTP_LAN', 'true').lower() == 'true'), args.template.read_text()))
    else:
        parser.error('Choose --inspect/--render (read-only) or --run (root)')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        # Do not emit subprocess output or confidential host configuration.
        sys.exit('HTTP guard failed; inspect root configuration and journal. Rules were not cleared.')
