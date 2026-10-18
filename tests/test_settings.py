import re

import pytest

from models import User, db
from tests.helpers import post


def navigation(html, label):
    return re.search(r'<nav[^>]*aria-label="' + label + r'"[^>]*>(.*?)</nav>', html, re.S).group(1)


@pytest.mark.parametrize('role', ['Administrator', 'Operator', 'Viewer'])
def test_settings_entry_and_tabs_follow_role(web, role):
    _, client = web
    db.session.get(User, 1).role = role
    db.session.commit()
    response = client.get('/settings')
    assert response.status_code == 302 and response.location.endswith('/settings/telegram')
    response = client.get('/settings', follow_redirects=True)
    assert response.status_code == 200
    main = navigation(response.text, 'Основна навігація')
    assert 'href="/settings" aria-current="page"' in main
    assert 'Telegram' not in main and 'Користувачі' not in main
    tabs = navigation(response.text, 'Вкладки налаштувань')
    assert 'href="/settings/telegram" aria-current="page"' in tabs
    assert ('href="/settings/users"' in tabs) == (role == 'Administrator')
    assert '<h1>Налаштування</h1>' in response.text
    assert client.get('/settings/users').status_code == (200 if role == 'Administrator' else 403)


@pytest.mark.parametrize('path', ['/settings', '/settings/telegram', '/settings/users',
                                  '/settings/users/add', '/settings/users/1/delete'])
def test_settings_require_login(web, path):
    module, _ = web
    assert module.app.test_client().get(path).status_code == 401


def test_user_forms_stay_in_settings_and_keep_csrf(web):
    _, client = web
    response = client.get('/settings/users')
    tabs = navigation(response.text, 'Вкладки налаштувань')
    assert 'href="/settings/users" aria-current="page"' in tabs
    assert 'href="/settings/users/add"' in response.text
    for path in ('/settings/telegram', '/settings/users/add', '/settings/users/1/delete'):
        assert client.post(path, data={}).status_code == 400
    response = post(client, '/settings/users/add', dict(username='new-viewer', role='Viewer',
        password='test-only-password-long', password_confirmation='test-only-password-long'))
    assert response.status_code == 302 and response.location.endswith('/settings/users')
    target = db.session.scalar(db.select(User).where(User.username == 'new-viewer'))
    path = f'/settings/users/{target.id}/delete'
    response = client.get(path)
    assert 'href="/settings/users" aria-current="page"' in navigation(response.text, 'Вкладки налаштувань')
    signed = re.search(r'name="confirmation_token" value="([^"]+)"', response.text).group(1)
    response = post(client, path, dict(confirmation_token=signed, username_confirmation='new-viewer'))
    assert response.status_code == 302 and response.location.endswith('/settings/users')
    assert User.query.count() == 1


@pytest.mark.parametrize('role', ['Operator', 'Viewer'])
@pytest.mark.parametrize('path', ['/settings/users/add', '/settings/users/1/delete'])
def test_non_admin_cannot_post_user_settings(web, role, path):
    _, client = web
    db.session.get(User, 1).role = role
    db.session.commit()
    assert post(client, path, {}).status_code == 403
    assert User.query.count() == 1


@pytest.mark.parametrize('path', ['/telegram', '/users'])
def test_old_bookmarks_open_same_settings_tabs(web, path):
    _, client = web
    response = client.get(path)
    assert response.status_code == 200
    assert 'Вкладки налаштувань' in response.text
    main = navigation(response.text, 'Основна навігація')
    assert 'href="/settings" aria-current="page"' in main
    assert 'Telegram' not in main and 'Користувачі' not in main
