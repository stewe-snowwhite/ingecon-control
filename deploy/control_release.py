"""Read-only control-release validation: local HTTP and Telegram getMe only."""
import argparse
import json
from pathlib import Path
import urllib.request

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from deploy.check_portal import http_check, telegram_check
from deploy.discovery_release import ROOT, DATABASE, verify_protected
from configuration import read_secret_file
import inverter_controls
from modbus_safety import writes_locked


def check(manifest):
    from app import create_app
    from models import User, Inverter
    assert inverter_controls.enabled(), 'Owner authorization is missing'
    assert writes_locked(), 'Legacy/raw write path must remain blocked'
    engine = create_engine('sqlite:///file:'+str(DATABASE)+'?mode=ro&uri=true')
    try:
        with Session(engine) as session:
            user = session.scalar(select(User).where(User.active.is_(True),User.role=='Administrator'))
            assert user is not None
            payload = dict(user_id=user.id,session_version=user.session_version)
            count = len(session.scalars(select(Inverter.id)).all())
        application = create_app(dict(SECRET_KEY=read_secret_file(ROOT/'instance/production/web-secret.key'),
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',SESSION_COOKIE_SECURE=False))
        cookie = application.session_interface.get_signing_serializer(application).dumps(payload)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for base in ('http://127.0.0.1:5000','http://127.0.0.1:80'):
            http_check(base)
            for path, marker in (('/', 'data-writes="true"'),
                ('/settings/modbus/control','HR1000=6/5'), ('/add','Автоматичний пошук')):
                query=urllib.request.Request(base+path,headers={'Cookie':'session='+cookie})
                with opener.open(query,timeout=10) as response:
                    assert response.status==200 and marker in response.read().decode()
            query=urllib.request.Request(base+'/api/statuses',headers={'Cookie':'session='+cookie})
            with opener.open(query,timeout=10) as response:
                data=json.load(response)
            assert data['monitor']['alive'] and data['counts']['total']==count
        telegram_check()  # No getUpdates, messages or command confirmations.
        verify_protected(manifest)
        print('CONTROL_RELEASE_READY: HTTP/auth/CSRF/inventory/heartbeat/Telegram OK; no inverter commands sent.')
    finally:
        engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args()
    try:
        check(json.loads(args.manifest.read_text()))
    except Exception:
        raise SystemExit('Control readiness check failed; credentials omitted.') from None
