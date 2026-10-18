"""Removal regressions; all authentication/command tests still use shared mocks."""
import pytest
from sqlalchemy import text
from models import db, User
from monitor import MonitorWorker
from tests.helpers import post


@pytest.mark.parametrize('role',['Administrator','Operator','Viewer',None])
def test_removed_urls_are_unavailable_for_every_role(web,modbus,role):
    module,client = web
    if role:
        db.session.get(User,1).role = role
        db.session.commit()
    else:
        with client.session_transaction() as session: session.clear()
    for path in ('/demo','/api/demo/status'):
        assert client.get(path).status_code==404
    for path in ('/demo/init','/demo/scenario','/demo/notifications'):
        assert post(client,path,{'scenario':'normal','load':'50','action':'enable'}).status_code==404
    assert modbus[1]==[]


def test_navigation_configuration_and_bundles_have_no_removed_feature(web):
    module,client = web
    html = client.get('/').get_data(as_text=True)
    assert '/demo' not in html and 'monitor_demo' not in html
    assert 'DEMO_MODE' not in module.app.config and 'MONITOR_DEMO_AVAILABLE' not in module.app.config
    assert not any('demo' in rule.rule for rule in module.app.url_map.iter_rules())
    assert client.get('/static/monitor_demo.js').status_code==404
    assert module.app.config['MODBUS_WRITES_ENABLED'] is False


def test_monitor_keeps_quarantine_for_archived_test_databases(web,modbus):
    module,_ = web
    db.session.execute(text('CREATE TABLE demo_metadata (key TEXT, value TEXT)'))
    db.session.commit()
    with pytest.raises(RuntimeError,match='Archived test database'):
        MonitorWorker(db.engine)._check_schema()
    assert modbus[1]==[]
