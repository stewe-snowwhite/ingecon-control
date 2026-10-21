"""Explicit additive discovery schema preparation; always backs up SQLite first."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import uuid

from sqlalchemy import create_engine
from models import db, DISCOVERY_TABLES


def prepare(database, backups):
    database, backups = Path(database), Path(backups)
    if not database.is_absolute() or not database.is_file() or database.is_symlink():
        raise ValueError('Потрібна наявна абсолютна SQLite-БД без symlink.')
    if backups.is_symlink():
        raise ValueError('Каталог резервних копій не може бути symlink.')
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = backups / ('before-discovery-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex+'.sqlite')
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = set(db.metadata.tables)-{table.name for table in DISCOVERY_TABLES}
        if not required <= tables or 'demo_metadata' in tables:
            raise ValueError('Невідповідна схема робочої БД; змін не виконано.')
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('Перевірка резервної копії не пройшла; змін не виконано.')
    with backup.open('rb') as stream:
        os.fsync(stream.fileno())
    directory_fd = os.open(backups, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    engine = create_engine('sqlite:///'+str(database), connect_args={'timeout': 5})
    try:
        db.metadata.create_all(engine, tables=DISCOVERY_TABLES, checkfirst=True)
    finally:
        engine.dispose()
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    args = parser.parse_args()
    print('Резервна копія:', prepare(args.database, args.backup_dir))
    print('Додано лише discovery_subnet, discovery_scan, discovery_result. Рядки наявних таблиць збережено.')


if __name__ == '__main__':
    main()
