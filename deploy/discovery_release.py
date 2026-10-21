"""Release snapshots and read-only HTTP validation. Never starts a scan or sends writes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from itertools import zip_longest
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path('/opt/ingecon-control')
DATABASE = ROOT/'instance/production/ingecon.sqlite'


def git(*args, root=ROOT):
    return subprocess.run(['git', *args], cwd=root, check=True, text=True,
                          capture_output=True, timeout=30).stdout.strip()


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def check_copy(source, destination):
    """Exercise SQLite restore into a NEW private file, never the working DB."""
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as saved, sqlite3.connect(destination) as restored:
        saved.backup(restored)
        if restored.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Restore verification failed')
        missing = object()
        if any(a != b for a, b in zip_longest(saved.iterdump(), restored.iterdump(), fillvalue=missing)):
            raise ValueError('Restored data differs from the backup')


def prepare(expected, previous, root=ROOT):
    if git('status', '--porcelain', root=root):
        raise ValueError('Commit/push all changes before preparing a production release')
    if git('branch', '--show-current', root=root) != 'main':
        raise ValueError('Production releases must use the main branch')
    if git('rev-parse', 'HEAD', root=root) != expected:
        raise ValueError('HEAD does not match the intended release')
    remote = git('ls-remote', 'origin', 'refs/heads/main', root=root).split()
    if not remote or remote[0] != expected:
        raise ValueError('Exact release is not available on GitHub')
    previous = git('rev-parse', previous+'^{commit}', root=root)
    database = root/'instance/production/ingecon.sqlite'
    if not database.is_file() or database.is_symlink():
        raise ValueError('Existing production database required')
    if shutil.disk_usage(root).free < max(256*1024**2, database.stat().st_size*8):
        raise ValueError('Insufficient space for safe backup and rollback')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    release = root/'instance/releases'/('discovery-'+stamp)
    backup = root/'instance/production/backups'/('discovery-'+stamp)
    release.mkdir(parents=True, mode=0o700)
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(release, 0o700)
    os.chmod(backup, 0o700)
    copy = backup/'production.sqlite'
    fd = os.open(copy, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as source, sqlite3.connect(copy) as destination:
        source.backup(destination)
    check_copy(copy, backup/'restore-verified.sqlite')
    with copy.open('rb') as stream:
        os.fsync(stream.fileno())
    protected = {}
    for relative in ('instance/production/portal.env', 'instance/production/web-secret.key',
                     'instance/telegram/bot.key', 'instance/production/modbus.json'):
        path = root/relative
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError('Unexpected protected configuration path')
            target = backup/relative.replace('/', '__')
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            content = path.read_bytes()
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            protected[relative] = hashlib.sha256(content).hexdigest()
    archive = release/'previous-source.tar'
    subprocess.run(['git', 'archive', '--format=tar', '--output='+str(archive), previous],
                   cwd=root, check=True, timeout=30)
    source_dir = release/'previous-source'
    source_dir.mkdir(mode=0o700)
    with tarfile.open(archive) as tar:
        tar.extractall(source_dir, filter='data')
    manifest = dict(version=1, expected_sha=expected, previous_sha=previous,
        directory=str(release), backup_dir=str(backup), database_backup=str(copy),
        previous_source=str(source_dir), protected=protected, created_at=time.time(),
        running_code_provenance='pre-existing running worker SHA is not instrumented; previous_sha is the verified on-disk source')
    private_json(release/'manifest.json', manifest)
    return release/'manifest.json'


def verify_protected(manifest):
    for relative, digest in manifest['protected'].items():
        if hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() != digest:
            raise ValueError('Protected configuration changed; do not overwrite it')


def check(manifest=None):
    """Real HTTP auth with an in-memory signed session; no password/session DB edits."""
    from deploy.check_portal import http_check, telegram_check
    from configuration import read_secret_file
    from app import create_app
    from models import db, User
    from modbus_safety import writes_locked
    from inverter_controls import enabled as controls_enabled
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from discovery_worker import schema_ready
    if not writes_locked() or controls_enabled():
        raise ValueError('Physical writes must remain blocked')
    secret = read_secret_file(ROOT/'instance/production/web-secret.key')
    application = create_app(dict(SECRET_KEY=secret, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                                   SESSION_COOKIE_SECURE=False))
    engine = create_engine('sqlite:///'+str(DATABASE), connect_args={'timeout': 5})
    try:
        if not schema_ready(engine):
            raise ValueError('Discovery schema is missing')
        with Session(engine) as session:
            user = session.scalar(db.select(User).where(User.role == 'Administrator', User.active.is_(True)))
            if user is None:
                raise ValueError('An existing administrator is required; none created')
            payload = dict(user_id=user.id, session_version=user.session_version)
        cookie = application.session_interface.get_signing_serializer(application).dumps(payload)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for base in ('http://127.0.0.1:5000', 'http://127.0.0.1:80'):
            http_check(base)
            for path, markers in (('/', ('Моніторинг',)), ('/add', ('Автоматичний пошук', 'Додати вручну', 'Погодити CIDR')),
                                  ('/add?mode=manual', ('confirm_unverified', 'name="port"', 'name="device_id"')),
                                  ('/history', ('Історія',)), ('/settings/telegram', ('Telegram',))):
                query = urllib.request.Request(base+path, headers={'Cookie':'session='+cookie})
                with opener.open(query, timeout=15) as response:
                    text = response.read().decode()
                    if response.status != 200 or not all(marker in text for marker in markers):
                        raise ValueError('Authenticated route check failed: '+path)
            query = urllib.request.Request(base+'/api/statuses', headers={'Cookie':'session='+cookie})
            with opener.open(query, timeout=10) as response:
                data = json.load(response)
            if not data['monitor']['alive']:
                raise ValueError('Monitor heartbeat is not current')
            with Session(engine) as session:
                from models import Inverter
                expected = session.scalar(db.select(db.func.count()).select_from(Inverter))
            if data['counts']['total'] != expected:
                raise ValueError('Existing inventory is not fully visible')
        telegram_check()  # getMe only: never starts another bot, polls or sends messages.
        if manifest:
            verify_protected(manifest)
        print('Authenticated HTTP routes, existing inventory, CSRF, heartbeat, Telegram and write lock: OK')
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    snapshot = actions.add_parser('prepare')
    snapshot.add_argument('--expected-sha', required=True)
    snapshot.add_argument('--previous-sha', required=True)
    validate = actions.add_parser('check')
    validate.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run this helper as codex; only activate_discovery.sh runs as root')
    if args.action == 'prepare':
        print(prepare(args.expected_sha, args.previous_sha))
    else:
        check(json.loads(args.manifest.read_text()) if args.manifest else None)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never expose urllib token URLs, cookies, secret values or password hashes.
        raise SystemExit('Release helper failed; no credentials printed. Inspect the local checks.') from None
