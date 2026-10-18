"""DEMO outbox with read-only checks of production identity; never a poller."""
import logging
import time

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from demo_monitor_store import DIRECTORY, DemoStore, Event, Subscription, Delivery, event
from models import User, TelegramLink


def allowed(engine, subscription):
    with Session(engine) as session:
        user = session.get(User,subscription.user_id)
        link = session.get(TelegramLink,subscription.telegram_id)
        return bool(user and user.active and user.role=='Administrator' and link and
                    link.user_id==user.id and user.session_version==link.user_version==subscription.user_version)


def subscribe(store, user, link, enabled, *, now=None):
    now = time.time() if now is None else now
    if enabled and (not link or link.user_id!=user.id or link.user_version!=user.session_version or user.role!='Administrator'):
        raise ValueError('Спочатку створіть чинну особисту Telegram-прив’язку.')
    with store.sessions.begin() as session:
        sub = session.get(Subscription,user.id)
        if not enabled:
            if sub: sub.enabled = False
        else:
            if sub is None:
                sub = Subscription(user_id=user.id)
                session.add(sub)
            sub.telegram_id,sub.user_version = link.telegram_id,user.session_version
            sub.enabled,sub.enabled_at,sub.expires_at = True,now,now+3600
            sub.cursor = session.scalar(select(func.max(Event.id))) or 0
        event(session,'notifications','enabled' if enabled else 'disabled',
              f'{user.username}: особисті [DEMO]-сповіщення '+('на одну годину.' if enabled else 'вимкнено.'),now=now)


class DemoNotifications:
    def __init__(self, store, production_engine, clock=time.time):
        self.store,self.production_engine,self.clock = store,production_engine,clock
        self.sessions = store.sessions
        self._fanout_after = 0

    def fanout(self):
        now = self.clock()
        with self.sessions.begin() as session:
            subscribers = session.scalars(select(Subscription).where(Subscription.enabled.is_(True),
                Subscription.expires_at>now,Subscription.user_id>self._fanout_after).order_by(Subscription.user_id).limit(20)).all()
            self._fanout_after = subscribers[-1].user_id if len(subscribers)==20 else 0
            for sub in subscribers:
                if not allowed(self.production_engine,sub): continue
                events = session.scalars(select(Event).where(Event.id>sub.cursor).order_by(Event.id).limit(100)).all()
                for item in events:
                    if item.notify and max(sub.enabled_at,now-3600)<=item.occurred_at<=now:
                        session.execute(insert(Delivery).values(event_id=item.id,telegram_id=sub.telegram_id,
                            user_id=sub.user_id,state='pending',attempts=0,next_attempt_at=0).on_conflict_do_nothing())
                if events: sub.cursor = events[-1].id

    def claim_delivery(self, excluded=()):
        now = self.clock()
        with self.sessions.begin() as session:
            earliest = select(func.min(Delivery.id)).where(Delivery.state=='pending').group_by(Delivery.telegram_id)
            row = session.scalar(select(Delivery).where(Delivery.id.in_(earliest),
                Delivery.telegram_id.not_in(excluded),Delivery.next_attempt_at<=now).order_by(Delivery.id).limit(1))
            if row is None: return None
            sub,item = session.get(Subscription,row.user_id),session.get(Event,row.event_id)
            if not (sub and item and item.notify and sub.enabled and sub.expires_at>now and
                    sub.telegram_id==row.telegram_id and max(sub.enabled_at,now-3600)<=item.occurred_at<=now and
                    allowed(self.production_engine,sub)):
                row.state,row.error = 'skipped','access_or_preferences'
                return False
            claimed = session.execute(update(Delivery).where(Delivery.id==row.id,Delivery.state=='pending')
                .values(state='sending',attempts=Delivery.attempts+1)).rowcount
            if not claimed: return False
            return row.id,row.telegram_id,(item.id,'DEMO-INVERTER-01',item.previous,item.state,item.occurred_at)

    def notification_text(self, data):
        from telegram_ui import utc
        ident,name,previous,state,occurred = data
        return f'[DEMO] {name} · подія #{ident}\n{previous} → {state}\n{utc(occurred)}\nТестовий симулятор. Це не production-тривога.'

    def finish_delivery(self, delivery_id, state, *, message_id=None, error=None, retry_after=None):
        with self.sessions.begin() as session:
            row = session.get(Delivery,delivery_id)
            if row is None or row.state!='sending': return
            row.state,row.error = state,error
            if state=='pending' and row.attempts>=5: row.state,row.error = 'failed','retry_exhausted'
            row.message_id,row.sent_at = message_id,self.clock() if state=='sent' else None
            if retry_after is not None: row.next_attempt_at = self.clock()+max(1,min(retry_after,3600))

    def disable_notifications(self, telegram_id):
        with self.sessions.begin() as session:
            session.execute(update(Subscription).where(Subscription.telegram_id==telegram_id).values(enabled=False))

    def recover(self):
        with self.sessions.begin() as session:
            session.execute(update(Delivery).where(Delivery.state=='sending').values(state='unknown',error='interrupted'))

    def cleanup(self):
        # Separate retention, never production history. Keep >=24h and 2,000 events.
        with self.sessions.begin() as session:
            cutoff = (session.scalar(select(func.max(Event.id))) or 0)-2000
            expired = select(Event.id).where(Event.id<cutoff,Event.occurred_at<self.clock()-86400)
            session.execute(delete(Delivery).where(Delivery.event_id.in_(expired)))
            session.execute(delete(Event).where(Event.id.in_(expired)))


class DemoNotificationBridge:
    """Lazy optional branch of the EXISTING bot's notification loop."""
    def __init__(self, production_engine, directory=None):
        self.production_engine = production_engine
        self.directory = DIRECTORY if directory is None else directory
        self.notifications = None
        self.failed = False
        self.cleaned = 0

    async def deliver(self, bot):
        from pathlib import Path
        from telegram_ui import notification_batch
        if not (Path(self.directory)/'monitor.sqlite').exists(): return
        try:
            if self.notifications is None:
                store = DemoStore(self.directory)
                self.notifications = DemoNotifications(store,self.production_engine)
                self.notifications.recover()
            if self.notifications.clock()-self.cleaned>=3600:
                self.notifications.cleanup()
                self.cleaned = self.notifications.clock()
            await notification_batch(self.notifications,bot)
            self.failed = False
        except (OSError,ValueError,SQLAlchemyError):
            if not self.failed: logging.getLogger(__name__).warning('DEMO notifications unavailable; production bot continues.')
            self.failed = True

    def close(self):
        if self.notifications: self.notifications.store.close()
