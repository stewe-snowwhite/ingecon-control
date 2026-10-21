"""100TL tests use synthetic SQLite and the shared offline Modbus mock only."""
import sqlite3
import time

import pytest
from sqlalchemy import create_engine

from input_registers import telemetry, operating_state_label
from models import Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat
from monitoring import Target, NetworkProbes, Observation, ProbeResult, apply_observation, target_for, current_statuses
from telemetry_schema import prepare, ready
from tests.helpers import post


def words(power=6298, state=2, limit=100, reason=1):
    values = [0]*19
    values[0], values[9], values[16], values[17] = state, power, limit, reason
    return tuple(values)


def test_model_profile_is_shared_across_ips_and_preserves_transport():
    for address in ('192.0.2.1', '192.0.2.45'):
        inv = Inverter(id=7, ip_address=address, model='ingecon_sun_100tl')
        config = MonitorConfig(port=1502, device_id=17, profile='generic', read_address=None)
        target = target_for(inv, config)
        assert target == Target(7, address, 1502, 17, 20, 16, 'ingecon_100tl_input')
        target.validate()
        assert target_for(inv, None).profile == 'ingecon_100tl_input'
    assert target_for(Inverter(id=8, ip_address='192.0.2.2', model='unknown'), None).read_address is None


@pytest.mark.parametrize('raw,watts', [(6298,62980), (0,0), (65534,-20), (32767,327670), (32768,-327680)])
def test_power_scaling_and_signed_words(raw, watts):
    assert telemetry(words(power=raw))['active_power_w'] == watts
    assert telemetry(words(power=raw))['active_power_raw'] == raw


@pytest.mark.parametrize('values', [(), words()[:-1], words()+ (0,), (False,)+words()[1:], (-1,)+words()[1:], (65536,)+words()[1:]])
def test_incomplete_or_invalid_blocks_are_rejected(values):
    with pytest.raises(ValueError): telemetry(values)


def test_unavailable_values_do_not_turn_into_fake_measurements():
    result = telemetry(words(state=65535, limit=65535, reason=65535))
    assert result['operating_state_code'] is None
    assert result['power_limit_percent'] is None
    assert result['limit_reason_code'] is None
    assert operating_state_label(2) == 'Підключений до мережі'
    assert operating_state_label(555) == 'Код стану 555'


def test_profile_reads_two_fc04_blocks_and_never_calls_writes(modbus):
    module, clients = modbus
    result = NetworkProbes().modbus(Target(1,'192.0.2.1',502,1,20,16,'ingecon_100tl_input'))
    assert result.ok and len(result.registers) == 19
    assert clients[0].calls == [('connect',),('read_input',20,16,1),('read_input',52,3,1)]


def seed(module):
    inv = Inverter(name='100TL', group='A', ip_address='192.0.2.1', model='ingecon_sun_100tl')
    module.db.session.add(inv); module.db.session.flush()
    module.db.session.add(MonitorHeartbeat(id=1,owner='mock',started_at=1000,last_seen=1000,interval_seconds=30,stopped=False))
    return inv


def observed(inv, now, ok=True):
    return Observation(target_for(inv,None),now,ProbeResult(False),ProbeResult(True),
                       ProbeResult(ok,5, None if ok else 'modbus_timeout',words() if ok else ()))


def test_persisted_telemetry_separate_from_availability_and_hysteresis(web):
    module,_ = web
    inv = seed(module)
    first = apply_observation(module.db.session,inv,observed(inv,1000),90)
    assert first.state == 'Unknown' and first.operating_state_code == 2
    second = apply_observation(module.db.session,inv,observed(inv,1030),90)
    module.db.session.commit()
    assert second.state == 'Online' and second.active_power_w == 62980
    snapshot = current_statuses(module.db.session,now=1030)['inverters'][0]
    assert snapshot['state'] == 'Online'
    assert snapshot['operating_state_label'] == 'Підключений до мережі'
    assert snapshot['limit_reason_code'] == 1 and snapshot['power_limit_percent'] == 100
    stale = current_statuses(module.db.session,now=1200)['inverters'][0]
    assert stale['state'] == 'Unknown'
    assert all(stale[key] is None for key in ('operating_state_code','operating_state_label','active_power_w','active_power_raw','limit_reason_code','power_limit_percent'))
    for now in (1060,1090,1120):
        row = apply_observation(module.db.session,inv,observed(inv,now,False),90)
        assert row.active_power_w is None and row.operating_state_code is None
        assert row.state == ('Offline' if now == 1120 else 'Degraded')


@pytest.mark.parametrize('role', ['Administrator','Operator','Viewer'])
def test_html_api_telemetry_is_readonly_for_all_roles(web,modbus,role):
    module,client = web
    module.db.session.get(module.User,1).role = role
    inv = seed(module)
    now = time.time()
    module.db.session.get(MonitorHeartbeat,1).last_seen = now
    apply_observation(module.db.session,inv,observed(inv,now-1),90)
    apply_observation(module.db.session,inv,observed(inv,now),90)
    module.db.session.commit()
    data = client.get('/api/statuses').json['inverters'][0]
    assert data['active_power_raw'] == 6298 and data['active_power_w'] == 62980
    html = client.get('/').get_data(as_text=True)
    for value in ('Підключений до мережі', '62,98 кВт', 'Без обмеження (1)', '100%', 'Доступність','Робочий стан'):
        assert value in html
    assert modbus[1] == []


def test_crud_model_selection_validated_and_retained(web):
    module,client = web
    fields=dict(name='100TL',group='A',ip='192.0.2.15',model='ingecon_sun_100tl',confirm_unverified='yes')
    assert post(client,'/add',fields).status_code == 302
    inv=module.db.session.scalar(module.db.select(Inverter))
    assert target_for(inv,module.db.session.get(MonitorConfig,inv.id)).profile == 'ingecon_100tl_input'
    assert post(client,f'/edit/{inv.id}',dict(name='100TL',group='B',ip='192.0.2.16')).status_code == 302
    assert inv.model == 'ingecon_sun_100tl'
    assert post(client,f'/edit/{inv.id}',dict(fields,model='anything')).status_code == 400
    assert 'ingecon_sun_100tl' in client.get(f'/edit/{inv.id}').get_data(as_text=True)


def old_database(path):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE inverter (id INTEGER PRIMARY KEY,name TEXT,ip_address TEXT,password TEXT)')
        c.execute("INSERT INTO inverter VALUES (1,'Keep','192.0.2.1','synthetic-secret')")
        c.execute("INSERT INTO inverter VALUES (2,'Unknown','192.0.2.2','synthetic-other')")
        c.execute('CREATE TABLE inverter_status (inverter_id INTEGER PRIMARY KEY, state TEXT, power_limit_percent REAL)')
        c.execute("INSERT INTO inverter_status VALUES (1,'Unknown',NULL)")
        c.execute('CREATE TABLE monitor_config (inverter_id INTEGER,port INTEGER,device_id INTEGER,profile TEXT)')
        c.execute("INSERT INTO monitor_config VALUES (1,1502,17,'generic')")


def test_additive_migration_backup_restore_idempotence_and_no_transport_changes(tmp_path):
    path=tmp_path/'production.sqlite'; old_database(path)
    backup=prepare(path,tmp_path/'backup',[1])
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as c:
        assert 'model' not in {r[1] for r in c.execute('PRAGMA table_info(inverter)')}
    assert backup.with_suffix('.restore-verified.sqlite').exists()
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT model FROM inverter ORDER BY id').fetchall()==[('ingecon_sun_100tl',),('unknown',)]
        assert c.execute('SELECT name,password FROM inverter WHERE id=1').fetchone()==('Keep','synthetic-secret')
        assert c.execute('SELECT port,device_id,profile FROM monitor_config').fetchone()==(1502,17,'generic')
    prepare(path,tmp_path/'backup',[1])
    engine=create_engine('sqlite:///'+str(path))
    assert ready(engine)
    engine.dispose()


def test_failed_restore_verification_prevents_any_ddl(tmp_path,monkeypatch):
    import telemetry_schema
    path=tmp_path/'production.sqlite'; old_database(path); original=path.read_bytes()
    def fail(*args): raise ValueError('simulated bad backup')
    monkeypatch.setattr(telemetry_schema,'check_copy',fail)
    with pytest.raises(ValueError): prepare(path,tmp_path/'backup',[1])
    assert path.read_bytes()==original


def test_migration_refuses_unknown_ids_before_changes(tmp_path):
    path=tmp_path/'production.sqlite'; old_database(path); original=path.read_bytes()
    with pytest.raises(ValueError): prepare(path,tmp_path/'backup',[99])
    assert path.read_bytes()==original and not (tmp_path/'backup').exists()


def test_incompatible_schema_rolls_back_all_ddl_and_keeps_verified_backup(tmp_path):
    path=tmp_path/'production.sqlite'; old_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE inverter_status ADD COLUMN active_power_w TEXT')
    with pytest.raises(ValueError): prepare(path,tmp_path/'backup',[1])
    with sqlite3.connect(path) as connection:
        assert 'model' not in {r[1] for r in connection.execute('PRAGMA table_info(inverter)')}
        assert 'operating_state_code' not in {r[1] for r in connection.execute('PRAGMA table_info(inverter_status)')}
    assert len(list((tmp_path/'backup').glob('*.restore-verified.sqlite'))) == 1


def test_cli_cannot_silently_override_model_selected_profile(web):
    module,_=web
    inv=seed(module); module.db.session.commit()
    result=module.app.test_cli_runner().invoke(args=['configure-monitor',str(inv.id),'--read-address','1000','--profile','generic'])
    assert result.exit_code != 0
    assert module.db.session.get(MonitorConfig,inv.id) is None
