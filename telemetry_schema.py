"""Additive 100TL telemetry migration. Verified SQLite backup before any DDL."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import uuid

from deploy.discovery_release import check_copy

COLUMNS = {
    'inverter': {'model': "TEXT NOT NULL DEFAULT 'unknown'"},
    'inverter_status': {key: 'INTEGER' for key in (
        'operating_state_code', 'active_power_raw', 'active_power_w', 'limit_reason_code')},
}


def ready(engine):
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    return all(table in tables and set(columns) <= {c['name'] for c in inspector.get_columns(table)}
               for table, columns in COLUMNS.items())


def prepare(database, backups, confirmed_ids=()):
    database, backups = Path(database), Path(backups)
    if not database.is_absolute() or not database.is_file() or database.is_symlink() or backups.is_symlink():
        raise ValueError('Existing absolute production database and safe backup directory required')
    if any(type(key) is not int or key < 1 for key in confirmed_ids):
        raise ValueError('Confirmed inverter IDs must be positive integers')
    ids = sorted(set(confirmed_ids))
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'inverter', 'inverter_status', 'monitor_config'} <= tables or 'demo_metadata' in tables:
            raise ValueError('Unexpected production schema')
        existing = {r[0] for r in source.execute('SELECT id FROM inverter')}
        if not set(ids) <= existing:
            raise ValueError('A confirmed inverter no longer exists')
        backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = backups / ('before-telemetry-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex+'.sqlite')
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with sqlite3.connect(backup) as saved:
            source.backup(saved)
    check_copy(backup, backup.with_suffix('.restore-verified.sqlite'))
    with backup.open('rb') as stream:
        os.fsync(stream.fileno())
    fd = os.open(backups, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    with sqlite3.connect(database, timeout=10) as connection:
        connection.execute('BEGIN IMMEDIATE')
        # Repeat the identity check inside the DDL transaction.
        if not set(ids) <= {r[0] for r in connection.execute('SELECT id FROM inverter')}:
            raise ValueError('Inventory changed before migration')
        for table, columns in COLUMNS.items():
            existing = {row[1]: row for row in connection.execute('PRAGMA table_info('+table+')')}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                else:
                    allowed = ('TEXT', 'VARCHAR(32)') if name == 'model' else ('INTEGER',)
                    if existing[name][2].upper() not in allowed:
                        raise ValueError('Existing telemetry column has an incompatible type')
        connection.executemany("UPDATE inverter SET model='ingecon_sun_100tl' WHERE id=?", [(key,) for key in ids])
        # An explicitly configured old 100TL profile is already model evidence.
        connection.execute("""UPDATE inverter SET model='ingecon_sun_100tl'
            WHERE model='unknown' AND id IN
            (SELECT inverter_id FROM monitor_config WHERE profile='ingecon_100tl_input')""")
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    parser.add_argument('--confirm-inverter-id', type=int, action='append', default=[])
    args = parser.parse_args()
    print('Verified telemetry backup:', prepare(args.database, args.backup_dir, args.confirm_inverter_id))
    print('Added telemetry columns; existing inventory, credentials and transport preserved.')


if __name__ == '__main__':
    main()
