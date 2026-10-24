"""Separate 30-second fleet controller. Dry-run unless --enable-writes is explicit."""
import argparse
import logging
from pathlib import Path
import signal
import threading
import time
import secrets

from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from commands import ControlBlocked, execute_devices
import inverter_controls
from modbus_config import load_configuration
from models import CommandAudit, Inverter, User
from monitor import database_url
from monitoring import current_statuses
from operation_locks import OperationBusy, resource_lock
import power_budget as budget

log=logging.getLogger(__name__)


class PowerController:
    def __init__(self,engine,*,enable_writes=False,clock=time.time,stop_event=None):
        if type(enable_writes) is not bool:raise ValueError('enable_writes must be boolean')
        self.engine,self.armed,self.clock=engine,enable_writes,clock
        self.stop_event=stop_event or threading.Event()

    def _authorized(self,session,config):
        user=session.get(User,config['owner_id'])
        if (config['database']!=budget.database_key(self.engine) or not user or not user.active or
            user.role!='Administrator' or budget.owner_key(user)!=config['owner_key']):
            raise ValueError('Дозвіл адміністратора відкликано; потрібне нове підтвердження.')
        if (not inverter_controls.enabled() or inverter_controls.fingerprint()!=config['authorization'] or
            load_configuration().fingerprint!=config['modbus'] or budget.inventory(session)!=config['targets']):
            raise ValueError('Дозвіл, карта або склад обладнання змінилися; підтвердьте автоматизацію заново.')
        return user

    def run_cycle(self):
        with budget.command_lock():
            config=budget.load()
            if not config or not config['enabled']:return
            state=config['state']
            now=self.clock()
            state.update(last_cycle_at=now,armed=self.armed)
            def finish(phase,reason):
                state.update(phase=phase,reason=reason)
                budget.save(config)
                return phase
            try:
                with Session(self.engine) as session:
                    self._authorized(session,config)
                    snapshot=current_statuses(session,now=now)
                rows=snapshot['inverters']
                for row in rows:
                    if row['checked_at'] is None or not 0<=now-row['checked_at']<=budget.INTERVAL*2:
                        row['stale']=True
                known=[row['active_power_w'] for row in rows if not row['stale'] and row['active_power_w'] is not None]
                state.update(actual_w=sum(known) if known else None,known=len(known))
                if not snapshot['monitor']['alive']:return finish('paused','Немає актуального моніторингу. Нових команд немає.')
                by_id={str(row['id']):row for row in rows}
                pending=[]
                for attempt in state.get('pending',[]):
                    row=by_id[attempt['id']]
                    if row['stale'] or row['checks']['modbus']['ok'] is not True or row['checked_at']<=attempt['at']:
                        pending.append(attempt);continue
                    observed=row['power_limit_percent']
                    if observed is not None and attempt['percent']-1<=observed<=attempt['percent']:
                        state.setdefault('sent',{})[attempt['id']]=attempt
                    else:
                        state['fault']=True
                        pending.append(attempt)
                state['pending']=pending
                if state.get('fault'):return finish('paused','Читання не підтвердило останній запис. Автоматичного повторення немає; потрібна перевірка й нове підтвердження.')
                if pending:return finish('awaiting_readback','Очікування свіжого FC04-підтвердження лімітів; підвищення призупинено.')
                for ident,attempt in state.get('sent',{}).items():
                    row=by_id[ident];observed=row['power_limit_percent']
                    if (not row['stale'] and observed is not None and attempt['percent']-1<=observed<=attempt['percent'] and
                        row['checked_at']>attempt['at']):row['effective_percent']=attempt['percent']
                plan=budget.allocation(config['target_w'],config['rated_w'],rows,state.get('estimates'))
                state['desired']=plan['percent'];state['reserved_w']=plan['reserved_w']
                if not self.armed:return finish('dry_run','Лише розрахунок: фізичний запис контролером не активовано.')
                available=[row for row in rows if str(row['id']) in plan['available_ids']]
                if not any(row['checked_at']>state.get('seen',{}).get(str(row['id']),-1) for row in available):
                    return finish('waiting','Очікування нових показників; команди не повторюються.')
                state['estimates']=plan['estimates']
                state['seen']={str(row['id']):row['checked_at'] for row in available}
                reductions=[];increases=[]
                for row in available:
                    ident=str(row['id']);current=row.get('effective_percent',row['power_limit_percent'])
                    if current is None:continue
                    desired=plan['percent'][ident]
                    if desired<current:reductions.append((ident,desired))
                    elif desired>current:increases.append((ident,desired))
                # Finish ALL reductions, then wait for FC04 in a later monitor cycle.
                changes=reductions or increases
                if not changes:return finish('active','Ліміти підтверджені читанням; змін не потрібно.')
                if not reductions:
                    current_sum=sum((row.get('effective_percent',row['power_limit_percent'])
                        if row in available else 100) or 0 for row in rows)
                    increase=sum(value-by_id[ident].get('effective_percent',by_id[ident]['power_limit_percent']) for ident,value in increases)
                    if (current_sum+increase)*config['rated_w']>config['target_w']*100:
                        return finish('paused','Недостатній підтверджений запас для підвищення. Нових команд немає.')
                for ident,percent in changes:
                    if self.stop_event.is_set():break
                    with Session(self.engine) as session:
                        user=self._authorized(session,config)
                        inv=session.get(Inverter,int(ident))
                        target=next(row for row in config['targets'] if str(row['id'])==ident)
                        transport=dict(port=target['port'],device_id=target['device_id'])
                        inverter_controls.plan(inv,transport,'limit',percent)
                        session.expunge(user);session.expunge(inv)
                    attempt=dict(id=ident,percent=percent,at=self.clock())
                    state.setdefault('pending',[]).append(attempt)
                    state.update(phase='awaiting_readback',reason='Команду підготовлено; очікується підтвердження читанням.')
                    budget.save(config)  # Durable no-retry marker BEFORE I/O.
                    request_id='auto:'+secrets.token_hex(16)
                    with Session(self.engine) as session,session.begin():
                        session.add(CommandAudit(request_id=request_id,source='automation',user_id=user.id,
                            username=user.username,occurred_at=self.clock(),action='limit',targets=[target],
                            result='processing',details=[dict(percent=percent,target_w=config['target_w'])]))
                    try:
                        with budget.automatic_command(config['revision'],inv.id):
                            details=execute_devices(user,'limit',[inv],percent,writes_enabled=True,source='web',
                                                    transports={inv.id:transport})
                    except AssertionError:raise  # Offline test guard must never be swallowed.
                    except ControlBlocked as error:
                        details=[dict(outcome='blocked',message=str(error),id=inv.id,name=inv.name)]
                    except Exception:
                        details=[dict(outcome='unknown',message='Результат невідомий; повторення немає.',id=inv.id,name=inv.name)]
                    attempt['at']=self.clock()  # Accept only FC04 newer than completion, not request start.
                    result=details[0]['outcome']
                    deferred=(result=='unconfirmed' and details[0].get('message')==inverter_controls.BUSY_MESSAGE)
                    with Session(self.engine) as session,session.begin():
                        session.execute(update(CommandAudit).where(CommandAudit.request_id==request_id)
                                        .values(result='deferred' if deferred else result,details=details,occurred_at=self.clock()))
                    if deferred:
                        # Endpoint lock was not acquired: no request was sent.
                        # Fresh telemetry may schedule a NEW attempt next cycle.
                        state['pending'].remove(attempt)
                        return finish('deferred','Пристрій опитує монітор. Запис не виконувався; очікування нового циклу даних.')
                    if result!='register_confirmed':
                        return finish('awaiting_readback','Результат запису не підтверджено. Повторення немає; очікується FC04.')
                return finish('awaiting_readback','Записи прийнято; очікування нового FC04 перед наступними змінами.')
            except ValueError as error:
                return finish('paused',str(error))

    def run(self,once=False):
        with resource_lock('power-controller:'+budget.database_key(self.engine)):
            while not self.stop_event.is_set():
                started=time.monotonic()
                try:self.run_cycle()
                except AssertionError:raise
                except OperationBusy:pass
                except Exception as error:log.error('Power controller cycle failed: %s',type(error).__name__)
                if once:return
                self.stop_event.wait(max(1,budget.INTERVAL-(time.monotonic()-started)))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-uri',required=True)
    parser.add_argument('--enable-writes',action='store_true',help='Owner-approved autonomous writes; omit for calculation only')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    url=database_url(args.database_uri)
    if not Path(url.database).is_file():raise ValueError('Existing SQLite database required')
    engine=create_engine(url,connect_args={'timeout':5})
    worker=PowerController(engine,enable_writes=args.enable_writes)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:worker.stop_event.set())
    try:worker.run(once=args.once)
    finally:engine.dispose()


if __name__=='__main__':main()
