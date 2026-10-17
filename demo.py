"""Isolated, authenticated TASK-04 demo; never runs a device transport."""
import fcntl
import hashlib
import os
import re
import secrets
import signal
import socketserver
import sqlite3
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import click
from sqlalchemy import Column, MetaData, String, Table, insert
from sqlalchemy.engine import URL
from werkzeug.serving import BaseWSGIServer

from models import db, Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, StateEvent, User
from monitoring import target_for
from demo_safety import install_demo_guard

ROOT = Path(__file__).resolve().parent
PURPOSE = 'ingecon-control-demo-v1'
DEMO_SCHEMA = MetaData()
MARKER = Table('demo_metadata', DEMO_SCHEMA,
               Column('key', String, primary_key=True), Column('value', String, nullable=False))
USERNAME = re.compile(r'[a-z0-9_.-]{1,64}', re.ASCII)
SAMPLES = (
    ('Демо · Сонячний дах', 'Дах', '192.0.2.10', 'Online'),
    ('Демо · Віддалена станція', 'Поле', '198.51.100.20', 'Offline'),
    ('Демо · Нестабільний порт', 'Поле', '203.0.113.30', 'Degraded'),
    ('Демо · Новий пристрій', 'Дах', '192.0.2.40', 'Unknown'),
)


class DemoPaths:
    def __init__(self, directory=None):
        self.directory = Path(directory or ROOT / 'instance' / 'demo').absolute()
        self.database = self.directory / 'demo.sqlite'
        self.secret = self.directory / 'secret.key'
        self.log = self.directory / 'demo.log'
        self.lock = self.directory / 'demo.lock'

    @property
    def database_url(self):
        # Keep '?' and '#' in directory names as literal path characters.
        return URL.create('sqlite', database=str(self.database))

    def prepare_directory(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise click.ClickException('Каталог демо має належати поточному користувачу й мати права 0700.')

    def check_file(self, path):
        if not path.exists() and not path.is_symlink():
            return
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise click.ClickException('Runtime-файли демо мають бути звичайними приватними файлами 0600.')
        for production in (ROOT / 'instance' / 'database.db', ROOT / 'modbus.log'):
            if production.exists() and path.samefile(production):
                raise click.ClickException('Демо не може використовувати робочу БД або журнал.')

    def secret_value(self):
        self.check_file(self.secret)
        if not self.secret.is_file():
            raise click.ClickException('Секрет демо відсутній. Виконайте init або setup-admin.')
        value = self.secret.read_text().strip()
        if len(value) < 32:
            raise click.ClickException('Секрет демо пошкоджено.')
        return value

    def validate_database(self, secret):
        self.check_file(self.database)
        if not self.database.is_file():
            raise click.ClickException('Демо-БД відсутня. Виконайте init або setup-admin.')
        try:
            with sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True) as connection:
                marker = dict(connection.execute('SELECT key, value FROM demo_metadata'))
        except sqlite3.Error as error:
            raise click.ClickException('Цей файл не є підготовленою демо-БД; його не буде змінено.') from error
        expected = hashlib.sha256(secret.encode()).hexdigest()
        if marker.get('purpose') != PURPOSE or marker.get('secret_hash') != expected:
            raise click.ClickException('Маркер демо або його секрет не відповідає БД; файл не буде змінено.')


def write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)


@contextmanager
def demo_lock(paths):
    paths.prepare_directory()
    paths.check_file(paths.lock)
    fd = os.open(paths.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise click.ClickException('Інший процес уже використовує це демо. Спочатку зупиніть його.') from error
        yield
    finally:
        os.close(fd)


def create_demo_app(paths):
    """Factory does not install an irreversible hook in a caller/test process."""
    paths.prepare_directory()
    paths.check_file(paths.log)
    secret = paths.secret_value()
    paths.validate_database(secret)
    from app import create_app
    application = create_app({
        'SECRET_KEY': secret, 'SQLALCHEMY_DATABASE_URI': paths.database_url,
        'DEMO_MODE': True, 'DEBUG': False, 'MODBUS_WRITES_ENABLED': False,
        'SESSION_COOKIE_NAME': 'ingecon_demo', 'SESSION_COOKIE_SECURE': False,
        'SESSION_COOKIE_HTTPONLY': True, 'SESSION_COOKIE_SAMESITE': 'Lax',
        'MODBUS_LOG_PATH': str(paths.log),
        'TRUSTED_HOSTS': ['127.0.0.1', '192.168.10.234', 'localhost'],
    })
    return application


def initialize(paths):
    """Called under demo_lock. No copy/read of production data or credentials."""
    paths.check_file(paths.database)
    paths.check_file(paths.secret)
    paths.check_file(paths.log)
    if paths.database.exists():
        # Validate before any writable connection or create_all.
        application = create_demo_app(paths)
        return application
    if not paths.secret.exists():
        write_private(paths.secret, secrets.token_urlsafe(48) + '\n')
    secret = paths.secret_value()
    # Reserve a fresh private file; never open/truncate an existing SQLite file.
    fd = os.open(paths.database, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    os.close(fd)
    from app import create_app
    bootstrap = create_app({'SECRET_KEY': secret, 'SQLALCHEMY_DATABASE_URI':paths.database_url,
                            'DEMO_MODE':True, 'MODBUS_WRITES_ENABLED':False})
    with bootstrap.app_context():
        DEMO_SCHEMA.create_all(db.engine)
        db.create_all()
        db.session.execute(insert(MARKER), [
            {'key':'purpose','value':PURPOSE},
            {'key':'secret_hash','value':hashlib.sha256(secret.encode()).hexdigest()},
        ])
        now = time.time()
        for name, group, host, state in SAMPLES:
            inv = Inverter(name=name, group=group, ip_address=host)
            db.session.add(inv)
            db.session.flush()
            config = MonitorConfig(inverter_id=inv.id)  # No register configured.
            db.session.add(config)
            db.session.flush()
            status = InverterStatus(inverter_id=inv.id, target_key=target_for(inv,config).key,
                                    state=state, checked_at=now)
            simulate_fields(status, now)
            db.session.add(status)
            if state != 'Unknown':
                db.session.add(StateEvent(inverter_id=inv.id, inverter_name=inv.name, target_key=status.target_key,
                    previous_state='Unknown', state=state, occurred_at=now-60*inv.id, reason=status.error_reason))
        db.session.add(MonitorHeartbeat(id=1, owner='demo-simulation', started_at=now, last_seen=now,
                                       interval_seconds=30, stopped=True))
        db.session.commit()
        db.engine.dispose()
    if not paths.log.exists():
        write_private(paths.log, 'ДЕМО: усі пристрої та результати вигадані. Мережеві перевірки й записи вимкнено.\n')
    return create_demo_app(paths)


def simulate_fields(status, now):
    state = status.state
    status.checked_at = now
    status.icmp_ok = False if state in ('Online','Offline') else True if state == 'Degraded' else None
    status.tcp_ok = True if state == 'Online' else False if state in ('Offline','Degraded') else None
    status.modbus_ok = True if state in ('Online','Degraded') else False if state == 'Offline' else None
    status.icmp_error = 'icmp_no_reply' if status.icmp_ok is False else None
    status.tcp_error = 'tcp_unreachable' if status.tcp_ok is False else None
    status.modbus_error = 'modbus_timeout' if state == 'Offline' else 'modbus_not_configured' if state == 'Unknown' else None
    status.error_reason = ';'.join(value for value in (status.tcp_error,status.modbus_error) if value) or None
    status.icmp_latency_ms = 2 if status.icmp_ok is not None else None
    status.tcp_latency_ms = 4 if status.tcp_ok is not None else None
    status.latency_ms = 7 if status.modbus_ok else None
    if status.modbus_ok: status.last_success_at = now
    elif state == 'Offline' and status.last_success_at is None: status.last_success_at = now-3600
    elif state == 'Unknown': status.last_success_at = None
    status.successes = 2 if state == 'Online' else 0
    status.failures = 3 if state == 'Offline' else 0
    status.power_limit_percent = 50 if state == 'Online' else None


def refresh_simulation(application, stopped=False):
    """Local synthetic updates only. No NetworkProbes or Modbus clients."""
    now = time.time()
    with application.app_context():
        for inv, config in db.session.execute(db.select(Inverter, MonitorConfig).outerjoin(
                MonitorConfig, Inverter.id == MonitorConfig.inverter_id)):
            key = target_for(inv, config).key
            status = db.session.get(InverterStatus, inv.id)
            if status is None:
                status = InverterStatus(inverter_id=inv.id, target_key=key, state='Unknown', checked_at=now)
                db.session.add(status)
            elif status.target_key != key:
                previous = status.state
                status.state, status.target_key = 'Unknown', key
                if previous != 'Unknown':
                    db.session.add(StateEvent(inverter_id=inv.id,inverter_name=inv.name,target_key=key,
                        previous_state=previous,state='Unknown',occurred_at=now,reason='target_changed'))
            simulate_fields(status, now)
            if config is not None: config.requested_at = None
        heartbeat = db.session.get(MonitorHeartbeat,1)
        heartbeat.owner, heartbeat.last_seen, heartbeat.stopped = 'demo-simulation', now, stopped
        heartbeat.last_error = None
        db.session.commit()


class DemoHTTPServer(BaseWSGIServer):
    def server_bind(self):
        # HTTPServer normally calls getfqdn (reverse DNS). Demo never does DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


def serve(application, host):
    with application.app_context():
        administrator = db.session.scalar(db.select(User.id).where(User.role=='Administrator',User.active.is_(True)))
        if administrator is None:
            raise click.ClickException('Спочатку створіть тестового адміністратора командою setup-admin.')
    refresh_simulation(application)
    stop = threading.Event()
    def tick():
        while not stop.wait(30):
            try:
                refresh_simulation(application)
            except Exception:
                click.echo('Помилка імітації: дані можуть застаріти.', err=True)
                # Leave the previous heartbeat to expire; never fall back to real probes.
    thread = threading.Thread(target=tick, name='demo-simulation', daemon=True)
    server = None
    def terminate(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.getsignal(signal.SIGTERM)
    try:
        server = DemoHTTPServer(host,5000,application)
        signal.signal(signal.SIGTERM, terminate)
        thread.start()
        url = '192.168.10.234' if host == '0.0.0.0' else host
        click.echo(f'ДЕМО: http://{url}:5000 · слухач {host}:5000 · debug вимкнено · вихідні з’єднання заблоковано')
        click.echo('Зупинка: Ctrl+C або SIGTERM. Використовуйте лише окремий тестовий пароль.')
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        stop.set()
        if thread.is_alive(): thread.join(timeout=10)
        if server is not None: server.server_close()
        refresh_simulation(application, stopped=True)


@click.group()
@click.option('--directory', type=click.Path(path_type=Path), default=ROOT/'instance'/'demo',
              help='Окремий приватний каталог демо; файли завжди demo.sqlite та secret.key.')
@click.pass_context
def cli(ctx, directory):
    """Безпечна демонстрація TASK-04 з вигаданими даними."""
    ctx.obj = DemoPaths(directory)


@cli.command('init')
@click.pass_obj
def init_command(paths):
    with demo_lock(paths):
        initialize(paths)
    click.echo('Демо-БД підготовлено; наявні демодані збережено. Далі: setup-admin.')


@cli.command('setup-admin')
@click.pass_obj
def setup_admin(paths):
    with demo_lock(paths):
        application = initialize(paths)
        username = click.prompt('Ім’я тестового адміністратора').strip().lower()
        if not USERNAME.fullmatch(username):
            raise click.ClickException('Ім’я: 1–64 символи a-z, 0-9, _, ., -.')
        password = click.prompt('Новий окремий тестовий пароль', hide_input=True, confirmation_prompt=True)
        with application.app_context():
            user = db.session.scalar(db.select(User).where(User.username==username))
            if user is None:
                user = User(username=username,role='Administrator')
                db.session.add(user)
            user.role, user.active = 'Administrator', True
            try: user.set_password(password)
            except ValueError as error: raise click.ClickException(str(error)) from error
            db.session.commit()
    click.echo('Тестового адміністратора збережено. Пароль та секрет не виводяться.')


@cli.command('serve')
@click.option('--host', type=click.Choice(['127.0.0.1','0.0.0.0']), default='127.0.0.1', show_default=True)
@click.pass_obj
def serve_command(paths, host):
    with demo_lock(paths):
        serve(create_demo_app(paths),host)


def main():
    # Determine the one allowed listener without trusting environment variables.
    args = sys.argv[1:]
    host = '127.0.0.1'
    for index, arg in enumerate(args):
        if arg == '--host' and index+1 < len(args): host = args[index+1]
        elif arg.startswith('--host='): host = arg.partition('=')[2]
    if host not in ('127.0.0.1','0.0.0.0'):
        click.echo('Error: Адреса слухача не дозволена.', err=True)
        sys.exit(2)
    os.umask(0o077)
    install_demo_guard(host)
    cli()


if __name__ == '__main__':
    main()
