import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from werkzeug.datastructures import MultiDict

from models import (CommandAudit, StateEvent, TelegramCommand, TelegramDelivery,
                    TelegramLink, TelegramLinkCode, User, db)
from telegram_store import TelegramAccess, TelegramStore
from tests.helpers import post, token


PASSWORD = 'new-test-password-only'


def add_user(username='target', role='Viewer', active=True):
    user = User(username=username, role=role, active=active)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


def data():
    return dict(username='new.user', role='Viewer', password=PASSWORD,
                password_confirmation=PASSWORD)


def confirmation(client, user_id):
    response = client.get(f'/users/{user_id}/delete')
    assert response.status_code == 200
    return re.search(r'name="confirmation_token" value="([^"]+)"', response.text).group(1)


def remove(client, user, **changes):
    submitted = dict(confirmation_token=confirmation(client, user.id), username_confirmation=user.username)
    submitted.update(changes)
    return post(client, f'/users/{user.id}/delete', submitted)


@pytest.mark.parametrize('role,expected', [(None, 401), ('Viewer', 403), ('Operator', 403)])
@pytest.mark.parametrize('path,method', [('/users','GET'), ('/users/add','GET'),
    ('/users/add','POST'), ('/users/2/delete','GET'), ('/users/2/delete','POST')])
def test_admin_only(web, role, expected, path, method):
    module, client = web
    add_user()
    if role is None:
        client = module.app.test_client()
    else:
        db.session.get(User, 1).role = role
        db.session.commit()
    response = client.get(path) if method == 'GET' else post(client, path, data())
    assert response.status_code == expected
    assert User.query.count() == 2
    assert CommandAudit.query.count() == 0


@pytest.mark.parametrize('role', ['Administrator', 'Operator', 'Viewer'])
def test_create_user_hash_role_login_and_audit(web, role):
    module, client = web
    submitted = data()
    submitted.update(username='  New.User  ', role=role)
    response = post(client, '/users/add', submitted)
    assert response.status_code == 302 and response.location.endswith('/users')
    user = db.session.scalar(db.select(User).where(User.username == 'new.user'))
    assert user.active and user.role == role and user.check_password(PASSWORD)
    assert user.password_hash.startswith('scrypt:') and PASSWORD not in user.password_hash
    record = db.session.scalar(db.select(CommandAudit))
    assert (record.source, record.action, record.username, record.result) == ('web','user_create','admin','success')
    assert record.targets == [dict(id=user.id, username='new.user', role=role)]
    assert PASSWORD not in str(record.details) + str(record.targets)
    other = module.app.test_client()
    assert post(other, '/login', dict(username='NEW.USER', password=PASSWORD)).status_code == 302
    assert other.get('/').status_code == 200
    assert other.get('/users').status_code == (200 if role == 'Administrator' else 403)
    html = client.get('/users').text + client.get('/users/add').text
    assert PASSWORD not in html and user.password_hash not in html


@pytest.mark.parametrize('changes', [dict(username=''), dict(username='аdmin'),
    dict(username='a b'), dict(username='a'*65), dict(role='root'), dict(role=''),
    dict(password='short', password_confirmation='short'),
    dict(password='x'*1025, password_confirmation='x'*1025),
    dict(password_confirmation='different-test-password')])
def test_create_validation(web, changes):
    _, client = web
    submitted = data()
    submitted.update(changes)
    response = post(client, '/users/add', submitted)
    assert response.status_code == 400
    assert User.query.count() == 1 and CommandAudit.query.count() == 0
    assert PASSWORD not in response.text


def test_duplicate_username_and_duplicate_fields(web):
    _, client = web
    submitted = data()
    submitted['username'] = 'ADMIN'
    assert post(client, '/users/add', submitted).status_code == 400
    for name in submitted:
        polluted = MultiDict(data())
        polluted.add(name, 'Administrator')
        assert post(client, '/users/add', polluted).status_code == 400
    assert User.query.count() == 1
    assert CommandAudit.query.count() == 0


@pytest.mark.parametrize('path', ['/users/add', '/users/2/delete'])
@pytest.mark.parametrize('csrf', [None, 'invalid'])
def test_csrf(web, path, csrf):
    _, client = web
    add_user()
    submitted = data()
    if csrf:
        submitted['csrf_token'] = csrf
    assert client.post(path, data=submitted).status_code == 400
    assert User.query.count() == 2


def test_self_deletion_forbidden_even_with_second_admin(web):
    _, client = web
    assert client.get('/users/1/delete').status_code == 400
    add_user(role='Administrator')
    assert client.get('/users/1/delete').status_code == 400
    assert post(client, '/users/1/delete', dict(confirmation_token='forged', username_confirmation='admin')).status_code == 400
    assert User.query.count() == 2


@pytest.mark.parametrize('role,active', [('Viewer',True), ('Operator',True),
    ('Administrator',True), ('Administrator',False)])
def test_delete_preserves_admin_and_revokes_web_cookie(web, role, active):
    module, client = web
    target = add_user(role=role, active=active)
    uid = target.id
    other = module.app.test_client()
    with other.session_transaction() as session:
        session['user_id'], session['session_version'] = uid, target.session_version
    assert remove(client, target).status_code == 302
    assert db.session.get(User, uid) is None
    assert db.session.get(User, 1).active
    assert other.get('/').status_code == 401
    assert client.get('/users').status_code == 200
    record = db.session.scalar(db.select(CommandAudit).where(CommandAudit.action == 'user_delete'))
    assert record.targets[0]['id'] == uid and record.user_id == 1


def test_delete_revokes_only_target_telegram_access_preserving_history(web):
    _, client = web
    target = add_user()
    other = add_user('other')
    store = TelegramStore(db.engine)
    store.redeem(1001, store.issue_code(target.id))
    store.redeem(1002, store.issue_code(other.id))
    db.session.add(TelegramLinkCode(user_id=target.id, digest='a'*64,
        user_version=target.session_version, expires_at=9999999999))
    event = StateEvent(inverter_id=1, inverter_name='Mock', target_key='b'*64,
        previous_state='Online', state='Offline', occurred_at=123)
    db.session.add(event)
    db.session.flush()
    for telegram_id in (1001, 1002):
        db.session.add(TelegramDelivery(event_id=event.id, telegram_id=telegram_id, state='pending'))
    for user, tid, intent in ((target, 1001, 'target-intent'), (other, 1002, 'other-intent')):
        db.session.add(TelegramCommand(id=intent, telegram_id=tid, user_id=user.id,
            payload={}, expires_at=9999999999, state='awaiting'))
        db.session.add(CommandAudit(request_id='tg:'+intent, source='telegram', telegram_id=tid,
            user_id=user.id, username=user.username, occurred_at=123, action='on', targets=[],
            result='awaiting', details=[]))
    db.session.commit()
    assert remove(client, target).status_code == 302
    assert db.session.get(TelegramLink, 1001) is None
    assert db.session.get(TelegramLinkCode, target.id) is None
    assert db.session.get(TelegramCommand, 'target-intent').state == 'cancelled'
    assert db.session.get(TelegramCommand, 'other-intent').state == 'awaiting'
    assert db.session.scalar(db.select(TelegramDelivery.state).where(TelegramDelivery.telegram_id == 1001)) == 'skipped'
    assert db.session.scalar(db.select(TelegramDelivery.state).where(TelegramDelivery.telegram_id == 1002)) == 'pending'
    assert StateEvent.query.count() == 1 and CommandAudit.query.count() == 3
    assert store.identity(1002) == ('other', 'Viewer')
    with pytest.raises(TelegramAccess):
        store.identity(1001)


@pytest.mark.parametrize('mutation', ['username', 'role', 'password', 'active', 'actor_version'])
def test_confirmation_invalid_after_changes(web, mutation):
    _, client = web
    target = add_user()
    signed = confirmation(client, target.id)
    if mutation == 'username': target.username = 'renamed'
    elif mutation == 'role': target.role = 'Operator'
    elif mutation == 'password': target.set_password('another-test-password')
    elif mutation == 'active': target.active = False
    else:
        admin = db.session.get(User, 1)
        admin.session_version += 1
        with client.session_transaction() as session:
            session['session_version'] = admin.session_version
    db.session.commit()
    response = post(client, f'/users/{target.id}/delete', dict(
        confirmation_token=signed, username_confirmation=target.username))
    assert response.status_code == 400
    assert User.query.count() == 2 and CommandAudit.query.count() == 0


@pytest.mark.parametrize('change', ['forged', 'expired', 'wrong_name', 'other_target', 'duplicate'])
def test_invalid_delete_confirmation(web, monkeypatch, change):
    _, client = web
    target = add_user()
    signed = confirmation(client, target.id)
    submitted = MultiDict(dict(confirmation_token=signed, username_confirmation=target.username))
    if change == 'forged': submitted['confirmation_token'] = 'forged'
    elif change == 'expired':
        # Test actual signed timestamp expiry without sleeping.
        monkeypatch.setattr('itsdangerous.timed.TimestampSigner.get_timestamp', lambda self: int(time.time()) + 121)
    elif change == 'wrong_name': submitted['username_confirmation'] = 'wrong'
    elif change == 'other_target':
        other = add_user('another')
        submitted['confirmation_token'] = confirmation(client, other.id)
    else: submitted.add('confirmation_token', signed)
    assert post(client, f'/users/{target.id}/delete', submitted).status_code == 400
    assert db.session.get(User, target.id) is not None


def test_deleted_id_reuse_does_not_restore_old_cookie_or_confirmation(web):
    module, client = web
    target = add_user()
    uid, old_version = target.id, target.session_version
    signed = confirmation(client, uid)
    old_client = module.app.test_client()
    with old_client.session_transaction() as session:
        session['user_id'], session['session_version'] = uid, old_version
    assert remove(client, target).status_code == 302
    # SQLite reuses the highest deleted row ID, even with the same username.
    assert post(client, '/users/add', dict(data(), username='target')).status_code == 302
    replacement = db.session.get(User, uid)
    assert replacement is not None and replacement.session_version != old_version
    assert old_client.get('/').status_code == 401
    assert post(client, f'/users/{uid}/delete', dict(confirmation_token=signed,
        username_confirmation='target')).status_code == 400
    assert db.session.get(User, uid) is not None


def test_atomic_failure_rolls_back_revoke_and_deletion(web, monkeypatch):
    _, client = web
    target = add_user()
    store = TelegramStore(db.engine)
    store.redeem(1001, store.issue_code(target.id))
    original = TelegramStore.revoke_user
    def fail(session, uid):
        original(session, uid)
        raise OperationalError('synthetic', {}, Exception('synthetic'))
    monkeypatch.setattr(TelegramStore, 'revoke_user', fail)
    assert remove(client, target).status_code == 503
    assert User.query.count() == 2 and CommandAudit.query.count() == 0
    assert store.identity(1001) == ('target', 'Viewer')


def test_lock_rechecks_stale_administrator(web, monkeypatch):
    import users_web
    _, client = web
    target = add_user()
    original = users_web.lock_administrator
    def demote_then_lock():
        # Simulate a concurrent committed demotion after request authentication.
        db.session.execute(update(User).where(User.id == 1).values(role='Viewer'),
                           execution_options={'synchronize_session': False})
        original()
    monkeypatch.setattr(users_web, 'lock_administrator', demote_then_lock)
    assert remove(client, target).status_code == 403
    assert User.query.count() == 2


def test_list_navigation_pagination_methods_and_no_secret_leaks(web):
    _, client = web
    admin = db.session.get(User, 1)
    html = client.get('/users').text
    assert 'Користувачі порталу' in html and 'href="/settings/users"' in html
    assert '/users/1/delete' not in html and '/settings/users/1/delete' not in html and admin.password_hash not in html
    db.session.add_all(User(username=f'user-{n:02}', role='Viewer', password_hash='test-only') for n in range(51))
    db.session.commit()
    assert 'Далі →' in client.get('/users').text
    assert 'user-50' in client.get('/users?page=2').text
    assert client.get('/users?page=0').status_code == 400
    assert client.get('/users?page=99999999').status_code == 400
    assert 'Користувачів на цій сторінці немає' in client.get('/users?page=3').text
    assert client.get('/users/99999/delete').status_code == 404
    assert post(client, '/users').status_code == 405
    admin.role = 'Viewer'
    db.session.commit()
    assert 'href="/settings/users"' not in client.get('/').text


def test_two_admins_cannot_concurrently_delete_each_other(web, tmp_path, monkeypatch):
    import users_web
    module, _ = web
    app = module.create_app(dict(TESTING=True, SECRET_KEY='isolated-test-only-secret-32-characters',
        SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path/'accounts.sqlite')))
    with app.app_context():
        db.create_all()
        alice = add_user('alice', 'Administrator')
        bob = add_user('bob', 'Administrator')
        identities = [(alice.id, alice.session_version, bob.id, 'bob'),
                      (bob.id, bob.session_version, alice.id, 'alice')]
    requests = []
    for uid, version, target_id, name in identities:
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'], session['session_version'] = uid, version
        requests.append((client, target_id, dict(confirmation_token=confirmation(client, target_id),
                         username_confirmation=name, csrf_token=token(client))))
    barrier = Barrier(2)
    original = users_web.lock_administrator
    def synchronized_lock():
        barrier.wait(timeout=10)
        original()
    monkeypatch.setattr(users_web, 'lock_administrator', synchronized_lock)
    def send(item):
        client, target_id, submitted = item
        return client.post(f'/users/{target_id}/delete', data=submitted).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(send, requests))
    assert sorted(codes) == [302, 403]
    with app.app_context():
        assert User.query.count() == 1
        remaining = db.session.scalar(db.select(User))
        assert remaining.active and remaining.role == 'Administrator'
        assert CommandAudit.query.count() == 1
        db.session.remove()
        db.engine.dispose()


def test_shared_navigation_safe_before_workers_load_new_routes(web):
    module, client = web
    module.app.config.pop('SETTINGS_AVAILABLE')
    assert client.get('/').status_code == 200
    assert 'href="/settings"' not in client.get('/').text
    response = client.get('/telegram')
    assert response.status_code == 200
    assert 'href="/settings/users"' not in response.text
