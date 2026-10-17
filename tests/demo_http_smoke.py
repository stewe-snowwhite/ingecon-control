"""Explicit loopback-only HTTP check, separate from the offline pytest suite.

Own temporary synthetic DB/admin; own guarded child server; no device probes.
Run: .venv/bin/python -m tests.demo_http_smoke
"""
import http.cookiejar
import json
import os
import re
import secrets
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:5000'


def main():
    # Unset production configuration; the child must depend only on demo files.
    environment = {key:value for key,value in os.environ.items()
                   if key not in ('INGECON_SECRET_KEY','INGECON_DATABASE_URI','FLASK_DEBUG')}
    environment['PYTHONUNBUFFERED'] = '1'
    with tempfile.TemporaryDirectory(prefix='ingecon-demo-http-') as directory:
        paths = Path(directory) / 'demo'
        command = [sys.executable,str(ROOT/'demo.py'),'--directory',str(paths)]
        password = secrets.token_urlsafe(32)
        subprocess.run(command+['setup-admin'],input=f'http-tester\n{password}\n{password}\n',
                       text=True,capture_output=True,check=True,cwd=ROOT,env=environment,timeout=30)
        process = subprocess.Popen(command+['serve'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                   text=True,cwd=ROOT,env=environment)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                ready = False
                deadline = time.monotonic()+10
                while time.monotonic() < deadline and process.poll() is None:
                    if selector.select(timeout=0.2):
                        line = process.stdout.readline()
                        if line.startswith('ДЕМО: http://127.0.0.1:5000'):
                            ready = True
                            break
                if not ready:
                    raise RuntimeError('Own demo listener did not start; check whether port 5000 is occupied.')
            opener = build_opener(ProxyHandler({}),HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def get(path):
                with opener.open(BASE+path,timeout=10) as response:
                    return response.read().decode('utf-8')
            def post(path,data):
                token = re.search(r'name="csrf_token" value="([^"]+)"',get('/login')).group(1)
                request = Request(BASE+path,data=urlencode(dict(data,csrf_token=token)).encode())
                return opener.open(request,timeout=10)
            try:
                get('/api/statuses')
                raise AssertionError('Anonymous API accepted')
            except HTTPError as error:
                assert error.code == 401
            assert 'ДЕМО' in get('/login')
            with post('/login',dict(username='http-tester',password=password)) as response:
                assert response.status == 200 and 'ДЕМО' in response.read().decode()
            for path in ('/','/history','/add','/edit/1','/login'):
                assert 'ДЕМО' in get(path)
            for path in ('/static/panel.css','/static/panel.js','/static/theme.js','/static/dashboard.js'):
                assert get(path)
            assert 'ДЕМО' in get('/download-log')
            result = json.loads(get('/api/statuses'))
            assert result['counts'] == dict(total=4,Online=1,Offline=1,Degraded=1,Unknown=1)
            assert result['monitor']['alive']
            with post('/control',dict(action='test',selected_ids='1')) as response:
                assert 'Перевірку заплановано' in response.read().decode()
            with post('/delete/4',{}) as response:
                assert 'confirmation_token' in response.read().decode()
            try:
                post('/control',dict(action='on',selected_ids='1'))
                raise AssertionError('Demo accepted write')
            except HTTPError as error:
                assert error.code == 403
            try:
                opener.open(Request(BASE+'/logout',data=b''),timeout=10)
                raise AssertionError('Demo accepted missing CSRF')
            except HTTPError as error:
                assert error.code == 400
            with post('/logout',{}) as response:
                assert response.status == 200
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
        assert process.returncode == 0, 'Demo did not shut down cleanly'
        with sqlite3.connect(paths/'demo.sqlite') as connection:
            assert connection.execute('SELECT stopped FROM monitor_heartbeat WHERE id=1').fetchone()[0]
    print('Demo HTTP: authenticated pages/assets/API/confirmation/results, CSRF and write denial passed; listener stopped.')


if __name__ == '__main__':
    main()
