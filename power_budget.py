"""Persistent fleet power budget. No network I/O; all changes are explicit."""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

import inverter_controls
from modbus_config import load_configuration
from models import CommandAudit, Inverter, MonitorConfig, TelegramCommand, User
from monitoring import target_for
from operation_locks import resource_lock
from power_feedback import ALGORITHM, allocation, fresh, number, remember

INTERVAL = 30
_LOCKED = ContextVar('power_budget_locked',default=False)
_AUTOMATIC = ContextVar('power_budget_automatic',default=None)


def policy_path():
    return Path(os.environ.get('INGECON_POWER_BUDGET_FILE',
        str(Path(__file__).parent/'instance/production/power-budget.json')))


def watts(text):
    if not isinstance(text,str) or not re.fullmatch(r'[0-9]{1,7}(?:[.,][0-9]{1,3})?',text.strip(),re.ASCII):
        raise ValueError('Вкажіть додатні кВт, до трьох цифр після коми.')
    result=int(Decimal(text.strip().replace(',','.'))*1000)
    if not 1 <= result <= 100_000_000:raise ValueError('Потужність має бути більшою за 0 і не перевищувати 100000 кВт.')
    return result


def kw(value):return f'{value/1000:g}'.replace('.',',')


@contextmanager
def command_lock():
    if _LOCKED.get():
        yield
        return
    with resource_lock('power-budget-commands'):
        token=_LOCKED.set(True)
        try:yield
        finally:_LOCKED.reset(token)


def load():
    try:fd=os.open(policy_path(),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:return None
    with os.fdopen(fd) as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>262144:
            raise ValueError('Небезпечний файл загального ліміту; керування заблоковане.')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('Повторне поле загального ліміту.')
                result[key]=value
            return result
        def invalid_constant(value):raise ValueError('Нечислове значення у конфігурації.')
        data=json.loads(stream.read(262145),object_pairs_hook=unique,parse_constant=invalid_constant)
    if (not isinstance(data,dict) or type(data.get('version')) is not int or
        data.get('version') not in (1,2) or type(data.get('enabled')) is not bool):
        raise ValueError('Некоректна конфігурація загального ліміту.')
    if not isinstance(data.get('revision'),str) or not isinstance(data.get('state'),dict):
        raise ValueError('Некоректний стан загального ліміту.')
    if data['enabled']:
        if (type(data.get('target_w')) is not int or not 1<=data['target_w']<=100_000_000 or
            not isinstance(data.get('targets'),list) or not 1<=len(data['targets'])<=100):
            raise ValueError('Некоректні параметри загального ліміту.')
    if data['version']==2 and data.get('algorithm')!=ALGORITHM:
        raise ValueError('Невідомий алгоритм регулювання.')
    return data


def save(data):
    # Caller holds command_lock. Persist BEFORE any command, including in-flight state.
    path=policy_path();path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    load()  # Never overwrite a malformed, foreign or insecure file.
    fd,temp=tempfile.mkstemp(prefix='.power-budget-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(data,stream,sort_keys=True,allow_nan=False)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        directory=os.open(path.parent,os.O_DIRECTORY|os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def database_key(engine):
    return str(Path(engine.url.database).resolve()) if engine.url.database!=':memory:' else ':memory:'


def owner_key(user):
    return hashlib.sha256(f'{user.id}:{user.username}:{user.password_hash}'.encode()).hexdigest()


def inventory(session):
    from commands import MAX_CONTROL_TARGETS
    result=[]
    for inv,config in session.execute(select(Inverter,MonitorConfig).outerjoin(
            MonitorConfig,Inverter.id==MonitorConfig.inverter_id).order_by(Inverter.id)):
        if inv.model!='ingecon_sun_100tl':raise ValueError('Для автоматизації всі зареєстровані пристрої мають бути підтвердженими INGECON SUN 100TL.')
        target=target_for(inv,config);target.validate()
        inverter_controls.plan(inv,dict(port=target.port,device_id=target.device_id),'limit',100)
        result.append(dict(id=inv.id,name=inv.name,group=inv.group,ip=inv.ip_address,
                           port=target.port,device_id=target.device_id,key=target.key))
    if not 1<=len(result)<=MAX_CONTROL_TARGETS:raise ValueError('Потрібно від 1 до 100 зареєстрованих 100TL.')
    if len({(r['ip'],r['port'],r['device_id']) for r in result})!=len(result):
        raise ValueError('Виявлено дублікати Modbus-пристроїв.')
    return result


def guard_manual(action,inverters):
    config=load()
    if not config or not config['enabled'] or action not in ('limit','limit_max','on'):return
    managed={row['id'] for row in config['targets']}
    if not managed.intersection(inv.id for inv in inverters):return
    permit=_AUTOMATIC.get()
    if (permit is not None and permit['revision']==config['revision'] and action=='limit' and
        len(inverters)==1 and inverters[0].id==permit['id']):return
    from commands import ControlBlocked
    raise ControlBlocked('Активний загальний ліміт у кВт. Спершу вимкніть автоматизацію для ручного керування.')


@contextmanager
def automatic_command(revision,inverter_id):
    token=_AUTOMATIC.set(dict(revision=revision,id=inverter_id))
    try:yield
    finally:_AUTOMATIC.reset(token)


def status(now=None):
    now=time.time() if now is None else now
    config=load()
    if not config:return dict(enabled=False,phase='disabled',reason='Автоматизацію вимкнено.')
    state=config['state']
    result=dict(enabled=config['enabled'],target_w=config.get('target_w'),algorithm=config.get('algorithm'),
        phase=state.get('phase','waiting'),reason=state.get('reason','Очікування контролера.'),
        last_cycle_at=state.get('last_cycle_at'),actual_w=state.get('actual_w'),
        known=state.get('known',0),total=len(config.get('targets',[])),
        armed=state.get('armed',False),percent=state.get('desired',{}),revision=config['revision'],
        reserved_w=state.get('reserved_w',0),offline_reserved_w=state.get('offline_reserved_w',0),
        missing_ids=state.get('missing_ids',[]),unknown_ids=state.get('unknown_ids',[]),
        estimated_total_w=state.get('estimated_total_w'),partial=state.get('known',0)<len(config.get('targets',[])))
    stamp=state.get('last_cycle_at')
    result['controller_alive']=type(stamp) in (float,int) and 0<=now-stamp<=INTERVAL*3
    if config['enabled'] and not result['controller_alive']:
        result.update(phase='stale',actual_w=None,known=0,partial=True,
            reason='Немає актуального heartbeat контролера. Фактичний загальний ліміт не підтверджено.')
    return result


class BudgetService:
    def __init__(self,engine,clock=time.time):self.engine,self.clock=engine,clock

    def actor(self,session,user_id,telegram_id=0):
        if telegram_id:
            from telegram_store import TelegramStore
            _,user=TelegramStore(self.engine,clock=self.clock).user(session,telegram_id)
            if user.id!=user_id:raise ValueError('Чужий запит загального ліміту.')
        else:user=session.get(User,user_id)
        if not user or not user.active or user.role!='Administrator':raise ValueError('Автоматизація доступна лише Administrator.')
        return user

    def preview(self,user_id,target_w=None,*,telegram_id=0):
        with command_lock(),Session(self.engine) as session,session.begin():
            user=self.actor(session,user_id,telegram_id);old=load()
            if old and old['enabled'] and old['database']!=database_key(self.engine):raise ValueError('Інша база автоматизації.')
            active=target_w is not None
            if active:
                import monitor_schedule
                if monitor_schedule.current(now=self.clock())['interval_seconds']>INTERVAL:
                    raise ValueError('Для автоматизації встановіть інтервал опитування не довший за 30 секунд.')
                if not inverter_controls.enabled():raise ValueError('Чинний дозвіл фізичного керування вимкнено.')
                targets=inventory(session)
                if type(target_w) is not int or not 1<=target_w<=100_000_000:
                    raise ValueError('Загальний ліміт: понад 0 і до 100000 кВт.')
            else:
                if not old or not old['enabled']:raise ValueError('Автоматизацію вже вимкнено.')
                targets=old['targets']
            payload=dict(version=2,algorithm=ALGORITHM,revision=secrets.token_hex(16),enabled=active,target_w=target_w,
                owner_id=user.id,owner_key=owner_key(user),database=database_key(self.engine),
                authorization=inverter_controls.fingerprint(),modbus=load_configuration().fingerprint,
                targets=targets,state={},created_at=self.clock())
            intent=secrets.token_hex(12)
            envelope=dict(kind='power_budget',config=payload,base=old['revision'] if old else None,
                          user_version=user.session_version)
            if telegram_id:
                old_ids=session.scalars(select(TelegramCommand.id).where(TelegramCommand.telegram_id==telegram_id,
                    TelegramCommand.state.in_(('awaiting','input','budget_input')))).all()
                session.execute(update(CommandAudit).where(CommandAudit.request_id.in_(
                    [prefix+key for key in old_ids for prefix in ('tg:','budget:')])).values(result='superseded'))
                session.execute(update(TelegramCommand).where(TelegramCommand.id.in_(old_ids)).values(state='cancelled'))
            session.add(TelegramCommand(id=intent,telegram_id=telegram_id,user_id=user.id,payload=envelope,
                                        expires_at=self.clock()+120,state='awaiting'))
            session.add(CommandAudit(request_id='budget:'+intent,source='telegram' if telegram_id else 'web',
                telegram_id=telegram_id or None,user_id=user.id,username=user.username,occurred_at=self.clock(),
                action='budget_enable' if active else 'budget_disable',targets=targets,result='awaiting',details=[]))
            return intent,payload

    def confirm(self,user_id,intent,*,telegram_id=0,cancel=False):
        with command_lock():
            with Session(self.engine) as session,session.begin():
                user=self.actor(session,user_id,telegram_id)
                row=session.get(TelegramCommand,intent)
                if (not row or row.user_id!=user.id or row.telegram_id!=telegram_id or row.state!='awaiting' or
                    row.payload.get('kind')!='power_budget'):raise ValueError('Підтвердження вже використано або недійсне.')
                payload=row.payload['config'];old=load()
                valid=(payload.get('version')==2 and payload.get('algorithm')==ALGORITHM and
                       row.expires_at>self.clock() and row.payload['user_version']==user.session_version and
                       row.payload['base']==(old['revision'] if old else None))
                if valid and payload['enabled']:
                    valid=(payload['targets']==inventory(session) and payload['authorization']==inverter_controls.fingerprint() and
                           payload['modbus']==load_configuration().fingerprint and inverter_controls.enabled())
                row.state='cancelled' if cancel else 'processing' if valid else 'invalidated'
                session.execute(update(CommandAudit).where(CommandAudit.request_id=='budget:'+intent).values(result=row.state))
                if cancel or not valid:return row.state
            # Claim is committed before the atomic file change. No inverter I/O.
            old_keys={str(t['id']):t['key'] for t in old['targets']} if old else {}
            retained={str(t['id']) for t in payload['targets'] if old_keys.get(str(t['id']))==t['key']}
            history={key:value for key,value in old['state'].get('history',{}).items() if key in retained} if old else {}
            payload['state']=dict(history=history,not_before=self.clock(),
                                  prior_pending=old['state'].get('pending',[]) if old else [],
                                  phase='waiting' if payload['enabled'] else 'disabled',
                                  reason='Очікування контролера.' if payload['enabled'] else 'Вимкнено вручну; останні ліміти збережено.')
            save(payload)
            with Session(self.engine) as session,session.begin():
                session.get(TelegramCommand,intent).state='configured'
                session.execute(update(CommandAudit).where(CommandAudit.request_id=='budget:'+intent)
                    .values(result='configured',occurred_at=self.clock()))
            return 'configured'
