"""Production release tools exercise synthetic SQLite and mock OS/Git only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

from deploy import discovery_release as release

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a'*40
PREVIOUS = 'b'*40


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO sentinel VALUES (1,?)', ('keep existing user and links',))


def test_backup_exercises_restore_without_changing_source(tmp_path):
    source = tmp_path/'production.sqlite'
    destination = tmp_path/'restore.sqlite'
    database(source)
    before = source.read_bytes()
    release.check_copy(source, destination)
    assert source.read_bytes() == before
    with sqlite3.connect(destination) as connection:
        assert connection.execute('SELECT value FROM sentinel').fetchone()[0] == 'keep existing user and links'
    assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): release.check_copy(source, destination)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    root = tmp_path/'project'
    production = root/'instance/production'
    database(production/'ingecon.sqlite')
    (production/'portal.env').write_text('SYNTHETIC_ENV=true\n')
    (production/'web-secret.key').write_text('synthetic-secret-only')
    token = root/'instance/telegram/bot.key'
    token.parent.mkdir(); token.write_text('synthetic-token-only')
    def git(*args, **kwargs):
        if args == ('status','--porcelain'): return ''
        if args == ('rev-parse','HEAD'): return SHA
        if args[0] == 'ls-remote': return SHA+'\trefs/heads/feature/modernization'
        if args[0] == 'rev-parse': return PREVIOUS
        raise AssertionError(args)
    monkeypatch.setattr(release, 'git', git)
    from types import SimpleNamespace
    monkeypatch.setattr(release.shutil, 'disk_usage', lambda _: SimpleNamespace(free=2*1024**3))
    calls = []
    def run(argv, **kwargs):
        assert argv[:3] == ['git','archive','--format=tar']
        calls.append(argv)
        output = Path(argv[3].split('=',1)[1])
        contents = root/'old.py'; contents.write_text('previous committed code\n')
        with tarfile.open(output, 'w') as tar: tar.add(contents, arcname='old.py')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(release.subprocess, 'run', run)
    return root, calls


def test_prepare_private_backups_and_source_snapshot_only(snapshot):
    root, calls = snapshot
    original = (root/'instance/production/ingecon.sqlite').read_bytes()
    manifest_path = release.prepare(SHA, PREVIOUS, root)
    manifest = json.loads(manifest_path.read_text())
    assert manifest['expected_sha'] == SHA and manifest['previous_sha'] == PREVIOUS
    assert (Path(manifest['previous_source'])/'old.py').read_text() == 'previous committed code\n'
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert (root/'instance/production/ingecon.sqlite').read_bytes() == original
    backup = Path(manifest['backup_dir'])
    assert (backup/'restore-verified.sqlite').is_file()
    assert (backup/'instance__telegram__bot.key').read_text() == 'synthetic-token-only'
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in backup.iterdir())
    assert len(calls) == 1


@pytest.mark.parametrize('reason', ['dirty','unpublished','wrong_sha','no_space'])
def test_prepare_refuses_before_production_changes(snapshot, monkeypatch, reason):
    root, _ = snapshot
    original_git = release.git
    def git(*args, **kwargs):
        if args == ('status','--porcelain') and reason == 'dirty': return ' M app.py'
        if args[0] == 'ls-remote' and reason == 'unpublished': return PREVIOUS+'\trefs/heads/feature/modernization'
        return original_git(*args, **kwargs)
    monkeypatch.setattr(release, 'git', git)
    if reason == 'no_space':
        from types import SimpleNamespace
        monkeypatch.setattr(release.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    with pytest.raises(ValueError):
        release.prepare(PREVIOUS if reason == 'wrong_sha' else SHA, PREVIOUS, root)
    assert not (root/'instance/releases').exists()


def test_protected_config_never_silently_replaced(tmp_path, monkeypatch):
    path = tmp_path/'config.key'; path.write_text('synthetic-original')
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    manifest = dict(protected={'config.key':hashlib.sha256(path.read_bytes()).hexdigest()})
    release.verify_protected(manifest)
    path.write_text('changed outside deploy')
    with pytest.raises(ValueError): release.verify_protected(manifest)
    assert path.read_text() == 'changed outside deploy'


def test_units_nonroot_single_worker_and_no_schedules():
    for service, module in (('discovery','discovery_worker'),('monitor','monitor')):
        text = (ROOT/f'deploy/systemd/ingecon-{service}.service').read_text()
        assert text.count('ExecStart=') == 1 and f'-m {module}' in text
        assert 'User=codex' in text and 'Group=codex' in text
        assert 'AF_NETLINK' in text and 'NoNewPrivileges=yes' in text
        assert 'CAP_NET_ADMIN' not in text and 'CAP_NET_RAW' not in text
        assert 'OnCalendar=' not in text and 'production/ingecon.sqlite' in text
    assert 'AF_NETLINK' in (ROOT/'deploy/systemd/ingecon-web.service').read_text()


def test_activation_traps_failures_and_preserves_working_data():
    text = (ROOT/'deploy/activate_discovery.sh').read_text()
    assert 'trap rollback ERR INT TERM' in text
    assert text.index('-m discovery_schema') < text.index('systemctl restart ingecon-web\n# Readiness')
    assert 'git reset' not in text and '--force' not in text
    assert 'systemctl restart ingecon-telegram' not in text and 'systemctl restart nginx' not in text
    assert 'production DB and new operational data were not overwritten' in text
    assert 'restore' not in text.split('app .venv/bin/python -m discovery_schema')[1].split('for name')[0]
    assert '-m discovery_release' not in text or 'discovery_release check' in text


def test_auth_checker_calls_only_safe_http_routes():
    text = (ROOT/'deploy/discovery_release.py').read_text()
    assert 'getUpdates' not in text and 'sendMessage' not in text
    assert 'confirmation_token' not in text
    assert 'session_version=user.session_version' in text
    assert "('/add?mode=manual'" in text
    assert 'urllib.request.Request' in text


def test_live_checker_with_mock_http_telegram_and_synthetic_database(tmp_path, monkeypatch):
    """Exercise real signed authorization through Flask, with no HTTP sockets."""
    import app
    from models import db, User, MonitorHeartbeat
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from deploy import check_portal
    import time
    root = tmp_path/'live'
    production = root/'instance/production'; production.mkdir(parents=True)
    path = production/'ingecon.sqlite'
    secret = production/'web-secret.key'; secret.write_text('synthetic-release-key-'*3); secret.chmod(0o600)
    engine = create_engine('sqlite:///'+str(path))
    db.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(User(username='stewe', role='Administrator', password_hash='synthetic-only', session_version=17))
        session.add(MonitorHeartbeat(id=1, owner='synthetic', started_at=time.time(), last_seen=time.time(), interval_seconds=30, stopped=False))
    engine.dispose()
    application = app.create_app(dict(SECRET_KEY=secret.read_text(),SQLALCHEMY_DATABASE_URI='sqlite:///'+str(path),SESSION_COOKIE_SECURE=False,TESTING=True))
    client = application.test_client()
    requests, external = [], []
    class Response:
        def __init__(self, response): self.response=response; self.status=response.status_code
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return self.response.data
    class Opener:
        def open(self, query, timeout):
            from urllib.parse import urlsplit
            url = urlsplit(query.full_url)
            cookie = query.get_header('Cookie').removeprefix('session=')
            client.set_cookie('session', cookie)
            requests.append(url.path)
            response = client.get(url.path+('?' +url.query if url.query else ''))
            return Response(response)
    monkeypatch.setattr(release, 'ROOT', root)
    monkeypatch.setattr(release, 'DATABASE', path)
    monkeypatch.setattr(release.urllib.request, 'build_opener', lambda *a: Opener())
    monkeypatch.setattr(check_portal, 'http_check', lambda base: external.append(base))
    monkeypatch.setattr(check_portal, 'telegram_check', lambda: external.append('mock-getMe'))
    before = path.read_bytes()
    release.check()
    assert external == ['http://127.0.0.1:5000','http://127.0.0.1:80','mock-getMe']
    assert requests.count('/add') == 4 and requests.count('/api/statuses') == 2
    assert path.read_bytes() == before
    with application.app_context(): db.engine.dispose()
