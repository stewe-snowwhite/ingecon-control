"""Explicit TASK-08 CLI: isolated initialization, simulator and monitor."""
import argparse
import logging
import signal

from sqlalchemy import select

from demo_monitor_store import (DIRECTORY, HOST, PORT, DEVICE, NAME, DemoStore, Sample, event)
from demo_modbus import decode, install_guard, Simulator
from models import Inverter
from monitor import MonitorWorker
from monitoring import Target, NetworkProbes, ProbeResult


class DemoTarget(Target):
    def validate(self):
        if (self.inverter_id,self.host,self.port,self.device_id,self.read_address,self.read_count,self.profile)!=(
                1,HOST,PORT,DEVICE,0,8,'demo_task08'):
            raise ValueError('Only the fixed TASK-08 loopback target is allowed')


def target_for_demo(inv, config):
    if config is None:
        return DemoTarget(inv.id,inv.ip_address)
    return DemoTarget(inv.id,inv.ip_address,config.port,config.device_id,
                      config.read_address,config.read_count,config.profile)


class DemoProbes(NetworkProbes):
    def icmp(self, target):
        return ProbeResult(None,error='demo_icmp_not_applicable')

    def modbus(self, target):
        result = super().modbus(target)
        if result.ok:
            try: decode(result.registers)
            except ValueError: return ProbeResult(False,result.latency_ms,'modbus_invalid_response')
        return result


def observed(session, inverter, observation, status):
    sample = session.get(Sample,1)
    if sample.availability != status.state:
        event(session,'availability',status.state,'Спільний механізм моніторингу: '+(status.error_reason or 'перевірка успішна'),
              previous=sample.availability,notify=status.state in ('Online','Offline'),now=observation.checked_at)
        sample.availability = status.state
    if observation.modbus.ok:
        values = decode(observation.modbus.registers)
        equipment = values.pop('equipment')
        if equipment != sample.equipment:
            event(session,'equipment',equipment,'Результат читання тестових регістрів.',
                  previous=sample.equipment,notify=equipment=='Alarm' or sample.equipment=='Alarm',now=observation.checked_at)
        sample.equipment,sample.values,sample.measured_at = equipment,values,observation.checked_at


class DemoWorker(MonitorWorker):
    def __init__(self, store, probes=None, interval=5, **kwargs):
        self.store = store
        super().__init__(store.engine,probes=probes or DemoProbes(timeout=0.3),interval=interval,
                         target_factory=target_for_demo,observation_handler=observed,**kwargs)

    def _check_schema(self):
        # The real worker rejects demo_metadata; this worker accepts only TASK-08.
        verified = DemoStore(self.store.directory)
        verified.close()
        with self.store.sessions() as session:
            inventory = session.scalars(select(Inverter)).all()
            if len(inventory)!=1 or inventory[0].name!=NAME:
                raise ValueError('Demo inventory was modified; refusing polling')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','simulator','monitor','status'))
    parser.add_argument('--directory',default=str(DIRECTORY))
    parser.add_argument('--interval',type=float,default=5)
    args = parser.parse_args(argv)
    store = DemoStore(args.directory,initialize=args.command=='init')
    try:
        if args.command=='init':
            print('TASK-08: окрему БД підготовлено. Реальні пристрої та старе demo не змінено.')
        elif args.command=='status':
            import json
            print(json.dumps(store.snapshot(),ensure_ascii=False,indent=2))
        elif args.command=='simulator':
            install_guard(simulator=True)
            with Simulator(store) as server:
                signal.signal(signal.SIGTERM,lambda *args:(_ for _ in ()).throw(KeyboardInterrupt()))
                print('[DEMO] Modbus TCP: 127.0.0.1:15020, FC03 only, Ctrl+C для зупинки.',flush=True)
                server.serve_forever(poll_interval=0.2)
        else:
            # Irreversible process guard BEFORE any network work. No ping subprocess.
            install_guard()
            logging.getLogger('pymodbus').setLevel(logging.CRITICAL)
            worker = DemoWorker(store,interval=args.interval)
            for sig in (signal.SIGTERM,signal.SIGINT):
                signal.signal(sig,lambda *args:worker.stop_event.set())
            print('[DEMO] Монітор: лише loopback-симулятор; Ctrl+C для зупинки.',flush=True)
            worker.run()
    finally:
        store.close()


if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: pass
