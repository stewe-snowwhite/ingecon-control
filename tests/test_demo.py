"""All demo route/data tests are offline; the autouse socket guard stays active."""
import hashlib
import importlib
import re
import socket
import sqlite3
import sys
import time

import click
import pytest
from click.testing import CliRunner

from demo import (DemoPaths, cli, create_demo_app, demo_lock, initialize,
                  refresh_simulation, serve)
from demo_safety import DemoNetworkGuard, DemoSafetyError, disable_device_transports
from models import db, Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, StateEvent, User
from monitor import MonitorWorker
from tests.helpers import post, confirmed_post


@pytest.fixture
def demo(tmp_path):
    paths = DemoPaths(tmp_path / 'private-demo')
    with demo_lock(paths):
        application = initialize(paths)
    application.config['TESTING'] = True
    refresh_simulation(application)
    yield paths, application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()


def admin_client(application, role='Administrator'):
    with application.app_context():
        user = User(username='tester', role=role)
        user.set_password('synthetic-test-password')
        db.session.add(user)
        db.session.commit()
        user_id, version = user.id, user.session_version
    client = application.test_client()
    with client.session_transaction() as session:
        session['user_id'], session['session_version'] = user_id, version
    return client


def test_factory_import_does_not_construct_production_app(monkeypatch):
    monkeypatch.delenv('INGECON_SECRET_KEY')
    sys.modules.pop('app', None)
    module = importlib.import_module('app')
    assert 'app' not in vars(module)
    assert callable(module.create_app)


def test_seed_is_private_synthetic_and_idempotent(demo, monkeypatch):
    paths, application = demo
    assert paths.directory.stat().st_mode & 0o777 == 0o700
    for path in (paths.database, paths.secret, paths.log, paths.lock):
        assert path.stat().st_mode & 0o777 == 0o600
    assert len(paths.secret_value()) >= 32
    with application.app_context():
        assert db.engine.url.database == str(paths.database)
        assert db.session.scalar(db.select(db.func.count(User.id))) == 0
        inverters = db.session.scalars(db.select(Inverter)).all()
        assert len(inverters) == 4 and all(not inv.password and not inv.login for inv in inverters)
        assert {s.state for s in db.session.scalars(db.select(InverterStatus))} == {
            'Online', 'Offline', 'Degraded', 'Unknown'}
        assert all(c.read_address is None for c in db.session.scalars(db.select(MonitorConfig)))
        inverters[0].name = 'My demo change'
        db.session.commit()
    monkeypatch.setenv('INGECON_DATABASE_URI', 'sqlite:////unavailable/production.sqlite')
    monkeypatch.setenv('INGECON_SECRET_KEY', 'production-key-must-never-be-used-' * 2)
    with demo_lock(paths):
        second = initialize(paths)
    assert second.config['SECRET_KEY'] == paths.secret_value()
    with second.app_context():
        assert db.session.get(Inverter, 1).name == 'My demo change'
        assert db.session.scalar(db.select(db.func.count(Inverter.id))) == 4
        db.engine.dispose()


def test_interactive_admin_password_hash_hidden_and_session_revoked(demo):
    paths, application = demo
    runner = CliRunner()
    password = 'separate-test-password-123'
    args = ['--directory', str(paths.directory), 'setup-admin']
    result = runner.invoke(cli, args, input=f'DemoAdmin\n{password}\n{password}\n')
    assert result.exit_code == 0 and password not in result.output
    with application.app_context():
        user = db.session.scalar(db.select(User).where(User.username == 'demoadmin'))
        assert user.role == 'Administrator' and user.active
        assert user.password_hash.startswith('scrypt:') and user.check_password(password)
        assert password not in user.password_hash
    client = application.test_client()
    response = post(client, '/login', {'username': 'DemoAdmin', 'password': password})
    assert response.status_code == 302
    cookie = response.headers['Set-Cookie']
    assert 'ingecon_demo=' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    assert 'Secure' not in cookie
    assert client.get('/').status_code == 200
    changed = 'another-test-password-456'
    result = runner.invoke(cli, args, input=f'DemoAdmin\n{changed}\n{changed}\n')
    assert result.exit_code == 0 and changed not in result.output
    assert client.get('/').status_code == 401


@pytest.mark.parametrize('input_text', ['bad user\n', 'tester\nshort\nshort\n',
                                       'tester\nlong-test-password\nwrong-confirmation\n'])
def test_interactive_admin_rejects_invalid_inputs(demo, input_text):
    paths, application = demo
    result = CliRunner().invoke(cli, ['--directory', str(paths.directory), 'setup-admin'], input=input_text)
    assert result.exit_code != 0
    with application.app_context():
        assert db.session.scalar(db.select(db.func.count(User.id))) == 0


@pytest.mark.parametrize('path,code', [('/', 200), ('/history', 200), ('/add', 200),
    ('/edit/1', 200), ('/login', 200), ('/missing', 404)])
def test_demo_html_pages(demo, path, code):
    _, application = demo
    client = admin_client(application)
    response = client.get(path)
    html = response.get_data(as_text=True)
    assert response.status_code == code
    assert 'ДЕМО' in html and 'lang="uk"' in html
    assert 'script-src \'self\'' in response.headers['Content-Security-Policy']
    assert 'csrf_token' in html and 'cdn.' not in html


@pytest.mark.parametrize('path', ['/static/panel.css', '/static/theme.js',
    '/static/panel.js', '/static/dashboard.js'])
def test_demo_local_assets(demo, path):
    _, application = demo
    assert application.test_client().get(path).status_code == 200


def test_demo_api_filters_heartbeat_and_no_history_for_regular_refresh(demo, modbus):
    _, application = demo
    client = admin_client(application)
    result = client.get('/api/statuses').json
    assert result['counts'] == dict(total=4, Online=1, Offline=1, Degraded=1, Unknown=1)
    assert result['monitor']['alive']
    online = next(row for row in result['inverters'] if row['state'] == 'Online')
    assert online['checks']['icmp']['ok'] is False and online['checks']['modbus']['ok'] is True
    assert len(client.get('/api/statuses?group=Поле&state=Offline').json['inverters']) == 1
    assert len(client.get('/api/statuses?q=НОВИЙ').json['inverters']) == 1
    with application.app_context():
        initial_events = db.session.scalar(db.select(db.func.count(StateEvent.id)))
        heartbeat = db.session.get(MonitorHeartbeat, 1)
        heartbeat.last_seen = time.time()-1000
        db.session.commit()
    stale = client.get('/api/statuses').json
    assert stale['counts']['Unknown'] == 4 and not stale['monitor']['alive']
    refresh_simulation(application)
    assert client.get('/api/statuses').json['counts'] == result['counts']
    with application.app_context():
        assert db.session.scalar(db.select(db.func.count(StateEvent.id))) == initial_events
    refresh_simulation(application, stopped=True)
    assert not client.get('/api/statuses').json['monitor']['alive']
    assert modbus[1] == []


@pytest.mark.parametrize('action', ['on', 'off', 'limit', 'limit_max'])
@pytest.mark.parametrize('accidentally_enabled', [False, True])
def test_demo_writes_always_forbidden(demo, modbus, action, accidentally_enabled):
    _, application = demo
    client = admin_client(application)
    application.config['MODBUS_WRITES_ENABLED'] = accidentally_enabled
    result = post(client, '/control', {'action': action, 'selected_ids': '1', 'limit_value':'25'},
                  headers={'Accept': 'application/json'})
    assert result.status_code == 403 and 'деморежимі' in result.json['error']
    assert modbus[1] == []


def test_crud_confirmation_results_and_queued_checks_are_local(demo, modbus):
    _, application = demo
    client = admin_client(application)
    fields = dict(name='Demo CRUD', group='Extra', ip='192.0.2.99', password='demo-only-device-secret')
    assert post(client, '/add', fields).status_code == 302
    refresh_simulation(application)
    assert client.get('/api/statuses').json['counts']['total'] == 5
    assert 'demo-only-device-secret' not in client.get('/edit/5').get_data(as_text=True)
    fields['ip'] = '192.0.2.100'
    assert post(client, '/edit/1', fields).status_code == 302
    assert post(client, '/test/1').status_code == 302
    result = post(client, '/control', {'action':'test', 'selected_ids':['1','2']})
    assert result.status_code == 200 and 'Перевірку заплановано' in result.get_data(as_text=True)
    refresh_simulation(application)
    with application.app_context():
        assert db.session.get(InverterStatus, 1).state == 'Unknown'
        assert db.session.get(MonitorConfig, 1).requested_at is None
        assert db.session.scalar(db.select(db.func.count(StateEvent.id))) == 4
    preview = post(client, '/delete/5')
    assert preview.status_code == 200 and 'confirmation_token' in preview.get_data(as_text=True)
    assert confirmed_post(client, '/delete/5').status_code == 200
    assert client.get('/edit/5').status_code == 404
    assert 'ДЕМО' in client.get('/download-log').get_data(as_text=True)
    assert modbus[1] == []


@pytest.mark.parametrize('role', ['Administrator', 'Operator', 'Viewer'])
def test_auth_csrf_roles_and_logout(demo, modbus, role):
    _, application = demo
    anonymous = application.test_client()
    for path in ('/', '/history', '/api/statuses', '/add', '/edit/1', '/download-log'):
        assert anonymous.get(path).status_code == 401
    assert anonymous.post('/login', data={'username':'tester', 'password':'synthetic-test-password'}).status_code == 400
    client = admin_client(application, role)
    assert client.get('/').status_code == 200
    assert client.get('/add').status_code == (200 if role == 'Administrator' else 403)
    assert client.get('/download-log').status_code == (200 if role == 'Administrator' else 403)
    assert post(client, '/test/1').status_code == (403 if role == 'Viewer' else 302)
    assert client.post('/control', data={'action':'test', 'selected_ids':'1'}).status_code == 400
    assert post(client, '/logout').status_code == 302
    assert client.get('/').status_code == 401 and modbus[1] == []


def test_serve_requires_interactive_admin_before_listener(demo, monkeypatch):
    _, application = demo
    def forbidden(*args, **kwargs):
        pytest.fail('No listener before an administrator exists')
    monkeypatch.setattr('demo.DemoHTTPServer', forbidden)
    with pytest.raises(click.ClickException, match='setup-admin'):
        serve(application, '127.0.0.1')


def test_real_monitor_rejects_demo_before_probes(demo):
    _, application = demo
    class ForbiddenProbes:
        def poll(self, target):
            pytest.fail('Real monitoring must never probe a demo DB')
    with application.app_context():
        with pytest.raises(RuntimeError, match='Demo database'):
            MonitorWorker(db.engine, probes=ForbiddenProbes()).run(once=True)


@pytest.mark.parametrize('kind', ['unmarked-db', 'changed-key', 'db-symlink', 'log-symlink', 'public-key'])
def test_invalid_runtime_files_rejected_without_changes(demo, tmp_path, kind):
    paths, application = demo
    with application.app_context():
        db.engine.dispose()
    if kind == 'unmarked-db':
        with sqlite3.connect(paths.database) as connection:
            connection.execute('DROP TABLE demo_metadata')
    elif kind == 'changed-key':
        paths.secret.write_text('a-different-key-for-demo-' * 3)
    elif kind == 'public-key':
        paths.secret.chmod(0o644)
    else:
        target = paths.database if kind == 'db-symlink' else paths.log
        original = tmp_path / 'untouched'
        target.rename(original)
        target.symlink_to(original)
    before = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    with pytest.raises(click.ClickException):
        create_demo_app(paths)
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before


def test_private_directory_and_singleton_lock(tmp_path):
    paths = DemoPaths(tmp_path / 'private')
    with demo_lock(paths):
        with pytest.raises(click.ClickException, match='Інший процес'):
            with demo_lock(paths):
                pass
    paths.directory.chmod(0o755)
    with pytest.raises(click.ClickException, match='0700'):
        with demo_lock(paths):
            pass


def test_directory_punctuation_cannot_redirect_sqlite(tmp_path):
    paths = DemoPaths(tmp_path/'private?mode=rw#literal')
    with demo_lock(paths):
        application = initialize(paths)
    with application.app_context():
        assert db.engine.url.database == str(paths.database)
        assert db.session.scalar(db.select(db.func.count(Inverter.id))) == 4
        db.engine.dispose()
    assert paths.database.is_file()
    assert not (tmp_path/'private').exists()


@pytest.mark.parametrize('event,args', [
    ('socket.connect', (None, ('192.0.2.1',502))), ('socket.sendto', (None,None)),
    ('socket.gethostbyname', ('example.invalid',)), ('socket.gethostbyaddr', ('192.0.2.1',)),
    ('socket.getnameinfo', (('192.0.2.1',502),0)),
    ('socket.gethostname', ()), ('socket.getaddrinfo', ('example.invalid',80,2,1,6)),
    ('socket.bind', (None,('0.0.0.0',5000))), ('socket.bind', (None,('127.0.0.1',502))),
    ('socket.__new__', (None,socket.AF_INET,socket.SOCK_DGRAM,0)),
    ('socket.__new__', (None,socket.AF_INET,socket.SOCK_RAW,0)),
    ('subprocess.Popen', ('ping',)), ('os.system', ('ping',)), ('os.fork', ()),
    ('os.exec', ('ping',)), ('os.posix_spawn', ('ping',)),
])
def test_outbound_audit_events_are_denied(event, args):
    with pytest.raises(DemoSafetyError):
        DemoNetworkGuard()(event, args)


@pytest.mark.parametrize('host', ['127.0.0.1', '0.0.0.0'])
def test_only_explicit_listener_audit_events_allowed(host):
    guard = DemoNetworkGuard(host)
    guard('socket.__new__', (None,socket.AF_INET,socket.SOCK_STREAM,0))
    guard('socket.bind', (None,(host,5000)))
    guard('socket.getaddrinfo', (host,5000,socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP))
    with pytest.raises(DemoSafetyError):
        guard('socket.connect', (None,(host,5000)))
    with pytest.raises(ValueError):
        DemoNetworkGuard('192.0.2.10')


def test_lan_host_auth_csrf_and_api(demo):
    _, application = demo
    client = application.test_client()
    host = {'Host': '192.168.10.234:5000'}
    assert client.get('/login', headers=host).status_code == 200
    assert client.get('/api/statuses', headers=host).status_code == 401
    assert client.get('/login', headers={'Host':'untrusted.invalid:5000'}).status_code == 400
    invalid = client.get('/api/statuses', headers={'Host':'untrusted.invalid:5000'})
    assert invalid.status_code == 400 and invalid.json['error'] == 'Недозволений Host.'
    assert b'untrusted.invalid' not in invalid.data
    client = admin_client(application)
    # Authenticate on the actual LAN host: cookies are scoped to that host.
    def submit(path, data):
        html = client.get('/login', headers=host).get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        return client.post(path, headers=host, data=dict(data, csrf_token=token))
    assert submit('/login', {'username':'tester','password':'synthetic-test-password'}).status_code == 302
    result = client.get('/api/statuses', headers=host)
    assert result.status_code == 200 and result.json['counts']['total'] == 4
    assert client.post('/control', headers=host,
                       data={'action':'test', 'selected_ids':'1'}).status_code == 400
    assert client.post('/control', headers=host,
                       data={'action':'test', 'selected_ids':'1','csrf_token':'forged'}).status_code == 400
    assert submit('/control', {'action':'on', 'selected_ids':'1'}).status_code == 403


@pytest.mark.parametrize('arguments,expected', [([], '127.0.0.1'), (['--host','0.0.0.0'], '0.0.0.0')])
def test_serve_cli_host_selection_without_starting_server(demo, monkeypatch, arguments, expected):
    paths, _ = demo
    calls = []
    monkeypatch.setattr('demo.serve', lambda application,host:calls.append((application,host)))
    result = CliRunner().invoke(cli, ['--directory',str(paths.directory),'serve',*arguments])
    assert result.exit_code == 0 and calls[0][1] == expected
    assert calls[0][0].config['DEMO_MODE'] and not calls[0][0].debug


def test_transport_blocking_is_independent_of_network_hook(monkeypatch):
    import modbus_utils
    import monitoring
    import pymodbus.client
    # Restore process globals automatically; never install an audit hook in pytest.
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', modbus_utils.ModbusTcpClient)
    monkeypatch.setattr(pymodbus.client, 'ModbusTcpClient', pymodbus.client.ModbusTcpClient)
    for name in ('icmp','tcp','modbus','poll'):
        monkeypatch.setattr(monitoring.NetworkProbes, name, getattr(monitoring.NetworkProbes,name))
    disable_device_transports()
    with pytest.raises(DemoSafetyError):
        modbus_utils.ModbusTcpClient('192.0.2.1')
    with pytest.raises(DemoSafetyError):
        pymodbus.client.ModbusTcpClient('192.0.2.1')
    for name in ('icmp','tcp','modbus','poll'):
        with pytest.raises(DemoSafetyError):
            getattr(monitoring.NetworkProbes(),name)(None)
