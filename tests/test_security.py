import logging
from pathlib import Path
from models import LoginThrottle

import pytest

from tests.helpers import post, token
from tests.fake_modbus import FakeModbusTcpClient


def seed(module):
    inv = module.Inverter(name='Mock', group='A', ip_address='192.0.2.1',
                          login='device-user', password='device-secret-sentinel')
    module.db.session.add(inv)
    module.db.session.commit()
    return inv


def role(module, value):
    user = module.db.session.scalar(module.db.select(module.User))
    user.role = value
    module.db.session.commit()
    return user


ROUTES = [('/', 'GET'), ('/add', 'GET'), ('/add', 'POST'), ('/edit/1', 'GET'),
          ('/edit/1', 'POST'), ('/delete/1', 'POST'), ('/test/1', 'POST'),
          ('/control', 'POST'), ('/download-log', 'GET'), ('/logout', 'POST')]


@pytest.mark.parametrize('path,method', ROUTES)
def test_all_business_routes_require_login(web, modbus, path, method):
    module, _ = web
    _, clients = modbus
    seed(module)
    client = module.app.test_client()
    response = client.get(path) if method == 'GET' else post(client, path)
    assert response.status_code == 401
    assert clients == []


@pytest.mark.parametrize('user_role', ['Viewer', 'Operator', 'Administrator'])
@pytest.mark.parametrize('path,method', ROUTES[:-1])
def test_role_matrix(web, modbus, tmp_path, user_role, path, method):
    module, client = web
    inv = seed(module)
    role(module, user_role)
    module.app.config['MODBUS_LOG_PATH'] = str(tmp_path / 'test.log')
    Path(module.app.config['MODBUS_LOG_PATH']).write_text('synthetic log only')
    data = {'name': 'Edited', 'group': 'A', 'ip': '192.0.2.1', 'login': '', 'password': '',
            'selected_ids': str(inv.id), 'action': 'test'}
    response = client.get(path) if method == 'GET' else post(client, path, data)
    allowed = (path == '/' or user_role == 'Administrator' or
               (user_role == 'Operator' and path in ('/test/1', '/control')))
    expected = (200 if method == 'GET' else 302) if allowed else 403
    assert response.status_code == expected
    if not allowed:
        assert modbus[1] == []


@pytest.mark.parametrize('user_role', ['Operator', 'Viewer'])
def test_non_admin_cannot_bulk_delete(web, user_role):
    module, client = web
    inv = seed(module)
    role(module, user_role)
    assert post(client, '/control', {'action': 'delete', 'selected_ids': str(inv.id)}).status_code == 403
    assert module.db.session.get(module.Inverter, inv.id) is not None


@pytest.mark.parametrize('path', ['/login', '/logout', '/add', '/edit/1', '/delete/1', '/test/1', '/control'])
@pytest.mark.parametrize('csrf_value', [None, 'forged'])
def test_missing_or_forged_csrf_never_changes_state(web, modbus, path, csrf_value):
    module, client = web
    seed(module)
    data = {'action': 'on', 'selected_ids': '1', 'username': 'admin', 'password': 'test-password-long'}
    if csrf_value is not None:
        data['csrf_token'] = csrf_value
    assert client.post(path, data=data).status_code == 400
    assert module.Inverter.query.count() == 1
    assert modbus[1] == []


def test_csrf_from_another_session_is_rejected(web):
    module, client = web
    other = module.app.test_client()
    foreign = token(other)
    assert client.post('/logout', data={'csrf_token': foreign}).status_code == 400


def test_login_hash_session_rotation_logout(web):
    module, _ = web
    client = module.app.test_client()
    old_token = token(client)
    with client.session_transaction() as sess:
        sess['untrusted_marker'] = 'removed-on-login'
    response = client.post('/login', data={'csrf_token': old_token, 'username': 'ADMIN', 'password': 'test-password-long'})
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert 'untrusted_marker' not in sess
        assert 'csrf_token' not in sess
        assert sess['user_id'] == 1
        assert sess.permanent
    assert client.post('/logout', data={'csrf_token': old_token}).status_code == 400
    assert client.get('/').status_code == 200
    assert post(client, '/logout').status_code == 302
    assert client.get('/').status_code == 401
    user = module.db.session.get(module.User, 1)
    assert user.password_hash.startswith('scrypt:')
    assert 'test-password-long' not in user.password_hash
    assert user.check_password('test-password-long')
    assert not user.check_password('incorrect')


@pytest.mark.parametrize('mutation', ['inactive', 'password', 'delete', 'role'])
def test_session_checks_current_account_each_request(web, mutation):
    module, client = web
    user = module.db.session.get(module.User, 1)
    if mutation == 'inactive': user.active = False
    if mutation == 'password': user.set_password('replacement-password')
    if mutation == 'delete': module.db.session.delete(user)
    if mutation == 'role': user.role = 'Viewer'
    module.db.session.commit()
    assert client.get('/add').status_code == (403 if mutation == 'role' else 401)


def test_account_rate_limit_logs_no_credentials(web, caplog):
    module, _ = web
    client = module.app.test_client()
    with caplog.at_level(logging.WARNING, logger='ingecon.auth'):
        for i in range(5):
            assert post(client, '/login', {'username': 'admin', 'password': 'wrong-secret'}).status_code == 401
        response = post(client, '/login', {'username': 'admin', 'password': 'test-password-long'})
    assert response.status_code == 429
    assert 0 < int(response.headers['Retry-After']) <= 300
    assert 'AUTH_FAILURE ip=127.0.0.1 reason=invalid_credentials' in caplog.text
    assert 'reason=rate_limited' in caplog.text
    assert 'wrong-secret' not in caplog.text and 'test-password-long' not in caplog.text
    assert 'admin' not in caplog.text
    assert module.db.session.scalar(module.db.select(LoginThrottle.attempts).where(
        LoginThrottle.attempts == 6)) == 6


def test_ip_limit_ignores_forwarded_header(web, caplog):
    module, _ = web
    module.app.config['LOGIN_IP_LIMIT'] = 2
    client = module.app.test_client()
    for username in ('ghost1', 'ghost2'):
        assert post(client, '/login', {'username': username, 'password': 'bad'},
                    headers={'X-Forwarded-For': '198.51.100.7'}).status_code == 401
    assert post(client, '/login', {'username': 'ghost3', 'password': 'bad'},
                headers={'X-Forwarded-For': '198.51.100.8'}).status_code == 429
    assert 'ip=127.0.0.1' in caplog.text
    assert '198.51.100.' not in caplog.text


def test_rate_limit_expires_without_sleep(web, monkeypatch):
    import security
    module, _ = web
    module.app.config['LOGIN_ACCOUNT_LIMIT'] = 1
    clock = [1000]
    monkeypatch.setattr(security.time, 'time', lambda: clock[0])
    client = module.app.test_client()
    assert post(client, '/login', {'username': 'ghost', 'password': 'bad'}).status_code == 401
    assert post(client, '/login', {'username': 'ghost', 'password': 'bad'}).status_code == 429
    clock[0] += 301
    assert post(client, '/login', {'username': 'ghost', 'password': 'bad'}).status_code == 401


@pytest.mark.parametrize('username,password', [('missing','bad'), ('admin','bad'), ('admin','x'*1025)])
def test_failed_login_is_generic(web, username, password):
    module, _ = web
    client = module.app.test_client()
    response = post(client, '/login', {'username':username,'password':password})
    assert response.status_code == 401
    assert 'Неправильні облікові дані.' in response.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_inverter_password_is_not_in_html_and_blank_preserves_it(web):
    module, client = web
    inv = seed(module)
    for path in ('/', '/edit/1', '/add'):
        assert 'device-secret-sentinel' not in client.get(path).get_data(as_text=True)
    post(client, '/edit/1', {'name':'Mock','group':'A','ip':'192.0.2.1','login':'device-user','password':''})
    assert module.db.session.get(module.Inverter, inv.id).password == 'device-secret-sentinel'


@pytest.mark.parametrize('bad_data', [
    {'action':'unknown','selected_ids':'1'},
    {'action':'limit','selected_ids':'1'},
    *[{'action':'limit','selected_ids':'1','limit_value':value}
      for value in ('bad','0','101','-1','1.5',' 50','50 ','+50','01','５０')],
    {'action':'on','selected_ids':'0'}, {'action':'on','selected_ids':'999'},
    {'action':'on','selected_ids':['1','999']}, {'action':'on','selected_ids':['1','1']},
    {'action':'on','selected_ids':'1 OR 1=1'}, {'action':'on','selected_ids':['1']*101},
    {'action':['on','off'],'selected_ids':'1'},
    {'action':'limit','selected_ids':'1','limit_value':['20','30']},
])
def test_invalid_commands_are_atomic_and_never_connect(web, modbus, bad_data):
    module, client = web
    seed(module)
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    assert post(client, '/control', bad_data).status_code == 400
    assert modbus[1] == []


@pytest.mark.parametrize('action', ['on','off','limit','limit_max'])
def test_write_switch_defaults_to_denied(web, modbus, action):
    module, client = web
    seed(module)
    assert module.app.config['MODBUS_WRITES_ENABLED'] is False
    assert post(client, '/control', {'action':action,'selected_ids':'1','limit_value':'25'}).status_code == 403
    assert modbus[1] == []


@pytest.mark.parametrize('failure', ['offline','write_error','read_error'])
def test_command_failure_is_not_reported_as_success(web, modbus, monkeypatch, failure):
    import modbus_utils
    module, client = web
    seed(module)
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    fake = FakeModbusTcpClient('192.0.2.1')
    if failure == 'offline': fake.online = False
    if failure == 'write_error': fake.write_error = True
    if failure == 'read_error': fake.read_error = True
    monkeypatch.setattr(modbus_utils, 'ModbusTcpClient', lambda *a,**k: fake)
    assert post(client, '/control', {'action':'on','selected_ids':'1'}).status_code == 302
    with client.session_transaction() as sess:
        messages = sess['_flashes']
        assert all(category == 'danger' for category,_ in messages)
        assert all('виконана' not in text and 'запущено' not in text for _,text in messages)


def test_verified_register_write_does_not_claim_physical_start(web, modbus):
    module, client = web
    seed(module)
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    post(client, '/control', {'action':'on','selected_ids':'1'})
    with client.session_transaction() as sess:
        assert sess['_flashes'][0][0] == 'info'
        assert 'фізичний стан інвертора не підтверджено' in sess['_flashes'][0][1]


def test_cookie_and_response_security(web):
    _, client = web
    response = client.get('/login')
    cookie = response.headers['Set-Cookie']
    assert all(x in cookie for x in ('Secure','HttpOnly','SameSite=Lax'))
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


def test_startup_requires_external_secret_and_disallows_debug(web, monkeypatch):
    module, _ = web
    monkeypatch.delenv('INGECON_SECRET_KEY')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        module.create_app()
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        module.create_app({'SECRET_KEY':'short'})
    with pytest.raises(RuntimeError, match='Debug'):
        module.create_app({'SECRET_KEY':'x'*32,'DEBUG':True})


def test_cli_user_creation_has_no_default_credentials(web):
    module, _ = web
    runner = module.app.test_cli_runner()
    result = runner.invoke(args=['create-user','new-user','--role','Viewer'],
                           input='new-password-long\nnew-password-long\n')
    assert result.exit_code == 0
    user = module.db.session.scalar(module.db.select(module.User).where(module.User.username=='new-user'))
    assert user.role == 'Viewer' and user.password_hash.startswith('scrypt:')
    assert runner.invoke(args=['create-user','new-user'],input='new-password-long\nnew-password-long\n').exit_code != 0
    assert runner.invoke(args=['create-user','another'],input='short\nshort\n').exit_code != 0


def test_get_cannot_execute_control_or_logout(web, modbus):
    _, client = web
    for path in ('/control','/test/1','/delete/1','/logout'):
        assert client.get(path).status_code == 405
    assert modbus[1] == []

@pytest.mark.parametrize('user_role,expected', [('Administrator',302),('Operator',302),('Viewer',403)])
def test_command_roles_with_mock_only(web, modbus, user_role, expected):
    module, client = web
    seed(module)
    role(module, user_role)
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    assert post(client, '/control', {'action':'on','selected_ids':'1'}).status_code == expected
    assert bool(modbus[1]) == (expected == 302)


def test_logout_revokes_copied_cookie(web):
    module, client = web
    stolen = client.get_cookie('session').value
    other = module.app.test_client()
    other.set_cookie('session', stolen)
    assert other.get('/').status_code == 200
    assert post(client, '/logout').status_code == 302
    assert other.get('/').status_code == 401


def test_signed_session_with_wrong_version_is_rejected(web):
    module, client = web
    with client.session_transaction() as sess:
        sess['session_version'] = 999
    assert client.get('/').status_code == 401


def test_https_csrf_requires_same_origin_referrer(web):
    _, client = web
    csrf_token = token(client)
    response = client.post('/logout', base_url='https://localhost',
                           data={'csrf_token': csrf_token}, headers={'Referer':'https://attacker.example/'})
    assert response.status_code == 400
    response = client.post('/logout', base_url='https://localhost',
                           data={'csrf_token': csrf_token}, headers={'Referer':'https://localhost/'})
    assert response.status_code == 302


def test_throttle_survives_new_app_instance(web, monkeypatch, tmp_path):
    # Separate synthetic DB only. Never touches the working database.
    from app import create_app
    from security import reserve_login_attempt
    from models import db
    config = {'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "throttle.sqlite"}',
              'SECRET_KEY': 'test-only-external-secret-32-characters', 'LOGIN_ACCOUNT_LIMIT': 1}
    first = create_app(config)
    with first.app_context():
        db.create_all()
        with first.test_request_context('/login', environ_base={'REMOTE_ADDR':'192.0.2.10'}):
            assert reserve_login_attempt('persistent-account') == 0
    second = create_app(config)
    with second.app_context(), second.test_request_context('/login', environ_base={'REMOTE_ADDR':'192.0.2.10'}):
        assert reserve_login_attempt('persistent-account') > 0


def test_tampered_session_is_rejected(web):
    module, client = web
    cookie = client.get_cookie('session').value
    other = module.app.test_client()
    other.set_cookie('session', cookie + 'tampered')
    assert other.get('/').status_code == 401


def test_authenticated_session_expires(web, monkeypatch):
    import time
    module, _ = web
    clock = [int(time.time())]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    client = module.app.test_client()
    assert post(client, '/login', {'username':'admin','password':'test-password-long'}).status_code == 302
    assert client.get('/').status_code == 200
    clock[0] += 1801
    assert client.get('/').status_code == 401


@pytest.mark.parametrize('flag', ['1','true','yes'])
def test_flask_cli_debug_environment_is_rejected(web, monkeypatch, flag):
    module, _ = web
    monkeypatch.setenv('FLASK_DEBUG', flag)
    with pytest.raises(RuntimeError, match='Debug'):
        module.create_app()
