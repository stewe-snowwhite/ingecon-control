"""HTTP deployment tests use a synthetic SQLite and no listener or device I/O."""
import importlib
import os
import re
import runpy
import sqlite3
from pathlib import Path

import pytest
from flask import jsonify, request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import portal
from configuration import cookie_secure_from_env, read_secret_file, secret_from_env
from deploy.prepare_portal import prepare, private_write
from models import db, User, Inverter
from telegram_store import TelegramStore, TelegramAccess
from tests.helpers import post

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def http_portal(tmp_path, monkeypatch):
    database = tmp_path/'ingecon.sqlite'
    engine = create_engine('sqlite:///'+str(database))
    db.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        user = User(username='stewe', role='Administrator')
        user.set_password('synthetic-http-password')
        session.add(user)
        session.add(Inverter(name='Mock only', group='Demo', ip_address='192.0.2.1'))
    engine.dispose()
    key = tmp_path/'secret.key'
    private_write(key, 'synthetic-test-key-never-production-'*2)
    monkeypatch.delenv('INGECON_SECRET_KEY', raising=False)
    monkeypatch.setenv('INGECON_SECRET_KEY_FILE', str(key))
    monkeypatch.setenv('INGECON_DATABASE_URI', 'sqlite:///'+str(database))
    monkeypatch.setenv('INGECON_COOKIE_SECURE', 'false')
    monkeypatch.setattr(portal, 'PRODUCTION_DATABASE', database)
    blocked = []
    monkeypatch.setattr(portal, 'disable_device_transports', lambda: blocked.append(True))
    application = portal.create_portal_app()
    application.config['TESTING'] = True
    assert blocked == [True]
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.mark.parametrize('raw,expected', [(None,True),('true',True),('TRUE',True),('false',False),(' False ',False)])
def test_cookie_policy_requires_explicit_false(monkeypatch,raw,expected):
    if raw is None: monkeypatch.delenv('INGECON_COOKIE_SECURE',raising=False)
    else: monkeypatch.setenv('INGECON_COOKIE_SECURE',raw)
    assert cookie_secure_from_env() is expected


@pytest.mark.parametrize('raw',['','0','1','yes','no','invalid'])
def test_invalid_cookie_environment_fails_closed(monkeypatch,raw):
    monkeypatch.setenv('INGECON_COOKIE_SECURE',raw)
    with pytest.raises(RuntimeError,match='explicitly true or false'):
        importlib.import_module('app').create_app()


def test_default_factory_still_secure_and_explicit_config_boolean(monkeypatch):
    factory = importlib.import_module('app').create_app
    assert factory().config['SESSION_COOKIE_SECURE'] is True
    assert factory({'SESSION_COOKIE_SECURE':False}).config['SESSION_COOKIE_SECURE'] is False
    with pytest.raises(RuntimeError,match='boolean'): factory({'SESSION_COOKIE_SECURE':'false'})


def test_http_login_csrf_authorization_api_and_write_denial(http_portal,modbus):
    client = http_portal.test_client()
    base = 'http://monitoring.local'
    def submit(path, data):
        html = client.get(base+'/login').get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"',html).group(1)
        return client.post(base+path,data=dict(data,csrf_token=csrf))
    assert client.get(base+'/').status_code == 401
    assert client.get(base+'/api/statuses').status_code == 401
    assert client.post(base+'/login',data={'username':'stewe','password':'synthetic-http-password'}).status_code == 400
    result = submit('/login',{'username':'stewe','password':'synthetic-http-password'})
    assert result.status_code == 302
    cookie = result.headers['Set-Cookie']
    assert 'Secure' not in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    assert client.get(base+'/').status_code == 200
    assert client.get(base+'/api/statuses').json['counts']['total'] == 1
    assert client.post(base+'/control',data={'action':'on','selected_ids':'1'}).status_code == 400
    assert submit('/control',{'action':'on','selected_ids':'1'}).status_code == 403
    assert modbus[1] == []


def test_proxy_trust_and_host_scheme_spoofing(http_portal):
    @http_portal.get('/test-proxy')
    def probe():
        return jsonify(ip=request.remote_addr,scheme=request.scheme,host=request.host)
    client = http_portal.test_client()
    headers = {'X-Forwarded-For':'10.42.0.42','X-Forwarded-Proto':'https','X-Forwarded-Host':'evil.invalid'}
    result = client.get('http://monitoring.local/test-proxy',headers=headers,environ_overrides={'REMOTE_ADDR':'127.0.0.1'})
    assert result.json == dict(ip='10.42.0.42',scheme='http',host='monitoring.local')
    remote = client.get('http://monitoring.local/test-proxy',headers=headers,environ_overrides={'REMOTE_ADDR':'198.51.100.2'})
    assert remote.json['ip'] == '198.51.100.2'
    assert client.get('http://evil.invalid/login').status_code == 400


def test_portal_telegram_uses_same_database(http_portal,modbus):
    with http_portal.app_context():
        store = TelegramStore(db.engine)
        code = store.issue_code(1)
        assert store.redeem(1001,code) == ('stewe','Administrator')
        assert store.snapshot(1001)['inverters'][0]['name'] == 'Mock only'
        intent,_ = store.prepare(1001,'off','i',1)
        assert store.confirm(1001,intent)[0] == 'blocked'
        assert modbus[1] == []


def test_private_key_validation_and_persistence(tmp_path,monkeypatch):
    key = tmp_path/'web.key'
    value = 'synthetic-key-'*5
    private_write(key,value+'\n')
    before = key.stat().st_mtime_ns
    assert read_secret_file(key) == value
    assert read_secret_file(key) == value and key.stat().st_mtime_ns == before
    monkeypatch.delenv('INGECON_SECRET_KEY',raising=False)
    monkeypatch.setenv('INGECON_SECRET_KEY_FILE',str(key))
    assert secret_from_env() == value
    monkeypatch.setenv('INGECON_SECRET_KEY','other-secret-'*5)
    with pytest.raises(RuntimeError,match='either'): secret_from_env()
    key.chmod(0o644)
    with pytest.raises(RuntimeError,match='private'): read_secret_file(key)
    alias = tmp_path/'alias.key'
    alias.symlink_to(key)
    with pytest.raises(RuntimeError): read_secret_file(alias)


def test_missing_short_or_binary_key_rejected(tmp_path):
    path = tmp_path/'secret.key'
    with pytest.raises(RuntimeError): read_secret_file(path)
    private_write(path,'short')
    with pytest.raises(RuntimeError): read_secret_file(path)
    path.write_bytes(b'\xff'*64)
    with pytest.raises(RuntimeError): read_secret_file(path)


@pytest.mark.parametrize('uri',['','sqlite:///:memory:','sqlite:////tmp/other.sqlite','postgresql://localhost/db'])
def test_portal_rejects_wrong_database_without_creating_it(uri):
    with pytest.raises(RuntimeError): portal.validate_database(uri)


def test_portal_refuses_demo_marker_and_missing_schema(http_portal,monkeypatch):
    path = portal.PRODUCTION_DATABASE
    uri = 'sqlite:///'+str(path)
    before = path.read_bytes()
    portal.validate_database(uri)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection: connection.execute('CREATE TABLE demo_metadata (key TEXT)')
    with pytest.raises(RuntimeError): portal.validate_database(uri)
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE demo_metadata')
        connection.execute('DROP TABLE telegram_rate')
    with pytest.raises(RuntimeError): portal.validate_database(uri)


def test_preparation_preserves_database_stewe_token_demo_and_existing_key(tmp_path,monkeypatch):
    root = tmp_path/'project'
    production = root/'instance'/'production'
    production.mkdir(parents=True)
    (root/'deploy').mkdir()
    (root/'deploy'/'portal.env.example').write_text('INGECON_COOKIE_SECURE=false\n')
    database = production/'ingecon.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE user (username TEXT, password_hash TEXT)')
        connection.execute('INSERT INTO user VALUES (?,?)',('stewe','unchanged-hash-sentinel'))
    demo = root/'instance'/'demo'
    demo.mkdir()
    (demo/'demo.sqlite').write_bytes(b'unchanged-demo')
    telegram = root/'instance'/'telegram'
    telegram.mkdir()
    (telegram/'bot.key').write_text('unchanged-token-sentinel')
    original = database.read_bytes()
    backup,key,env = prepare(root)
    secret = read_secret_file(key)
    assert env.stat().st_mode & 0o777 == 0o600
    assert key.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
    assert database.read_bytes() == original
    second,_,_ = prepare(root)
    assert second != backup and read_secret_file(key) == secret
    assert (demo/'demo.sqlite').read_bytes() == b'unchanged-demo'
    assert (telegram/'bot.key').read_text() == 'unchanged-token-sentinel'
    with sqlite3.connect(backup) as connection:
        assert connection.execute('SELECT * FROM user').fetchone() == ('stewe','unchanged-hash-sentinel')


def test_gunicorn_is_loopback_http_and_small():
    config = runpy.run_path(str(ROOT/'deploy'/'gunicorn.conf.py'))
    assert config['bind'] == '127.0.0.1:5000'
    assert config['workers'] == 1 and config['threads'] == 2
    assert config['secure_scheme_headers'] == {} and not config['daemon']
    assert config['worker_tmp_dir'].startswith('/run/')


def test_deployment_config_scope_and_single_bot():
    nginx = (ROOT/'deploy'/'nginx'/'ingecon-control.conf').read_text()
    assert 'listen 80 default_server;' in nginx and 'listen [::]:80 default_server;' in nginx
    assert 'server_name monitoring.local;' in nginx
    assert 'proxy_pass http://127.0.0.1:5000;' in nginx
    assert 'ssl_certificate' not in nginx and 'listen 443' not in nginx
    assert '192.168.' not in nginx and 'allow ' not in nginx
    assert 'X-Forwarded-For $remote_addr;' in nginx
    guard = (ROOT/'deploy'/'nftables'/'http-guard.nft').read_text()
    lines = [line for line in guard.splitlines() if not line.lstrip().startswith('#')]
    actual = '\n'.join(lines)
    assert 'flush ruleset' not in actual and 'destroy table inet ingecon_http_portal' in actual
    assert 'iifname "tailscale0" tcp dport 80 accept' in actual
    assert '@lan_v4 accept' in actual and 'timeout 15s' in actual
    assert 'fib saddr . iif oif exists' in actual
    assert 'tcp dport 80 counter drop' in actual and 'policy accept;' in actual
    assert 'dport 22' not in actual and 'hook output' not in actual
    web = (ROOT/'deploy'/'systemd'/'ingecon-web.service').read_text()
    bot = (ROOT/'deploy'/'systemd'/'ingecon-telegram.service').read_text()
    assert '--database /opt/ingecon-control/instance/production/ingecon.sqlite' in bot
    assert '--token-file /opt/ingecon-control/instance/telegram/bot.key' in bot
    assert bot.count('ExecStart=') == 1 and 'prepare-db' not in bot
    for unit in (web,bot):
        assert 'WantedBy=multi-user.target' in unit and 'User=codex' in unit
        assert 'UMask=0077' in unit and 'ProtectSystem=strict' in unit
        assert 'instance/demo' in unit
