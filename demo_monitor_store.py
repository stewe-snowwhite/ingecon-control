"""TASK-08 storage. Separate metadata/file; production schema is never migrated."""
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from sqlalchemy import Boolean, Float, Integer, JSON, String, UniqueConstraint, create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from models import db, Inverter, MonitorConfig, InverterStatus, StateEvent, MonitorHeartbeat
from monitoring import current_statuses

ROOT = Path(__file__).resolve().parent
DIRECTORY = ROOT/'instance'/'monitor-demo'
PURPOSE = 'ingecon-monitor-demo-v1'
NAME = 'DEMO-INVERTER-01'
HOST, PORT, DEVICE = '127.0.0.1', 15020, 1
SCENARIOS = {'normal':'Нормальна робота', 'fault':'Аварія', 'offline':'Втрата зв’язку',
             'recovery':'Відновлення зв’язку', 'stopped':'Зупинка інвертора'}
EVENT_TYPES = ('scenario','load','equipment','availability','notifications')


class Base(DeclarativeBase):
    pass


class Marker(Base):
    __tablename__ = 'demo_metadata'
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class Settings(Base):
    __tablename__ = 'demo_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    scenario: Mapped[str] = mapped_column(String, default='normal')
    load: Mapped[float] = mapped_column(Float, default=50)


class Sample(Base):
    __tablename__ = 'demo_sample'
    id: Mapped[int] = mapped_column(primary_key=True)
    measured_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    equipment: Mapped[str] = mapped_column(String, default='Unknown')
    availability: Mapped[str] = mapped_column(String, default='Unknown')


class Event(Base):
    __tablename__ = 'demo_event'
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String)
    previous: Mapped[str] = mapped_column(String, default='')
    state: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(String)
    notify: Mapped[bool] = mapped_column(Boolean, default=False)


class Subscription(Base):
    __tablename__ = 'demo_subscription'
    user_id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer)
    user_version: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    cursor: Mapped[int] = mapped_column(Integer, default=0)


class Delivery(Base):
    __tablename__ = 'demo_delivery'
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer)
    telegram_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (UniqueConstraint('event_id','telegram_id'),)


CORE_TABLES = [model.__table__ for model in (Inverter, MonitorConfig, InverterStatus, StateEvent, MonitorHeartbeat)]


def event(session, kind, state, detail, *, previous='', notify=False, now=None):
    session.add(Event(occurred_at=time.time() if now is None else now,kind=kind,state=state,
                      previous=previous,detail=detail[:256],notify=notify))


def load_value(raw):
    if not isinstance(raw,str) or not re.fullmatch(r'(?:100(?:\.0{1,2})?|[0-9]{1,2}(?:\.[0-9]{1,2})?)',raw):
        raise ValueError('Навантаження має бути числом від 0 до 100, до двох знаків після крапки.')
    return float(raw)


class DemoStore:
    def __init__(self, directory=DIRECTORY, *, initialize=False):
        self.directory = Path(directory).absolute()
        # No aliases, shared files, production directories or TASK-05 data.
        if any(p.is_symlink() for p in (self.directory,*self.directory.parents)):
            raise ValueError('Demo paths must not contain symlinks')
        self.directory = self.directory.resolve()  # Reject aliases containing '..' too.
        if any(self.directory.is_relative_to(ROOT/'instance'/name) for name in ('production','demo')):
            raise ValueError('TASK-08 needs its own private directory')
        if initialize:
            self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Demo directory must be private and owned by this user')
        self.path = self.directory/'monitor.sqlite'
        created = False
        if initialize and not self.path.exists() and not self.path.is_symlink():
            fd = os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            os.close(fd)
            created = True
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Invalid demo SQLite file')
        if not created:
            # Read-only validation BEFORE opening an SQLAlchemy writer or DDL.
            try:
                with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as connection:
                    tag = connection.execute('SELECT value FROM demo_metadata WHERE key=?',('purpose',)).fetchone()
                    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    required = set(Base.metadata.tables)|{table.name for table in CORE_TABLES}
                    if tag != (PURPOSE,) or not required <= tables or 'user' in tables:
                        raise ValueError('Not a TASK-08 database')
            except sqlite3.Error:
                raise ValueError('Not a prepared TASK-08 database') from None
        self.engine = create_engine(URL.create('sqlite',database=str(self.path)),connect_args={'timeout':5})
        self.sessions = sessionmaker(self.engine,expire_on_commit=False)
        if created:
            db.metadata.create_all(self.engine,tables=CORE_TABLES)
            Base.metadata.create_all(self.engine)
            with self.sessions.begin() as session:
                session.add(Marker(key='purpose',value=PURPOSE))
                session.add(Settings(id=1,scenario='normal',load=50))
                session.add(Sample(id=1,equipment='Unknown',availability='Unknown'))
                session.add(Inverter(id=1,name=NAME,group='DEMO',ip_address=HOST))
                session.flush()
                session.add(MonitorConfig(inverter_id=1,port=PORT,device_id=DEVICE,read_address=0,read_count=8,profile='demo_task08'))
                event(session,'scenario','normal','Створено ізольований симулятор; опитування ще не виконувалось.')

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()

    def settings(self):
        with self.sessions() as session:
            row = session.get(Settings,1)
            return row.scenario,row.load

    def change(self, scenario, raw_load, username):
        if scenario not in SCENARIOS:
            raise ValueError('Невідомий демонстраційний сценарій.')
        load = load_value(raw_load)
        with self.sessions.begin() as session:
            row = session.get(Settings,1)
            if row.scenario != scenario:
                event(session,'scenario',scenario,f'{username}: {SCENARIOS[scenario]}',previous=row.scenario)
            if row.load != load:
                event(session,'load',str(load),f'{username}: тестове навантаження {load:g}%')
            row.scenario,row.load = scenario,load

    def snapshot(self, user_id=None, kind=None, *, now=None):
        if kind is not None and kind not in EVENT_TYPES:
            raise ValueError('Невідомий тип події.')
        now = time.time() if now is None else now
        with self.sessions() as session:
            result = current_statuses(session,now=now)
            sample,settings = session.get(Sample,1),session.get(Settings,1)
            item = result['inverters'][0]
            live = not item['stale'] and item['checks']['modbus']['ok'] is True
            subscription = session.get(Subscription,user_id) if user_id else None
            result.update(name=NAME,scenario=settings.scenario,load=settings.load,
                          equipment=sample.equipment if live else 'Unknown',
                          measured_at=sample.measured_at,measurements=sample.values if live else None,
                          notifications=bool(subscription and subscription.enabled and subscription.expires_at>now),
                          notification_expires_at=subscription.expires_at if subscription and subscription.enabled else None)
            query = select(Event).order_by(Event.id.desc()).limit(100)
            if kind: query = query.where(Event.kind==kind)
            result['events'] = [dict(id=e.id,time=e.occurred_at,kind=e.kind,state=e.state,detail=e.detail)
                                for e in session.scalars(query)]
            result['deliveries'] = [dict(id=d.id,event_id=d.event_id,state=d.state,error=d.error)
                                   for d in session.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(20))]
            return result
