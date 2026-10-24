"""SQLite-only Telegram identity, command intents, audit and notification outbox."""
import hashlib
import logging
import re
import secrets
import time

from sqlalchemy import case, delete, func, inspect, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from commands import MAX_CONTROL_TARGETS, ControlBlocked, command_payload, execute_devices, transports_for
import inverter_controls
from models import (User, Inverter, StateEvent, TelegramLinkCode, TelegramLink,
                    TelegramCommand, CommandAudit, TelegramDelivery, TelegramUpdate, TelegramRate)
from monitoring import current_statuses
from telegram_control import TelegramControlPolicy
from modbus_config import load_configuration
import modbus_safety
import monitor_refresh

CODE = re.compile(r'[A-Za-z0-9_-]{32}', re.ASCII)
LIMIT_INPUT_SECONDS = 300
NOTIFY_STATES = {'Online':'online', 'Offline':'offline', 'Degraded':'degraded'}
log = logging.getLogger('ingecon.telegram')


class TelegramAccess(ValueError):
    pass


def positive_id(value):
    return type(value) is int and 0 < value < 2**63


def latest_event(session):
    return session.scalar(select(func.max(StateEvent.id))) or 0


def intent_audits(ids):
    # Shared intent table also holds web/Telegram fleet-budget confirmations.
    return or_(*(CommandAudit.request_id.in_(select(prefix+TelegramCommand.id).where(
        TelegramCommand.id.in_(ids))) for prefix in ('tg:','budget:')))


def reserve_rate(session, key, now, limit, window):
    expired = TelegramRate.expires_at <= now
    stmt = insert(TelegramRate).values(key=key, attempts=1, expires_at=now+window)
    count = session.scalar(stmt.on_conflict_do_update(index_elements=['key'], set_={
        'attempts':case((expired,1),else_=TelegramRate.attempts+1),
        'expires_at':case((expired,now+window),else_=TelegramRate.expires_at),
    }).returning(TelegramRate.attempts))
    return count <= limit


class TelegramStore:
    def __init__(self, engine, clock=time.time, control_policy=None):
        self.engine, self.clock = engine, clock
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self._fanout_after = 0
        self.control_policy = control_policy or TelegramControlPolicy()

    @property
    def controls_enabled(self):
        return self.control_policy.enabled and (inverter_controls.enabled() or not modbus_safety.writes_locked())

    def control_payload(self, user, action, inverters, value=100, *, all_targets=False):
        payload = command_payload(user, action, value, inverters)
        payload['control_policy'] = self.control_policy.fingerprint
        payload['modbus_config'] = load_configuration().fingerprint
        if all_targets:
            payload['target_scope'] = 'all'
        return payload

    def user(self, session, telegram_id):
        pair = None
        if positive_id(telegram_id):
            # One SQLite snapshot avoids mixing old pairing and new account
            # versions during a concurrent web logout; refresh cached ORM rows.
            pair = session.execute(select(TelegramLink, User).join(User, User.id==TelegramLink.user_id)
                .where(TelegramLink.telegram_id==telegram_id).execution_options(populate_existing=True)).first()
        link, user = pair if pair else (None, None)
        if (not link or not user or not user.active or
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
    def preserve_on_web_logout(session, user_id, previous_version, next_version):
        """Caller first rotates User atomically, in this same write transaction.

        Carry forward only currently valid pairings/codes. A password-revoked
        or otherwise stale record must never be reauthorized by a web logout.
        No pairing timestamp, notification preference or code expiry is reset.
        """
        schema = inspect(session.connection())
        for model in (TelegramLink, TelegramLinkCode):
            if schema.has_table(model.__tablename__):
                session.execute(update(model).where(model.user_id==user_id,
                    model.user_version==previous_version).values(user_version=next_version))

    @staticmethod
    def revoke_user(session, user_id):
        """Revoke access inside the caller's transaction, preserving audit/history."""
        session.execute(delete(TelegramLinkCode).where(TelegramLinkCode.user_id==user_id))
        ids = select(TelegramLink.telegram_id).where(TelegramLink.user_id==user_id)
        session.execute(update(TelegramDelivery).where(TelegramDelivery.telegram_id.in_(ids),
            TelegramDelivery.state=='pending').values(state='skipped',error='unlinked'))
        pending = select(TelegramCommand.id).where(TelegramCommand.user_id==user_id,
                                                   TelegramCommand.state.in_(('awaiting','input','budget_input')))
        session.execute(update(CommandAudit).where(intent_audits(pending))
            .values(result='cancelled'))
        session.execute(update(TelegramCommand).where(TelegramCommand.user_id==user_id,
            TelegramCommand.state.in_(('awaiting','input','budget_input'))).values(state='cancelled'))
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
            now = self.clock()
            return dict(current_statuses(session,now=now), generated_at=now)

    def request_status_refresh(self,telegram_id):
        with self.sessions.begin() as session:
            self.user(session,telegram_id)
            return monitor_refresh.request(session,self.clock())

    def status_refresh_progress(self,telegram_id,ticket):
        with self.sessions() as session:
            self.user(session,telegram_id)
            return monitor_refresh.progress(session,ticket,self.clock())

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
        if kind == 'a':
            if type(target) is not int or target != 0:
                raise TelegramAccess('Некоректна ціль.')
            rows = session.scalars(select(Inverter).order_by(Inverter.id).limit(MAX_CONTROL_TARGETS+1)).all()
            if not rows:
                raise TelegramAccess('Група «Усі» порожня.')
            if len(rows)>MAX_CONTROL_TARGETS:
                raise TelegramAccess(f'Спільна межа системи — {MAX_CONTROL_TARGETS} пристроїв на команду. Виберіть меншу групу.')
            return rows, None
        if kind not in ('i','g') or not positive_id(target):
            raise TelegramAccess('Некоректна ціль.')
        inverter = session.get(Inverter,target)
        if not inverter:
            raise TelegramAccess('Інвертор або групу не знайдено.')
        group = inverter.group if kind=='g' else None
        rows = (session.scalars(select(Inverter).where(Inverter.group==group).order_by(Inverter.id).limit(MAX_CONTROL_TARGETS+1)).all()
                if group is not None else [inverter])
        if len(rows)>MAX_CONTROL_TARGETS:
            raise TelegramAccess(f'Спільна межа системи — {MAX_CONTROL_TARGETS} пристроїв на команду. Виберіть меншу групу.')
        return rows, group

    def prepare(self, telegram_id, action, kind, target, value=100):
        if action not in ('on','off','limit','limit_max') or type(value) is not int or not 1 <= value <= 100:
            raise TelegramAccess('Дозволено start/stop і ліміт 1–100 із підтвердженням.')
        with self.sessions.begin() as session:
            _, user = self.user(session,telegram_id)
            if user.role not in ('Administrator','Operator'):
                raise TelegramAccess('Viewer не має права керувати.')
            inverters, group = self.targets(session,kind,target)
            payload = self.control_payload(user,action,inverters,value,all_targets=kind=='a')
            return self._new_intent(session,user,telegram_id,payload,group)

    def _new_intent(self, session, user, telegram_id, payload, group, *, input_request=False):
        # A new choice supersedes both an old prompt and an old confirmation.
        old = select(TelegramCommand.id).where(TelegramCommand.telegram_id==telegram_id,
            TelegramCommand.state.in_(('awaiting','input','budget_input')))
        session.execute(update(CommandAudit).where(intent_audits(old)).values(result='superseded'))
        session.execute(update(TelegramCommand).where(TelegramCommand.id.in_(old)).values(state='cancelled'))
        intent = secrets.token_hex(12)
        session.add(TelegramCommand(id=intent,telegram_id=telegram_id,user_id=user.id,
            payload=payload,group_name=group,expires_at=self.clock()+(LIMIT_INPUT_SECONDS if input_request else 120),
            state='input' if input_request else 'awaiting'))
        session.add(CommandAudit(request_id='tg:'+intent,source='telegram',telegram_id=telegram_id,
            user_id=user.id,username=user.username,occurred_at=self.clock(),action=payload['action'],
            targets=payload['targets'],result='awaiting_input' if input_request else 'awaiting',details=[]))
        return intent,payload

    def _targets_match(self, session, user, payload, inverters, group):
        if user.role not in ('Administrator','Operator') or self.control_payload(user,
                payload['action'],inverters,payload.get('value',100),
                all_targets=payload.get('target_scope')=='all')!=payload:
            return False
        ids=[target[0] for target in payload['targets']]
        if payload.get('target_scope')=='all':
            return session.scalars(select(Inverter.id).order_by(Inverter.id)).all()==ids
        return group is None or session.scalars(select(Inverter.id).where(
            Inverter.group==group).order_by(Inverter.id)).all()==ids

    def request_limit_input(self, telegram_id, kind, target):
        with self.sessions.begin() as session:
            _,user=self.user(session,telegram_id)
            if user.role not in ('Administrator','Operator'):
                raise TelegramAccess('Viewer не має права керувати.')
            if not self.controls_enabled:
                raise TelegramAccess('Керування лімітом заблоковане конфігурацією.')
            inverters,group=self.targets(session,kind,target)
            try:
                self.control_policy.validate(inverters,'limit')
            except ValueError as error:
                raise TelegramAccess(str(error)) from None
            payload=self.control_payload(user,'limit',inverters,100,all_targets=kind=='a')
            payload['input_scope']=dict(kind=kind,target=target,prompt_id=None)
            return self._new_intent(session,user,telegram_id,payload,group,input_request=True)

    def bind_limit_prompt(self, telegram_id, intent, message_id):
        if not positive_id(message_id):raise TelegramAccess('Некоректний запит введення.')
        with self.sessions.begin() as session:
            _,user=self.user(session,telegram_id)
            row=session.get(TelegramCommand,intent)
            if not row or row.telegram_id!=telegram_id or row.user_id!=user.id or row.state!='input':
                raise TelegramAccess('Запит введення вже скасований.')
            payload=dict(row.payload)
            payload['input_scope']=dict(payload['input_scope'],prompt_id=message_id)
            row.payload=payload

    def cancel_limit_input(self, telegram_id):
        with self.sessions.begin() as session:
            _,user=self.user(session,telegram_id)
            ids=session.scalars(select(TelegramCommand.id).where(TelegramCommand.telegram_id==telegram_id,
                TelegramCommand.user_id==user.id,TelegramCommand.state.in_(('input','budget_input')))).all()
            if not ids:return False
            session.execute(update(TelegramCommand).where(TelegramCommand.id.in_(ids)).values(state='cancelled'))
            session.execute(update(CommandAudit).where(CommandAudit.request_id.in_(['tg:'+value for value in ids]))
                .values(result='cancelled'))
            return True

    def submit_limit_input(self, telegram_id, text, reply_to=None):
        failure=None
        with self.sessions.begin() as session:
            _,user=self.user(session,telegram_id)
            if user.role not in ('Administrator','Operator'):
                raise TelegramAccess('Viewer не має права керувати.')
            row=session.scalar(select(TelegramCommand).where(TelegramCommand.telegram_id==telegram_id,
                TelegramCommand.user_id==user.id,TelegramCommand.state=='input').order_by(TelegramCommand.expires_at.desc()).limit(1))
            if row is None:raise TelegramAccess('Спочатку виберіть інвертор або групу й натисніть «Ввести ліміт».')
            scope=row.payload['input_scope']
            if not positive_id(scope.get('prompt_id')):
                raise TelegramAccess('Запит введення не готовий. Натисніть «Ввести ліміт» ще раз.')
            if reply_to is not None and reply_to!=scope['prompt_id']:
                raise TelegramAccess('Це відповідь на інший запит. Використайте останній запит введення.')
            if not isinstance(text,str) or not re.fullmatch(r'(?:[1-9][0-9]?|100)\s*%?',text.strip(),re.ASCII):
                raise TelegramAccess('Введіть ціле число від 1 до 100, наприклад 37 або 37%. Для скасування — /cancel.')
            value=int(text.strip().rstrip('%').strip())
            claimed=session.execute(update(TelegramCommand).where(TelegramCommand.id==row.id,
                TelegramCommand.state=='input').values(state='input_claimed')).rowcount
            if not claimed:raise TelegramAccess('Цей запит введення вже використаний.')
            session.expire_all()
            _,user=self.user(session,telegram_id)
            payload={key:value for key,value in row.payload.items() if key!='input_scope'}
            ids=[target[0] for target in payload['targets']]
            inverters=session.scalars(select(Inverter).where(Inverter.id.in_(ids)).order_by(Inverter.id)).all()
            try:
                matches=self.controls_enabled and self._targets_match(session,user,payload,inverters,row.group_name)
            except ValueError:
                matches=False  # A malformed/revoked configuration never authorizes input.
            if row.expires_at<=self.clock():
                result='expired';failure='Час введення минув. Натисніть «Ввести ліміт» ще раз.'
            elif not matches:
                result='invalidated';failure='Права, конфігурація або список пристроїв змінилися. Виберіть ціль ще раз.'
            else:
                result='awaiting'
                payload=dict(payload,value=value)
                row.payload=payload
                row.expires_at=self.clock()+120
            row.state=result
            session.execute(update(CommandAudit).where(CommandAudit.request_id=='tg:'+row.id)
                .values(result=result,occurred_at=self.clock()))
            intent=row.id
        if failure:raise TelegramAccess(failure)
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
            session.expire_all()
            _, user = self.user(session, telegram_id)  # Recheck after the atomic write claim.
            payload = row.payload
            if payload.get('kind')=='power_budget':
                raise TelegramAccess('Використайте кнопки підтвердження загального ліміту.')
            ids = [target[0] for target in payload['targets']]
            inverters = session.scalars(select(Inverter).where(Inverter.id.in_(ids)).order_by(Inverter.id)).all()
            result, details = 'processing', []
            if row.expires_at <= now:
                result = 'expired'
            elif cancel:
                result = 'cancelled'
            elif not self._targets_match(session,user,payload,inverters,row.group_name):
                result = 'invalidated'
            else:
                try:
                    # Preflight all targets before persisting the execution claim.
                    self.control_policy.validate(inverters, payload['action'])
                    if inverter_controls.enabled():
                        transports = transports_for(inverters)
                        for inv in inverters:
                            inverter_controls.plan(inv, transports[inv.id], payload['action'], payload['value'])
                except ValueError as error:
                    result = 'blocked'
                    details = [dict(id=inv.id,name=inv.name,outcome='blocked',message=str(error)) for inv in inverters]
            session.execute(update(TelegramCommand).where(TelegramCommand.id==intent).values(state=result))
            session.execute(update(CommandAudit).where(CommandAudit.request_id=='tg:'+intent)
                .values(result=result,details=details,occurred_at=now))
            # Detached immutable-in-practice request snapshots; no SQLite lock
            # or checked-out connection is held during device operations.
            if result == 'processing':
                transports = transports_for(inverters)
                session.expunge(user)
                for inverter in inverters:
                    session.expunge(inverter)
        if result != 'processing':
            return result, details
        try:
            details = execute_devices(user, payload['action'], inverters, payload.get('value',100), source='telegram',
                writes_enabled=self.controls_enabled, telegram_policy=self.control_policy, transports=transports)
            count = sum(row['outcome'] == 'register_confirmed' for row in details)
            result = 'confirmed' if count == len(details) else 'partial' if count else 'unconfirmed'
        except ControlBlocked as error:
            result = 'blocked'
            details = [dict(id=inv.id,name=inv.name,outcome='blocked',message=str(error)) for inv in inverters]
        except Exception as error:
            # The write might have happened; never automatically retry this intent.
            log.error('Control execution interrupted: %s', type(error).__name__)
            result = 'unknown'
            details = [dict(id=inv.id,name=inv.name,outcome='unknown',
                message='Результат невідомий; автоматичного повторення немає.') for inv in inverters]
        with self.sessions.begin() as session:
            session.execute(update(TelegramCommand).where(TelegramCommand.id==intent,
                TelegramCommand.state=='processing').values(state=result))
            session.execute(update(CommandAudit).where(CommandAudit.request_id=='tg:'+intent)
                .values(result=result,details=details,occurred_at=self.clock()))
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
            processing = select(TelegramCommand.id).where(TelegramCommand.state=='processing')
            session.execute(update(CommandAudit).where(intent_audits(processing))
                .values(result='unknown'))
            session.execute(update(TelegramCommand).where(TelegramCommand.state=='processing').values(state='unknown'))
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
                                                       TelegramCommand.state.in_(('awaiting','input','budget_input')))
            session.execute(update(CommandAudit).where(intent_audits(expired))
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
