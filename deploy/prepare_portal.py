"""Back up the existing DB and create private portal configuration, never deploy.

Run from the repo: .venv/bin/python -m deploy.prepare_portal
Does not stop processes, migrate a DB, or touch /etc, tokens or unrelated runtime files.
"""
import os
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from configuration import read_secret_file

ROOT = Path(__file__).resolve().parents[1]


def private_write(path, content):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)


def prepare(root=ROOT):
    production = root/'instance'/'production'
    database = production/'ingecon.sqlite'
    if database.is_symlink() or not database.is_file():
        raise RuntimeError('An existing regular production database is required.')
    backups = production/'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    info = backups.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Backup directory must be private and owned by the current user.')
    backup = backups/('pre-http-portal-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+'.sqlite')
    private_write(backup, '')
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as source:
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
    with sqlite3.connect(backup.as_uri()+'?mode=ro', uri=True) as saved:
        if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup verification failed; configuration was not prepared.')
        if saved.execute('SELECT count(*) FROM user WHERE username=?', ('stewe',)).fetchone()[0] != 1:
            raise RuntimeError('Expected existing stewe account; no account will be created or modified.')
    key = production/'web-secret.key'
    if not key.exists() and not key.is_symlink():
        value = os.environ.get('INGECON_SECRET_KEY') or secrets.token_urlsafe(48)
        if len(value) < 32 or value != value.strip():
            raise RuntimeError('Existing secret is invalid; it will not be replaced automatically.')
        private_write(key, value+'\n')
    read_secret_file(key)  # Validate an existing key without replacing or printing it.
    env = production/'portal.env'
    if not env.exists() and not env.is_symlink():
        private_write(env, (root/'deploy'/'portal.env.example').read_text())
    info = env.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Portal environment file must be private; it was not overwritten.')
    return backup, key, env


def main():
    backup, key, env = prepare()
    print('Verified backup:', backup)
    print('Persistent private key (value hidden):', key)
    print('Private environment file:', env)
    print('No database rows, services, network rules, Telegram token or unrelated runtime data were changed.')


if __name__ == '__main__':
    main()
