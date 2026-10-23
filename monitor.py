"""Standalone read-only monitor. No Flask server or system service is started."""
import argparse
import logging
import math
import os
import signal
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from models import Inverter, MonitorConfig, MonitorHeartbeat
from monitoring import NetworkProbes, apply_observation, target_for
from operation_locks import resource_lock
import monitor_schedule

log = logging.getLogger(__name__)


class MonitorWorker:
    def __init__(self, engine, probes=None, interval=30, stop_event=None,
                 clock=time.time, monotonic=time.monotonic):
        if not math.isfinite(interval) or not 1 <= interval <= 86400:
            raise ValueError('Interval must be 1–86400 seconds')
        self.engine = engine
        self.probes = probes or NetworkProbes()
        self.interval = interval
        self.base_interval = interval
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.monotonic = monotonic
        self.owner = uuid.uuid4().hex
        self.started_at = clock()
        self._has_started = False

    def _flush(self, observations, *, duration=None, error=None, stopped=False):
        # Network work is done before entering this short write transaction.
        with Session(self.engine) as session, session.begin():
            heartbeat = session.get(MonitorHeartbeat, 1)
            measured = duration if duration is not None else (heartbeat.cycle_duration_seconds if heartbeat else 0)
            actual_interval = measured+1 if measured >= self.interval else self.interval
            for observation in observations:
                inv = session.get(Inverter, observation.target.inverter_id)
                if inv is None:
                    continue
                config = session.get(MonitorConfig, inv.id)
                if target_for(inv, config).key != observation.target.key:
                    continue  # Target edited while a network request was in flight.
                apply_observation(session, inv, observation, actual_interval*3)
                if config and config.requested_at is not None and config.requested_at <= observation.checked_at:
                    config.requested_at = None
            if heartbeat is None:
                heartbeat = MonitorHeartbeat(id=1, owner=self.owner, started_at=self.started_at,
                                             last_seen=self.clock(), interval_seconds=self.interval,
                                             stopped=False, cycle_duration_seconds=0)
                session.add(heartbeat)
            heartbeat.owner = self.owner
            heartbeat.started_at = self.started_at
            heartbeat.last_seen = self.clock()
            heartbeat.interval_seconds = actual_interval
            heartbeat.last_error = error
            heartbeat.stopped = stopped
            if duration is not None:
                heartbeat.cycle_duration_seconds = duration

    def run_cycle(self):
        self._refresh_interval()
        start = self.monotonic()
        # Release the read transaction before network probes.
        with Session(self.engine) as session:
            targets = [target_for(inv, config) for inv, config in session.execute(
                select(Inverter, MonitorConfig).outerjoin(MonitorConfig, Inverter.id == MonitorConfig.inverter_id)
                .order_by(Inverter.id))]
        if not self._has_started:
            self._flush([])
            self._has_started = True
        pending = []
        last_flush = self.monotonic()
        for target in targets:
            if self.stop_event.is_set():
                break
            pending.append(self.probes.poll(target))
            if self.monotonic()-last_flush >= self.interval:
                self._flush(pending, duration=self.monotonic()-start)
                pending = []
                last_flush = self.monotonic()
        duration = self.monotonic()-start
        self._flush(pending, duration=duration)
        return duration

    def _refresh_interval(self):
        # Read a tiny shared file. Expiry needs no writes, service restart or web session.
        interval = monitor_schedule.current(now=self.clock(), default=self.base_interval)['interval_seconds']
        if interval != self.interval:
            self.interval = interval
            if self._has_started:
                # Publish a longer freshness window before entering a longer sleep.
                self._flush([])

    def _wait_for_next_cycle(self, started, completed):
        while not self.stop_event.is_set():
            self._refresh_interval()
            # Re-read once a second so shorter intervals and 30-minute expiry take
            # effect even while waiting on a much longer configured interval.
            deadline = started+self.interval
            if deadline <= completed:
                deadline = completed+1
            delay = deadline-self.monotonic()
            if delay <= 0:
                return
            self.stop_event.wait(min(1, delay))

    def run(self, once=False):
        database = str(Path(self.engine.url.database).resolve()) if self.engine.url.database != ':memory:' else ':memory:'
        with resource_lock('monitor:'+database):
            self._check_schema()
            last_error = None
            try:
                while not self.stop_event.is_set():
                    start = self.monotonic()
                    try:
                        self.run_cycle()
                        last_error = None
                    except AssertionError:
                        raise  # Fail closed when the test network guard is triggered.
                    except Exception as error:
                        kind = type(error).__name__
                        if kind != last_error:
                            log.error('Monitor cycle failed: %s', kind)
                        last_error = kind
                        try:
                            self._flush([], error='monitor_cycle_error')
                        except SQLAlchemyError:
                            pass  # Missing heartbeat will become stale.
                    if once:
                        break
                    # No catch-up burst or parallel cycles when polling overruns.
                    self._wait_for_next_cycle(start, self.monotonic())
            finally:
                try:
                    self._flush([], stopped=True, error='monitor_cycle_error' if last_error else None)
                except SQLAlchemyError:
                    pass

    def _check_schema(self):
        existing = set(inspect(self.engine).get_table_names())
        if 'demo_metadata' in existing:
            raise RuntimeError('Archived test database: real monitoring is forbidden.')
        required = {'inverter', 'monitor_config', 'inverter_status', 'state_event', 'monitor_heartbeat'}
        if not required <= existing:
            raise RuntimeError('Monitoring schema missing; back up the DB and initialize it explicitly.')
        from telemetry_schema import ready
        if not ready(self.engine):
            raise RuntimeError('Telemetry columns missing; run the backed-up migration before restarting.')


def database_url(uri):
    url = make_url(uri)
    if url.drivername != 'sqlite' or not url.database or url.database == ':memory:':
        raise ValueError('The standalone monitor requires a file-based SQLite database')
    path = Path(url.database)
    if not path.is_absolute():
        path = Path(__file__).parent / 'instance' / path
    return url.set(database=str(path.resolve()))


def parser():
    result = argparse.ArgumentParser(description='Read-only inverter monitor; polls the configured addresses.')
    result.add_argument('--database-uri', default=os.environ.get('INGECON_DATABASE_URI', 'sqlite:///database.db'))
    result.add_argument('--interval', type=float, default=os.environ.get('INGECON_MONITOR_INTERVAL', '30'))
    result.add_argument('--timeout', type=float, default=os.environ.get('INGECON_PROBE_TIMEOUT', '2'))
    result.add_argument('--once', action='store_true', help='One cycle, then mark the monitor stopped')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    # Validate before opening the DB, creating lock files, or touching the network.
    if not math.isfinite(args.interval) or not 1 <= args.interval <= 86400:
        raise ValueError('Interval must be 1–86400 seconds')
    probes = NetworkProbes(timeout=args.timeout)
    url = database_url(args.database_uri)
    if not Path(url.database).is_file():
        raise RuntimeError('Database does not exist; initialize it explicitly after backup.')
    engine = create_engine(url, connect_args={'timeout': 5})
    worker = MonitorWorker(engine, probes, args.interval)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: worker.stop_event.set())
    # Log state/error changes, not every failed PyModbus packet.
    logging.getLogger('pymodbus').setLevel(logging.CRITICAL)
    try:
        worker.run(once=args.once)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
