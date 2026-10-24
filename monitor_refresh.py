"""On-demand read requests via existing SQLite fields. No device transport calls."""
from dataclasses import dataclass
import math

from sqlalchemy import select, update

from models import Inverter, MonitorConfig
from monitoring import current_statuses, target_for

MIN_REQUEST_INTERVAL = 5  # Shared monitor bound, independent of the number of callers.


@dataclass(frozen=True)
class RefreshTicket:
    requested_at: float
    targets: dict


def capture(session,now):
    snapshot=dict(current_statuses(session,now=now),generated_at=now)
    targets={inv.id:target_for(inv,config).key for inv,config in session.execute(
        select(Inverter,MonitorConfig).outerjoin(MonitorConfig,Inverter.id==MonitorConfig.inverter_id))}
    return snapshot,targets


def progress(session,ticket,now):
    snapshot,targets=capture(session,now)
    completed=sum(not row['stale'] and type(row['checked_at']) in (int,float) and
                  math.isfinite(row['checked_at']) and row['checked_at']>=ticket.requested_at
                  for row in snapshot['inverters'])
    if targets!=ticket.targets:state='inventory_changed'
    elif not targets:state='empty'
    elif not snapshot['monitor']['alive']:state='monitor_unavailable'
    elif completed==len(targets):state='refreshed'
    else:state='pending'
    return dict(state=state,snapshot=snapshot,completed=completed,total=len(targets))


def request(session,now):
    _,targets=capture(session,now)
    ticket=RefreshTicket(now,targets)
    result=progress(session,ticket,now)
    if result['state']!='pending':return ticket,result
    # Preserve every existing transport/profile; only the request marker changes.
    for inv,config in session.execute(select(Inverter,MonitorConfig).outerjoin(
            MonitorConfig,Inverter.id==MonitorConfig.inverter_id)):
        if config is None:
            target=target_for(inv,None)
            session.add(MonitorConfig(inverter_id=inv.id,port=target.port,device_id=target.device_id,
                read_address=target.read_address,read_count=target.read_count,profile=target.profile))
    session.flush()
    session.execute(update(MonitorConfig).where(MonitorConfig.inverter_id.in_(targets)).values(requested_at=now))
    return ticket,result


def pending(session):
    return session.scalar(select(MonitorConfig.inverter_id).join(Inverter,Inverter.id==MonitorConfig.inverter_id)
        .where(MonitorConfig.requested_at.is_not(None)).limit(1)) is not None
