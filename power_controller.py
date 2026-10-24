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
from models import CommandAudit, Inverter, InverterStatus, User
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

    def _history_and_upgrade(self, session, config):
        state=config['state']
        history=state.setdefault('history',{})
        # Bootstrap outage reserves from stored LAST KNOWN telemetry, never as live power.
        # Target identity must match; stale data from another endpoint is excluded.
        for target in config['targets']:
            status=session.get(InverterStatus,target['id'])
            if (str(target['id']) not in history and status and status.target_key==target['key'] and
                status.checked_at is not None and budget.number(status.active_power_w)):
                budget.remember(history,dict(id=target['id'],active_power_w=status.active_power_w,
                    power_limit_percent=status.power_limit_percent,checked_at=status.checked_at))
        if config['version']==1:
            config.update(version=2,algorithm=budget.ALGORITHM)
            config.pop('rated_w',None)
            state.update(not_before=self.clock(),seen={})
            state.pop('estimates',None)
            state.pop('desired',None)
            # Preserve pending/sent/fault evidence across the algorithm change.
            session.add(CommandAudit(request_id='feedback:'+config['revision'],source='automation',
                user_id=config['owner_id'],username=session.get(User,config['owner_id']).username,
                occurred_at=self.clock(),action='budget_algorithm_upgrade',targets=config['targets'],
                result='configured',details=[dict(algorithm=budget.ALGORITHM,target_w=config['target_w'])]))
            session.commit()

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
                    self._history_and_upgrade(session,config)
                    snapshot=current_statuses(session,now=now)
                rows=snapshot['inverters']
                for row in rows:
                    if row['checked_at'] is None or not 0<=now-row['checked_at']<=budget.INTERVAL*2:
                        row['stale']=True
                by_id={str(row['id']):row for row in rows}
                pending=[]
                faults=state.setdefault('faults',{})
                for attempt in state.get('pending',[]):
                    ident=attempt['id'];row=by_id[ident]
                    if not budget.fresh(row) or row['checked_at']<=attempt['at']:
                        pending.append(attempt);continue
                    observed=row['power_limit_percent']
                    if budget.number(observed) and attempt['percent']-1<=observed<=attempt['percent']:
                        state.setdefault('sent',{})[ident]=attempt
                    else:
                        faults[ident]='FC04 не підтвердив запис; потрібне нове підтвердження оператора.'
                        pending.append(attempt)
                state['pending']=pending
                for ident,attempt in state.get('sent',{}).items():
                    row=by_id[ident];observed=row['power_limit_percent']
                    if (budget.fresh(row) and budget.number(observed) and
                        attempt['percent']-1<=observed<=attempt['percent'] and row['checked_at']>attempt['at']):
                        row['effective_percent']=attempt['percent']
                blocked=set(faults)|{p['id'] for p in pending}
                plan=budget.allocation(config['target_w'],rows,state['history'],blocked=blocked)
                for key in ('history','reserved_w','offline_reserved_w','missing_ids','unknown_ids',
                            'estimated_total_w','actual_w','known','assigned_w'):
                    state[key]=plan[key]
                state['desired']=plan['percent']
                if not snapshot['monitor']['alive']:
                    return finish('paused','Немає актуального моніторингу. Нових команд немає.')
                if state.get('fault'):
                    return finish('paused','Попередній запис не підтверджено. Перевірте журнал і підтвердьте ціль заново.')
                if not self.armed:return finish('dry_run','Лише розрахунок: фізичний запис контролером не активовано.')
                available=[row for row in rows if str(row['id']) in plan['available_ids']]
                eligible=[row for row in available if row['checked_at']>state.get('not_before',-1) and
                          row['checked_at']>state.get('seen',{}).get(str(row['id']),-1)]
                online_pending=any(budget.fresh(by_id[p['id']]) for p in pending)
                reductions=[];increases=[]
                for row in eligible:
                    ident=str(row['id']);current=row.get('effective_percent',row['power_limit_percent'])
                    desired=plan['percent'][ident]
                    if desired<current:reductions.append((ident,desired))
                    elif desired>current:increases.append((ident,desired))
                # Lost/pending devices retain their reserve but do not block other reductions.
                # Reachable in-flight commands must settle before any increases.
                changes=reductions or ([] if online_pending else increases)
                state.setdefault('seen',{}).update({str(row['id']):row['checked_at'] for row in eligible})
                if not changes:
                    if online_pending:return finish('awaiting_readback','Очікування нового FC04 і виміряної генерації після запису.')
                    if plan['missing_ids'] or faults or plan['fixed_ids']:
                        return finish('partial',
                            ('Доступні інвертори регулюються з резервом для недоступних. ' if plan['missing_ids'] else
                             'Частина пристроїв тимчасово некерована; їхня виміряна потужність врахована. ')
                            +('Немає даних для оцінки частини резерву; підвищення заблоковані. ' if plan['unknown_ids'] else '')
                            +('Загальна генерація підтверджена лише частково; резерв є оцінкою.' if plan['missing_ids'] else
                              'Потрібна перевірка непідтверджених команд.'))
                    if plan['within_target']:
                        return finish('active','Виміряна генерація в межах цілі; нових команд не потрібно.')
                    if not eligible:return finish('waiting','Очікування нових показників; команди не повторюються.')
                    if available and all(plan['percent'][str(r['id'])]==100 for r in available):
                        return finish('underproduction','Доступна генерація нижча за ціль; усі доступні ліміти 100%.')
                    if available and all(plan['percent'][str(r['id'])]==1 for r in available) and plan['actual_w']>config['target_w']:
                        return finish('limit_unmet','На мінімальному ліміті генерація перевищує ціль; обмеження не підтверджено.')
                    return finish('waiting','Очікування нового виміру для наступного кроку регулювання.')
                for ident,percent in changes:
                    if self.stop_event.is_set():break
                    if not 0<=self.clock()-by_id[ident]['checked_at']<=budget.INTERVAL*2:
                        state.update(actual_w=None,known=0)
                        return finish('waiting','Показники застаріли під час виконання; очікування нового опитування.')
                    with Session(self.engine) as session:
                        user=self._authorized(session,config)
                        inv=session.get(Inverter,int(ident))
                        target=next(row for row in config['targets'] if str(row['id'])==ident)
                        live=session.get(InverterStatus,inv.id)
                        if (not live or live.target_key!=target['key'] or live.modbus_ok is not True or
                            live.active_power_w is None or live.power_limit_percent!=by_id[ident]['power_limit_percent']):
                            continue  # Lost/changed after planning: let the next measured cycle reallocate.
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
