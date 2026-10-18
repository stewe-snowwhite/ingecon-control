"""Explicit post-activation checks. HTTP reads and Telegram getMe only.

Run as codex using the project venv. Never sends getUpdates, sendMessage,
inverter traffic or control commands; does not authenticate as a real user.
"""
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from deploy.http_guard import inventory
from telegram_bot import load_token


def http_check(base):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, data=None):
        query = urllib.request.Request(base+path, data=data, headers={'Host':'monitoring.local'})
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


def main():
    for service in ('ingecon-http-guard','ingecon-web','ingecon-telegram','nginx','tailscaled'):
        for state in ('is-active','is-enabled'):
            result = subprocess.run(['systemctl',state,service],check=True,capture_output=True,text=True,timeout=5)
            print(service,state,result.stdout.strip())
    http_check('http://127.0.0.1:5000')
    http_check('http://127.0.0.1:80')
    addresses,_ = inventory()
    targets = [addr['local'] for link in addresses if link.get('ifname') == 'tailscale0'
               for addr in link.get('addr_info',[]) if addr.get('family') == 'inet']
    assert targets, 'No Tailscale IPv4 address'
    for target in targets:
        http_check('http://'+target)
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
    print('Local checks passed. Also check LAN/Tailscale from a DIFFERENT client; no reboot was performed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit('Post-activation check failed. Inspect services locally; no credentials were printed.')
