"""Bounded discovery queue worker, separate from Gunicorn and monitoring."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

import discovery
from models import (DiscoveryScan, DiscoveryResult, DiscoverySubnet, User, CommandAudit,
                    DISCOVERY_TABLES)
from operation_locks import resource_lock

log = logging.getLogger('ingecon.discovery')
ACTIVE = ('queued', 'running', 'cancelling')


def audit(session, user_id, action, result, details):
    session.add(CommandAudit(request_id='scan:'+uuid.uuid4().hex, source='web', user_id=user_id,
        occurred_at=time.time(), action=action, targets=[], result=result, details=details))


def schema_ready(engine):
    return {table.name for table in DISCOVERY_TABLES} <= set(inspect(engine).get_table_names())


class Worker:
    def __init__(self, engine, stop=None):
        self.engine = engine
        self.stop = stop if stop is not None else threading.Event()

    def allowed(self, scan_id):
        with Session(self.engine) as session:
            scan = session.get(DiscoveryScan, scan_id)
            user = session.get(User, scan.user_id)
            if self.stop.is_set() or scan.state == 'cancelling' or not user or not user.active or user.role != 'Administrator' or user.session_version != scan.user_version:
                return False
            for item in scan.subnets:
                subnet = session.get(DiscoverySubnet, item['id'])
                if not subnet or (subnet.cidr, subnet.interface, subnet.mode) != (item['cidr'], item['interface'], item['mode']):
                    return False
            return True

    def run_one(self):
        with resource_lock('discovery-queue'), Session(self.engine) as session, session.begin():
            scan = session.scalar(select(DiscoveryScan).where(DiscoveryScan.state == 'queued').order_by(DiscoveryScan.id).limit(1))
            if scan is None:
                return False
            scan.state = 'running'
            scan.heartbeat = time.time()
            scan_id, scopes, unit = scan.id, list(scan.subnets), scan.device_id
        log.info('SCAN_STARTED id=%s', scan_id)
        error = None
        state = 'failed'
        try:
            targets = {}
            for item in scopes:
                scope = discovery.checked_scope(item['cidr'])
                if (scope.interface, scope.mode) != (item['interface'], item['mode']):
                    raise ValueError('Маршрут або сегмент змінився після погодження.')
                for ip in discovery.host_addresses(scope.cidr, discovery.local_inventory()):
                    targets.setdefault(ip, scope)
            if len(targets) > discovery.MAX_HOSTS:
                raise ValueError('Перевищено ліміт адрес.')
            # Four-address batches bound memory, cancellation and SQLite writes.
            items = list(targets.items())
            with ThreadPoolExecutor(max_workers=discovery.WORKERS, thread_name_prefix='discovery') as executor:
                for start in range(0, len(items), discovery.WORKERS):
                    if not self.allowed(scan_id):
                        state = 'cancelled'
                        break
                    futures = [executor.submit(discovery.probe, ip, scope, unit) for ip, scope in items[start:start+discovery.WORKERS]]
                    results = [future.result() for future in futures]
                    with Session(self.engine) as session, session.begin():
                        row = session.get(DiscoveryScan, scan_id)
                        for result in results:
                            if result:
                                session.add(DiscoveryResult(scan_id=scan_id, **result))
                                row.found += 1
                        row.processed += len(futures)
                        row.heartbeat = time.time()
            if state != 'cancelled':
                state = 'completed'
        except AssertionError:
            raise  # Test network guards must never be swallowed.
        except Exception as exc:
            state = 'failed'
            error = str(exc)[:256] if isinstance(exc, ValueError) else 'Безпечний пошук перервано; перевірте локальні маршрути й доступність worker.'
        finally:
            with Session(self.engine) as session, session.begin():
                row = session.get(DiscoveryScan, scan_id)
                if row.state == 'cancelling' or self.stop.is_set():
                    state = 'cancelled'
                row.state, row.error = state, error
                row.finished_at = row.heartbeat = time.time()
                audit(session, row.user_id, 'discovery', state, dict(scan_id=scan_id, processed=row.processed, found=row.found, error=error))
            log.info('SCAN_FINISHED id=%s state=%s', scan_id, state)
        return True

    def run(self, once=False):
        database = self.engine.url.database
        key = str(Path(database).resolve()) if database and database != ':memory:' else ':memory:'
        with resource_lock('discovery-worker:'+key):
            if not schema_ready(self.engine):
                raise ValueError('Спершу виконайте окрему підготовку схеми з резервною копією.')
            with Session(self.engine) as session, session.begin():
                # We hold the only worker lock: recover scans interrupted by death.
                for row in session.scalars(select(DiscoveryScan).where(DiscoveryScan.state.in_(('running', 'cancelling')))):
                    row.state, row.error, row.finished_at = 'failed', 'Попередній worker завершився до закінчення пошуку.', time.time()
                    audit(session, row.user_id, 'discovery', 'failed', dict(scan_id=row.id, reason='worker_interrupted'))
            while not self.stop.is_set():
                worked = self.run_one()
                if once:
                    return
                if not worked:
                    self.stop.wait(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run', action='store_true', required=True, help='Explicit permission for read-only network discovery')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Не запускайте discovery worker від root.')
    if not args.database.is_absolute() or not args.database.is_file() or args.database.is_symlink():
        parser.error('Потрібна наявна абсолютна SQLite-БД без symlink.')
    engine = create_engine('sqlite:///'+str(args.database), connect_args={'timeout': 5})
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    try:
        Worker(engine, stop).run(once=args.once)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
