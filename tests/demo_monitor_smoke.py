"""Explicit real TCP smoke test: temporary TASK-08 DB and guarded loopback ONLY.

Not collected by pytest. No production users, tokens, databases or services.
"""
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

from demo_monitor import DemoWorker
from demo_monitor_store import DemoStore
from demo_modbus import install_guard


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='ingecon-task08-smoke-') as temporary:
        store = DemoStore(Path(temporary)/'isolated',initialize=True)
        process = subprocess.Popen([sys.executable,str(root/'demo_monitor.py'),'simulator','--directory',str(store.directory)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                if not selector.select(timeout=15) or '[DEMO] Modbus TCP:' not in process.stdout.readline():
                    details = process.stderr.read()[-2000:] if process.poll() is not None else 'readiness timeout'
                    raise RuntimeError('Our simulator did not become ready; no endpoint was polled: '+details)
            # After the owned child is ready, no other process or remote transport
            # can be started from this process. Guard cannot be removed.
            install_guard()
            worker = DemoWorker(store)
            worker._check_schema()
            def cycle():
                worker.run_cycle()
                return store.snapshot()
            assert cycle()['inverters'][0]['state']=='Unknown'
            assert cycle()['inverters'][0]['state']=='Online'
            store.change('fault','70','smoke')
            assert cycle()['equipment']=='Alarm'
            store.change('offline','70','smoke')
            assert [cycle()['inverters'][0]['state'] for _ in range(3)]==['Degraded','Degraded','Offline']
            store.change('recovery','80','smoke')
            time.sleep(1.2)  # Let bounded simulated timeouts finish; no real peer.
            assert cycle()['inverters'][0]['state']=='Degraded'
            restored = cycle()
            assert restored['inverters'][0]['state']=='Online'
            assert restored['measurements']['power_w']==4000
            store.change('stopped','80','smoke')
            assert cycle()['equipment']=='Stopped'
            worker._flush([],stopped=True)
            assert not store.snapshot()['monitor']['alive']
            print('PASS: real FC03 over guarded loopback; Online, Alarm, Offline, recovery, Stopped, heartbeat. No writes or Telegram traffic.')
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Only our explicitly created temporary child, never production.
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            store.close()


if __name__=='__main__': main()
