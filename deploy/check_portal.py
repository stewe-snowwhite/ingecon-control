"""Explicit post-activation checks. HTTP reads and Telegram getMe only.

Run as codex using the project venv. Never sends getUpdates, sendMessage,
inverter traffic or control commands; does not authenticate as a real user.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from deploy.http_guard import discover, inventory
from telegram_bot import load_token


def http_check(base):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None):
        # Use the URL's actual numeric Host: DHCP access must work without DNS.
        query = urllib.request.Request(base+path, data=data)
        try:
            return opener.open(query,timeout=5)
        except urllib.error.HTTPError as error:
            return error
    with request('/login') as response:
        assert response.status == 200, 'Login page'
        cookie = response.headers.get('Set-Cookie','')
        assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Secure' not in cookie, 'HTTP cookie policy'
    with request('/api/statuses') as response:
        assert response.status == 401, 'Anonymous API authorization'
    with request('/login',data=b'username=invalid-probe') as response:
        assert response.status == 400, 'CSRF rejection'
    print(base+': login=200, anonymous API=401, missing CSRF=400, cookie OK')


def service_check(service):
    for state in ('is-active','is-enabled'):
        result = subprocess.run(['systemctl',state,service],check=True,capture_output=True,text=True,timeout=5)
        print(service,state,result.stdout.strip())


def telegram_check():
    main_pid = subprocess.run(['systemctl','show','ingecon-telegram','--property=MainPID','--value'],
                              check=True,capture_output=True,text=True,timeout=5).stdout.strip()
    pollers = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            args = (proc/'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if (any(arg.endswith(b'telegram_bot.py') for arg in args) or b'telegram_bot' in args) and b'run' in args:
            pollers.append(proc.name)
    assert main_pid != '0' and pollers == [main_pid], 'Expected exactly one systemd Telegram poller'
    # Never display exceptions from urllib: its URL contains the secret token.
    token = load_token('/opt/ingecon-control/instance/telegram/bot.key')
    with urllib.request.urlopen('https://api.telegram.org/bot'+token+'/getMe',timeout=10) as response:
        payload = json.load(response)
    assert payload.get('ok') and payload.get('result',{}).get('is_bot'), 'Telegram getMe failed'
    print('Telegram: single polling MainPID '+main_pid+', getMe OK; token hidden')


def tailscale_check(addresses):
    state = subprocess.run(['systemctl','is-active','tailscaled'],capture_output=True,text=True,timeout=5)
    targets = [addr['local'] for link in addresses if link.get('ifname') == 'tailscale0'
               and 'UP' in link.get('flags',[]) for addr in link.get('addr_info',[])
               if addr.get('family') in ('inet','inet6') and addr.get('scope') == 'global']
    if state.returncode != 0:
        print('Optional Tailscale unavailable; LAN and Telegram do not require it.')
        return
    assert targets, 'Tailscale is active but has no usable VPN address'
    for target in targets:
        http_check('http://'+('['+target+']' if ':' in target else target))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-no-lan',action='store_true',
                        help='Explicit diagnostic fallback only when no LAN can be safely determined')
    args = parser.parse_args(argv)
    failures = []
    def check(label, operation):
        try:
            operation()
        except Exception:
            # Keep checking independent channels, without leaking token URLs.
            print(label+': FAILED (details omitted to protect credentials)')
            failures.append(label)
    for service in ('ingecon-http-guard','ingecon-web','ingecon-telegram','nginx'):
        check(service,lambda name=service:service_check(name))
    check('Gunicorn loopback',lambda:http_check('http://127.0.0.1:5000'))
    check('Nginx loopback',lambda:http_check('http://127.0.0.1:80'))
    addresses, routes = [], []
    def network_checks():
        nonlocal addresses, routes
        addresses, routes = inventory()
        snapshot = discover(addresses,routes)
        if not snapshot['lan']:
            print('No safely determined LAN. Do not widen HTTP rules; review the local network.')
            if not args.allow_no_lan:
                raise RuntimeError('Primary LAN access cannot be verified')
        for address in sorted({entry[2] for entry in snapshot['lan']}):
            check('Primary LAN '+address,lambda target=address:http_check('http://'+target))
    check('LAN discovery',network_checks)
    check('Telegram',telegram_check)
    check('Optional Tailscale',lambda:tailscale_check(addresses))
    if failures:
        raise RuntimeError('Failed checks: '+', '.join(failures))
    print('Required checks passed. Confirm LAN and available Tailscale from another client; no reboot was performed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Post-activation check failed. Inspect services locally; no credentials were printed.')
