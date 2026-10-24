"""Bounded read-only polling with synthetic endpoints, no network or database I/O."""
import threading

import pytest

from monitor import MonitorWorker, parser
from monitoring import Observation, ProbeResult, Target
from models import Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, StateEvent
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_monitoring import storage, observation


def test_26_devices_are_polled_once_with_at_most_eight_independent_endpoints():
    guard=threading.Lock();barrier=threading.Barrier(8,timeout=5)
    active={};calls=[];peak=[0];count=[0]
    targets=[Target(n,'192.0.2.'+str(n if n!=2 else 1),device_id=n) for n in range(1,27)]
    class Probes:
        def poll(self,target):
            key=(target.host,target.port)
            with guard:
                active[key]=active.get(key,0)+1
                assert active[key]==1,'Parallel operation on the same gateway'
                count[0]+=1;initial=count[0]<=8
                peak[0]=max(peak[0],sum(active.values()))
                calls.append(target.inverter_id)
            if initial:barrier.wait()
            with guard:active[key]-=1
            return Observation(target,1000,ProbeResult(False),ProbeResult(True),ProbeResult(True))
    worker=MonitorWorker(None,Probes(),workers=8)
    observations=list(worker._observations(targets))
    assert sorted(calls)==list(range(1,27))
    assert sorted(o.target.inverter_id for o in observations)==list(range(1,27))
    assert peak[0]==8 and all(value==0 for value in active.values())


@pytest.mark.parametrize('workers',[0,9,-1,True,1.5,'8'])
def test_invalid_concurrency_is_rejected_before_probes(workers):
    with pytest.raises(ValueError):MonitorWorker(None,workers=workers)


def test_stop_before_parallel_cycle_does_not_probe():
    class Probes:
        def poll(self,_):raise AssertionError('No probe after cancellation')
    worker=MonitorWorker(None,Probes(),workers=8)
    worker.stop_event.set()
    assert list(worker._observations([Target(1,'192.0.2.1')]))==[]


def test_parallel_probe_errors_are_not_silenced():
    class Probes:
        def poll(self,_):raise AssertionError('Offline guard')
    with pytest.raises(AssertionError,match='Offline guard'):
        list(MonitorWorker(None,Probes(),workers=8)._observations([Target(1,'192.0.2.1')]))


def test_default_monitor_stays_sequential_unless_explicitly_enabled():
    assert parser().parse_args([]).workers==1
    assert parser().parse_args(['--workers','8']).workers==8


def test_parallel_cycle_persists_26_statuses_and_hysteresis_on_the_main_thread(storage):
    with Session(storage) as session,session.begin():
        for number in range(2,27):
            session.add(Inverter(id=number,name='Mock '+str(number),group='A',ip_address='192.0.2.'+str(number)))
            session.add(MonitorConfig(inverter_id=number,read_address=1000,read_count=2,profile='legacy_ingeteam'))
    clock=[1000];threads=[]
    class Probes:
        def poll(self,target):
            threads.append(threading.current_thread().name)
            return observation(clock[0],target=target)
    worker=MonitorWorker(storage,Probes(),workers=8,clock=lambda:clock[0])
    worker.run_cycle();clock[0]+=30;worker.run_cycle()
    with Session(storage) as session:
        rows=session.scalars(select(InverterStatus)).all()
        assert len(rows)==26 and all(row.state=='Online' for row in rows)
        assert len(session.scalars(select(StateEvent)).all())==26
        assert session.get(MonitorHeartbeat,1).last_seen==1030
    assert len(threads)==52 and all(name.startswith('monitor') for name in threads)
