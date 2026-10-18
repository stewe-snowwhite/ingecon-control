"""Allow current server IP Hosts without binding deployment to a DHCP address."""
import ipaddress
import json
import os
from pathlib import Path
import stat

STATE = Path('/run/ingecon-http-guard/state.json')


def current_hosts():
    hosts = ['monitoring.local', 'localhost', '127.0.0.1', '[::1]']
    try:
        fd = os.open(STATE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                    info.st_mode & 0o022 or info.st_size > 16384):
                return hosts
            snapshot = json.load(stream)
        for value in snapshot['hosts']:
            ip = ipaddress.ip_address(value)
            hosts.append('['+str(ip)+']' if ip.version == 6 else str(ip))
    except (OSError, ValueError, KeyError, TypeError):
        pass  # A missing/bad state file never enables arbitrary Host headers.
    return hosts
