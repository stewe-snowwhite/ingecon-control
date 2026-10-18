"""SQLite-only Telegram identity, command intents, audit and notification outbox."""
import hashlib
import re
import secrets
import time

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from commands import ControlBlocked, command_payload, execute_devices
from models import (User, Inverter, StateEvent, TelegramLinkCode, TelegramLink,
                    TelegramCommand, CommandAudit, TelegramDelivery, TelegramUpdate, TelegramRate)
from monitoring import current_statuses

CODE = re.compile(r'[A-Za-z0-9_-]{32}', re.ASCII)
NOTIFY_STATES = {'Online':'online', 'Offline':'offline', 'Degraded':'degraded'}


class TelegramAccess(ValueError):
    pass


def positive_id(value):
    return type(value) is int and 0 < value < 2**63


def latest_event(session):
    return session.scalar(select(func.max(StateEvent.id))) or 0


def reserve_rate(session, key, now, limit, window):
    expired = TelegramRate.expires_at <= now
    stmt = insert(TelegramRate).values(key=key, attempts=1, expires_at=now+window)
    count = session.scalar(stmt.on_conflict_do_update(index_elements=['key'], set_={
        'attempts':case((expired,1),else_=TelegramRate.attempts+1),
        'expires_at':case((expired,now+window),else_=TelegramRate.expires_at),
    }).returning(TelegramRate.attempts))
    return count <= limit


class TelegramStore:
    def __init__(self, engine, clock=time.time):
        self.engine, self.clock = engine, clock
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self._fanout_after = 0

    def user(self, session, telegram_id):
        link = session.get(TelegramLink, telegram_id)
        user = session.get(User, link.user_id) if link else None
        if (not positive_id(telegram_id) or not link or not user or not user.active or
            user.role not in ('Administrator','Operator','Viewer') or link.user_version != user.session_version):
            raise TelegramAccess('Спочатку отримайте код у вебпанелі: Налаштування → Telegram → Створити код. Надішліть /start КОД у приватному чаті.')
        return link, user

    def issue_code(self, user_id):
        code = secrets.token_urlsafe(24)
        with self.sessions.begin() as session:
            user = session.get(User, user_id)
            if not user or not user.active:
                raise TelegramAccess('Користувач недоступний.')
            if session.scalar(select(TelegramLink.telegram_id).where(TelegramLink.user_id==user_id)):
                raise TelegramAccess('Спочатку відкличте наявну прив’язку.')
            session.execute(delete(TelegramLinkCode).where(TelegramLinkCode.user_id==user_id))
            session.add(TelegramLinkCode(user_id=user_id, digest=hashlib.sha256(code.encode()).hexdigest(),
                user_version=user.session_version, expires_at=self.clock()+300))
        return code

    def redeem(self, telegram_id, code):
        if not positive_id(telegram_id):
            raise TelegramAccess('Некоректний Telegram ID.')
        now, result = self.clock(), None
        with self.sessions.begin() as session:
            allowed = reserve_rate(session, 'link:'+str(telegram_id), now, 5, 300)
            if allowed and CODE.fullmatch(code):
                claimed = session.execute(delete(TelegramLinkCode).where(
                    TelegramLinkCode.digest==hashlib.sha256(code.encode()).hexdigest(),
                    TelegramLinkCode.expires_at>now).returning(TelegramLinkCode.user_id,
                                                             TelegramLinkCode.user_version)).first()
                if claimed:
                    user = session.get(User, claimed.user_id)
                    occupied = session.scalar(select(TelegramLink.telegram_id).where(
                        (TelegramLink.telegram_id==telegram_id) | (TelegramLink.user_id==claimed.user_id)))
                    if user and user.active and user.session_version==claimed.user_version and occupied is None:
                        session.add(TelegramLink(telegram_id=telegram_id,user_id=user.id,
                            user_version=user.session_version,linked_at=now,last_event_id=latest_event(session)))
                        result = (user.username,user.role)
        if result is None:
            raise TelegramAccess('Код недійсний, застарів або прив’язка вже існує. Отримайте новий код у вебпанелі; максимум 5 спроб за 5 хвилин.')
        return result

    def unlink(self, user_id):
        with self.sessions.begin() as session:
            self.revoke_user(session, user_id)

    @staticmethod
    def revoke_user(session, user_id):
        """Revoke access inside the caller's transaction, preserving audit/history."""
        session.execute(delete(TelegramLinkCode).where(TelegramLinkCode.user_id==user_id))
        ids = select(TelegramLink.telegram_id).where(TelegramLink.user_id==user_id)
        session.execute(update(TelegramDelivery).where(TelegramDelivery.telegram_id.in_(ids),
            TelegramDelivery.state=='pending').values(state='skipped',error='unlinked'))
        pending = select(TelegramCommand.id).where(TelegramCommand.user_id==user_id,
                                                   TelegramCommand.state=='awaiting')
        session.execute(update(CommandAudit).where(CommandAudit.request_id.in_(
            select('tg:'+TelegramCommand.id).where(TelegramCommand.id.in_(pending))))
            .values(result='cancelled'))
        session.execute(update(TelegramCommand).where(TelegramCommand.user_id==user_id,
            TelegramCommand.state=='awaiting').values(state='cancelled'))
        session.execute(delete(TelegramLink).where(TelegramLink.user_id==user_id))

    def web_link(self, user_id):
        with self.sessions() as session:
            return session.scalar(select(TelegramLink).where(TelegramLink.user_id==user_id))

    def identity(self, telegram_id):
        with self.sessions() as session:
            _, user = self.user(session,telegram_id)
            return user.username, user.role

    def snapshot(self, telegram_id):
        with self.sessions() as session:
            self.user(session,telegram_id)
            return current_statuses(session,now=self.clock())

    def inventory(self, telegram_id, kind, page=0):
        if kind not in ('i','g') or type(page) is not int or not 0 <= page <= 100000:
            raise TelegramAccess('Некоректна сторінка.')
        with self.sessions() as session:
            self.user(session,telegram_id)
            if kind == 'i':
                rows = session.execute(select(Inverter.id,Inverter.name,Inverter.ip_address)
                    .order_by(Inverter.id).offset(page*8).limit(9)).all()
            else:
                rows = session.execute(select(func.min(Inverter.id),Inverter.group)
                    .group_by(Inverter.group).order_by(Inverter.group).offset(page*8).limit(9)).all()
            return [tuple(row) for row in rows[:8]], len(rows)>8

    def history(self, telegram_id):
        with self.sessions() as session:
            self.user(session,telegram_id)
            events = session.scalars(select(StateEvent).order_by(StateEvent.id.desc()).limit(8)).all()
            return [(e.id,e.inverter_name,e.previous_state,e.state,e.occurred_at) for e in events]

    def preferences(self, telegram_id, setting=None, representative=None):
        with self.sessions.begin() as session:
            link, _ = self.user(session,telegram_id)
            if setting:
                if setting in ('notifications','online','offline','degraded'):
                    setattr(link,setting,not getattr(link,setting))
                elif setting == 'group':
                    inv = session.get(Inverter,representative) if representative else None
                    if representative and inv is None:
                        raise TelegramAccess('Групу не знайдено.')
                    link.group_filter = inv.group if inv else None
                else:
                    raise TelegramAccess('Невідоме налаштування.')
                link.last_event_id = latest_event(session)  # Never replay a disabled backlog.
            return {key:getattr(link,key) for key in ('notifications','online','offline','degraded','group_filter')}

    def accept_update(self, bot_id, update_id, telegram_id):
        if not positive_id(telegram_id) or type(update_id) is not int or not 0 <= update_id < 2**63:
            return False
        now = self.clock()
        with self.sessions.begin() as session:
            inserted = session.execute(insert(TelegramUpdate).values(key=f'{bot_id}:{update_id}', received_at=now)
                .on_conflict_do_nothing()).rowcount
            return bool(inserted and reserve_rate(session,'update:'+str(telegram_id),now,30,60))

    def targets(self, session, kind, target):
        if kind not in ('i','g') or not positive_id(target):
            raise TelegramAccess('Некоректна ціль.')
        inverter = session.get(Inverter,target)
        if not inverter:
            raise TelegramAccess('Інвертор або групу не знайдено.')
        group = inverter.group if kind=='g' else None
        rows = (session.scalars(select(Inverter).where(Inverter.group==group).order_by(Inverter.id).limit(11)).all()
                if group is not None else [inverter])
        if len(rows)>10:
            raise TelegramAccess('Максимум 10 цілей у Telegram-підтвердженні. Виберіть окремий інвертор.')
        return rows, group

    def prepare(self, telegram_id, action, kind, target):
        if action not in ('on','off'):
            raise TelegramAccess('Дозволено тільки start/stop із підтвердженням.')
        with self.sessions.begin() as session:
            _, user = self.user(session,telegram_id)
            if user.role not in ('Administrator','Operator'):
                raise TelegramAccess('Viewer не має права керувати.')
            inverters, group = self.targets(session,kind,target)
            payload = command_payload(user,action,100,inverters)
            # Keep an audit of previews, including those superseded without a click.
            old = select(TelegramCommand.id).where(TelegramCommand.telegram_id==telegram_id,
                                                 TelegramCommand.state=='awaiting')
            session.execute(update(CommandAudit).where(CommandAudit.request_id.in_(
                select('tg:'+TelegramCommand.id).where(TelegramCommand.id.in_(old))))
                .values(result='superseded'))
            session.execute(update(TelegramCommand).where(TelegramCommand.id.in_(old)).values(state='cancelled'))
            intent = secrets.token_hex(12)
            session.add(TelegramCommand(id=intent,telegram_id=telegram_id,user_id=user.id,
                payload=payload,group_name=group,expires_at=self.clock()+120))
            session.add(CommandAudit(request_id='tg:'+intent,source='telegram',telegram_id=telegram_id,
                user_id=user.id,username=user.username,occurred_at=self.clock(),action=action,
                targets=payload['targets'],result='awaiting',details=[]))
            return intent,payload

    def confirm(self, telegram_id, intent, cancel=False):
        now = self.clock()
        with self.sessions.begin() as session:
            _, user = self.user(session,telegram_id)
            row = session.execute(update(TelegramCommand).where(TelegramCommand.id==intent,
                TelegramCommand.telegram_id==telegram_id,TelegramCommand.user_id==user.id,
                TelegramCommand.state=='awaiting').values(state='processing').returning(
                    TelegramCommand.payload,TelegramCommand.group_name,TelegramCommand.expires_at)).first()
            if not row:
                raise TelegramAccess('Підтвердження вже використано або недійсне.')
            payload = row.payload
            ids = [target[0] for target in payload['targets']]
            inverters = session.scalars(select(Inverter).where(Inverter.id.in_(ids)).order_by(Inverter.id)).all()
            result, details = 'blocked', []
            if row.expires_at <= now:
                result = 'expired'
            elif cancel:
                result = 'cancelled'
            elif (user.role not in ('Administrator','Operator') or
                  command_payload(user,payload['action'],100,inverters)!=payload):
                result = 'invalidated'
            elif row.group_name is not None and session.scalars(select(Inverter.id).where(
                    Inverter.group==row.group_name).order_by(Inverter.id)).all()!=ids:
                result = 'invalidated'
            else:
                try:
                    execute_devices(user,payload['action'],inverters,source='telegram')
                except ControlBlocked as error:
                    details = [dict(id=inv.id,name=inv.name,outcome='blocked',message=str(error)) for inv in inverters]
            session.execute(update(TelegramCommand).where(TelegramCommand.id==intent).values(state=result))
            session.execute(update(CommandAudit).where(CommandAudit.request_id=='tg:'+intent)
                .values(result=result,details=details,occurred_at=now))
            return result, details

    def audit_rejected(self, telegram_id, callback_id):
        with self.sessions.begin() as session:
            link = session.get(TelegramLink,telegram_id)
            user = session.get(User,link.user_id) if link else None
            request_id = 'reject:'+hashlib.sha256(callback_id.encode()).hexdigest()[:48]
            session.execute(insert(CommandAudit).values(request_id=request_id,source='telegram',
                telegram_id=telegram_id,user_id=user.id if user else None,username=user.username if user else None,
                occurred_at=self.clock(),action='rejected',targets=[],result='denied',details=[])
                .on_conflict_do_nothing())

    def fanout(self):
        """Bounded per-recipient cursor; no writes when there are no new events."""
        now = self.clock()
        with self.sessions.begin() as session:
            links = session.scalars(select(TelegramLink).where(TelegramLink.notifications.is_(True),
                TelegramLink.telegram_id>self._fanout_after).order_by(TelegramLink.telegram_id).limit(20)).all()
            self._fanout_after = links[-1].telegram_id if len(links)==20 else 0
            for link in links:
                try:
                    self.user(session,link.telegram_id)
                except TelegramAccess:
                    continue
                events = session.scalars(select(StateEvent).where(StateEvent.id>link.last_event_id)
                    .order_by(StateEvent.id).limit(50)).all()
                for event in events:
                    field = NOTIFY_STATES.get(event.state)
                    inverter = session.get(Inverter,event.inverter_id)
                    if (field and getattr(link,field) and now-86400 <= event.occurred_at <= now and
                        event.occurred_at >= link.linked_at and
                        (link.group_filter is None or inverter and inverter.group==link.group_filter)):
                        session.execute(insert(TelegramDelivery).values(event_id=event.id,
                            telegram_id=link.telegram_id,state='pending',attempts=0,next_attempt_at=0,created_at=now)
                            .on_conflict_do_nothing())
                if events:
                    link.last_event_id = events[-1].id

    def claim_delivery(self, excluded=()):
        now = self.clock()
        with self.sessions.begin() as session:
            earliest = select(func.min(TelegramDelivery.id)).where(TelegramDelivery.state=='pending')\
                .group_by(TelegramDelivery.telegram_id)
            row = session.scalar(select(TelegramDelivery).where(TelegramDelivery.id.in_(earliest),
                TelegramDelivery.telegram_id.not_in(excluded),
                TelegramDelivery.next_attempt_at<=now).order_by(TelegramDelivery.id).limit(1))
            if row is None:
                return None
            event = session.get(StateEvent,row.event_id)
            allowed = event is not None
            try:
                link, _ = self.user(session,row.telegram_id)
                field = NOTIFY_STATES.get(event.state) if event else None
                inv = session.get(Inverter,event.inverter_id) if event else None
                allowed = bool(allowed and link.notifications and field and getattr(link,field) and
                    event.occurred_at>=link.linked_at and now-86400<=event.occurred_at<=now and
                    (link.group_filter is None or inv and inv.group==link.group_filter))
            except TelegramAccess:
                allowed = False
            if not allowed:
                row.state, row.error = 'skipped','access_or_preferences'
                return False
            claimed = session.execute(update(TelegramDelivery).where(TelegramDelivery.id==row.id,
                TelegramDelivery.state=='pending').values(state='sending',attempts=TelegramDelivery.attempts+1)).rowcount
            if not claimed:
                return False
            return row.id,row.telegram_id,(event.id,event.inverter_name,event.previous_state,event.state,event.occurred_at)

    def finish_delivery(self, delivery_id, state, *, message_id=None, error=None, retry_after=None):
        with self.sessions.begin() as session:
            row = session.get(TelegramDelivery,delivery_id)
            if row is None or row.state!='sending':
                return
            row.state, row.error = state,error
            if state=='pending' and row.attempts>=5:
                row.state, row.error = 'failed','retry_exhausted'
            row.message_id = message_id
            row.sent_at = self.clock() if state=='sent' else None
            if retry_after is not None:
                row.next_attempt_at = self.clock()+max(1,min(retry_after,86400))

    def recover(self):
        with self.sessions.begin() as session:
            session.execute(update(TelegramDelivery).where(TelegramDelivery.state=='sending')
                .values(state='unknown',error='interrupted'))

    def disable_notifications(self, telegram_id):
        with self.sessions.begin() as session:
            session.execute(update(TelegramLink).where(TelegramLink.telegram_id==telegram_id)
                .values(notifications=False))

    def cleanup(self):
        """Run hourly: bounded deletes; keep command audit until an explicit retention policy."""
        now = self.clock()
        with self.sessions.begin() as session:
            expired = select(TelegramCommand.id).where(TelegramCommand.expires_at<=now,
                                                       TelegramCommand.state=='awaiting')
            session.execute(update(CommandAudit).where(CommandAudit.request_id.in_(
                select('tg:'+TelegramCommand.id).where(TelegramCommand.id.in_(expired))))
                .values(result='expired'))
            session.execute(update(TelegramCommand).where(TelegramCommand.id.in_(expired)).values(state='expired'))
            for model, condition, key in (
                (TelegramUpdate,TelegramUpdate.received_at<now-7*86400,TelegramUpdate.key),
                (TelegramRate,TelegramRate.expires_at<now-86400,TelegramRate.key),
                (TelegramLinkCode,TelegramLinkCode.expires_at<now,TelegramLinkCode.user_id),
                (TelegramCommand,TelegramCommand.expires_at<now-86400,TelegramCommand.id),
                (TelegramDelivery,(TelegramDelivery.created_at<now-30*86400)&
                    TelegramDelivery.state.in_(('sent','unknown','failed','skipped')),TelegramDelivery.id),
            ):
                session.execute(delete(model).where(key.in_(select(key).where(condition).limit(1000))))
