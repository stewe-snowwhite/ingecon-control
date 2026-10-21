"""Synthetic netlink, SQLite and Modbus only; never scans a real network."""
from dataclasses import asdict
import ipaddress
import json
import sqlite3
import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import discovery as d
from discovery_worker import Worker
from models import (db, User, Inverter, MonitorConfig, DiscoverySubnet, DiscoveryScan,
                    DiscoveryResult, CommandAudit, DISCOVERY_TABLES)
from tests.helpers import post

REAL_LOCAL_INVENTORY = d.local_inventory

LAN = [['enp1s0', '10.12.0.0/24', '10.12.0.1']]
LOCAL = d.Scope('10.12.0.0/30', 'enp1s0', 'local')
ROUTED = d.Scope('10.13.0.0/30', 'enp1s0', 'routed')


def direct(ip):
    return dict(dev='enp1s0', prefsrc='10.12.0.1')


def routed(ip):
    return dict(dev='enp1s0', prefsrc='10.12.0.1', gateway='10.12.0.254')


ROUTES = [dict(dst='10.13.0.0/24', **routed(None))]


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(d, 'local_inventory', lambda: LAN)
    monkeypatch.setattr(d, 'route_to', direct)
    return LAN


@pytest.mark.parametrize('cidr', ['0.0.0.0/0', '8.8.8.0/24', '100.64.0.0/24', '127.0.0.0/24',
    '169.254.0.0/24', '224.0.0.0/24', '10.0.0.0/8', '192.168.0.0/16', '10.0.0.1/24',
    '10.0.0.0/31', '10.0.0.1/32', '::1/128', 'garbage'])
def test_denied_ranges(cidr):
    with pytest.raises(ValueError):
        d.private_network(cidr)


def test_local_scope_excludes_own_ip_and_accepts_first_host_server(network):
    assert d.checked_scope(LOCAL.cidr) == LOCAL
    assert d.host_addresses(LOCAL.cidr, LAN) == ['10.12.0.2']


def test_routed_scope_needs_explicit_private_route(network):
    assert d.checked_scope(ROUTED.cidr, lookup=routed, routes=ROUTES) == ROUTED
    with pytest.raises(ValueError, match='default route'):
        d.checked_scope(ROUTED.cidr, lookup=routed, routes=[dict(dst='default', **routed(None))])


@pytest.mark.parametrize('route', [dict(dev='tailscale0', prefsrc='100.64.1.1'),
    dict(dev='tun0', prefsrc='10.12.0.1'), dict(dev='docker0', prefsrc='10.12.0.1'),
    dict(dev='enp1s0', prefsrc='10.12.0.1', gateway='8.8.8.8'),
    dict(dev='enp1s0', prefsrc='10.12.0.1', type='blackhole'),
    dict(dev='enp1s0', prefsrc='10.12.0.1', via='vpn'),
    dict(dev='enp1s0', prefsrc='10.12.0.1', flags=['linkdown'])])
def test_route_boundaries_fail_closed(network, route):
    with pytest.raises(ValueError):
        d.check_destination('10.12.0.2', LOCAL, lookup=lambda _: route)


def test_local_inventory_uses_existing_physical_interface_guard(monkeypatch):
    addresses = [dict(ifname='tailscale0', flags=['UP'], addr_info=[dict(local='100.84.37.60', scope='global')])]
    monkeypatch.setattr(d, 'inventory', lambda: (addresses, []))
    assert REAL_LOCAL_INVENTORY() == []


def profile_reader(calls):
    def read(request):
        calls.append(request)
        return dict(outcome='read', message='ok', registers=([0]*16 if request.address == 20 else [75, 0, 0]))
    return read


def run_probe(scope=LOCAL, mac='00:25:CA:2C:10:20', opened=True, reader=None):
    calls = []
    result = d.probe('10.12.0.2' if scope.mode == 'local' else '10.13.0.1', scope, 7,
        boundary=lambda *a: True, tcp=lambda ip: opened,
        mac_lookup=lambda *a: mac.lower() if mac else None, reader=reader or profile_reader(calls))
    return result, calls


def test_local_search_profile_is_not_unique_model():
    result, calls = run_probe()
    assert result['confidence'] == 'profile' and result['modbus_ok']
    assert 'унікальну модель не визначено' in result['reason']
    assert [(r.function, r.address, r.count, r.device_id) for r in calls] == [(4,20,16,7),(4,52,3,7)]


def test_routed_search_does_not_require_mac():
    result, calls = run_probe(ROUTED, mac=None)
    assert result['confidence'] == 'profile' and result['mac'] is None
    assert len(calls) == 2


def test_local_missing_mac_is_possible_and_never_bypasses_mac_filter():
    result, calls = run_probe(mac=None)
    assert result['confidence'] == 'possible' and not result['modbus_ok']
    assert calls == []


def test_foreign_mac_is_excluded():
    result, calls = run_probe(mac='00:11:22:33:44:55')
    assert result is None and calls == []


def test_unavailable_modbus_is_reported_for_matching_mac():
    result, calls = run_probe(opened=False)
    assert not result['tcp_ok'] and not result['modbus_ok'] and calls == []
    result, calls = run_probe(reader=lambda _: dict(outcome='unconfirmed', message='timeout', registers=[]))
    assert result['tcp_ok'] and not result['modbus_ok'] and 'timeout' in result['reason']


@pytest.mark.parametrize('words', [[0]*19, [65535]*19, [1]*7+[9999]+[1]*11])
def test_open_port_or_wrong_words_not_sufficient(words):
    assert not d.matches_profile(words)


def test_route_change_between_tcp_and_modbus_stops_reads():
    boundaries, reads = [], []
    def boundary(*args):
        boundaries.append(1)
        if len(boundaries) > 1:
            raise ValueError('route changed')
    with pytest.raises(ValueError):
        d.probe('10.12.0.2', LOCAL, 1, boundary=boundary, tcp=lambda _: True,
                mac_lookup=lambda *a: '00:25:ca:2c:11:22', reader=profile_reader(reads))
    assert reads == []


def test_mock_pymodbus_only_fc04_even_when_write_interlock_is_false(modbus):
    result = d.probe('10.12.0.2', LOCAL, 1, boundary=lambda *a: True,
        tcp=lambda _: True, mac_lookup=lambda *a: '00:25:ca:2c:11:22')
    assert result['modbus_ok']
    clients = modbus[1]
    assert all(client.closed for client in clients)
    assert [call for client in clients for call in client.calls if call[0].startswith('read')] == [('read_input',20,16,1),('read_input',52,3,1)]
    assert not any(call[0] == 'write' for client in clients for call in client.calls)


def approve(client, cidr=LOCAL.cidr):
    return post(client, '/add/discovery/subnets', dict(cidr=cidr, approve='yes'))


def queue(client, **data):
    return post(client, '/add/discovery/scans', dict(subnet_id='1', device_id='1', **data))


def test_embedded_page_and_manual_prefill_without_probe(web, network, monkeypatch):
    module, client = web
    def forbidden(*a): raise AssertionError('web must not scan')
    monkeypatch.setattr(d, 'probe', forbidden)
    html = client.get('/add').get_data(as_text=True)
    assert 'Автоматичний пошук' in html and 'Додати вручну' in html
    assert '/add/discovery' in html and '<input id="name"' not in html
    row = DiscoveryScan(user_id=1, user_version=1, subnets=[], device_id=1, total=1, created_at=time.time(), state='completed')
    db.session.add(row); db.session.flush()
    result = DiscoveryResult(scan_id=row.id, ip='10.12.0.2', mac=None, cidr=LOCAL.cidr, port=502, device_id=7,
                             tcp_ok=True, modbus_ok=True, confidence='profile', reason='test')
    db.session.add(result); db.session.commit()
    html = client.get(f'/add?mode=manual&candidate={result.id}').get_data(as_text=True)
    assert 'value="10.12.0.2"' in html and 'value="502"' in html and 'value="7"' in html
    assert Inverter.query.count() == 0


def test_approval_queue_progress_duplicates_and_cancel(web, network):
    module, client = web
    assert approve(client).status_code == 302
    assert queue(client).status_code == 302
    row = DiscoveryScan.query.one()
    assert row.state == 'queued' and row.total == 1
    assert queue(client).status_code == 409
    db.session.add(DiscoveryResult(scan_id=row.id, ip='10.12.0.2', mac=None, cidr=LOCAL.cidr, port=502, device_id=1,
        tcp_ok=True, modbus_ok=True, confidence='profile', reason='test'))
    db.session.add(Inverter(name='Existing', group='A', ip_address='10.12.0.2'))
    db.session.commit()
    data = client.get(f'/add/discovery/scans/{row.id}').json
    assert data['results'][0]['registered'] is True
    assert post(client, f'/add/discovery/scans/{row.id}/cancel').status_code == 302
    assert row.state == 'cancelled'
    assert CommandAudit.query.count() == 3


def test_only_approved_cidr_and_current_route_can_be_queued(web, network, monkeypatch):
    _, client = web
    assert queue(client).status_code == 400
    assert approve(client).status_code == 302
    monkeypatch.setattr(d, 'route_to', lambda _: dict(dev='tailscale0', prefsrc='10.12.0.1'))
    assert queue(client).status_code == 400
    assert DiscoveryScan.query.count() == 0


def test_subnet_delete_and_confirmation(web, network):
    _, client = web
    assert post(client, '/add/discovery/subnets', dict(cidr=LOCAL.cidr)).status_code == 400
    assert approve(client).status_code == 302
    assert approve(client).status_code == 400
    assert post(client, '/add/discovery/subnets/1/delete').status_code == 400
    assert post(client, '/add/discovery/subnets/1/delete', dict(confirm='yes')).status_code == 302
    assert DiscoverySubnet.query.count() == 0


@pytest.mark.parametrize('role', [None, 'Operator', 'Viewer'])
def test_all_discovery_endpoints_admin_only(web, network, role):
    module, client = web
    if role is None:
        client = module.app.test_client()
    else:
        user = User.query.one(); user.role = role; db.session.commit()
    expected = 401 if role is None else 403
    assert client.get('/add/discovery/scans/1').status_code == expected
    for path in ('/add/discovery/subnets', '/add/discovery/subnets/1/delete', '/add/discovery/scans', '/add/discovery/scans/1/cancel'):
        assert post(client, path).status_code == expected


@pytest.mark.parametrize('path', ['/add/discovery/subnets', '/add/discovery/subnets/1/delete', '/add/discovery/scans', '/add/discovery/scans/1/cancel'])
def test_discovery_csrf(web, path):
    _, client = web
    assert client.post(path, data={}).status_code == 400


def manual(**kwargs):
    return dict(name='Mock', group='A', ip='10.12.0.2', port='1502', device_id='7', confirm_unverified='yes', **kwargs)


def test_manual_unreachable_can_be_added_with_confirmation_and_transport(web, modbus):
    module, client = web
    assert post(client, '/add', manual()).status_code == 302
    inv = Inverter.query.one(); transport = db.session.get(MonitorConfig, inv.id)
    assert (transport.port, transport.device_id, transport.read_address) == (1502, 7, None)
    assert modbus[1] == []
    assert post(client, '/add', manual()).status_code == 400
    assert Inverter.query.count() == 1
    changed = manual(); changed['device_id'] = '8'
    assert post(client, '/add', changed).status_code == 302


@pytest.mark.parametrize('field,value', [('port','0'),('port','65536'),('device_id','248'),('device_id','0'),
    ('port','2.5'),('device_id','-1'),('port','１２'),('ip','127.0.0.1'),('confirm_unverified','no')])
def test_manual_validation(web, field, value):
    _, client = web
    data = manual(); data[field] = value
    assert post(client, '/add', data).status_code == 400
    assert Inverter.query.count() == 0


def test_edit_transport_and_duplicate_guard(web):
    _, client = web
    assert post(client, '/add', manual()).status_code == 302
    data = manual(); data['ip'] = '10.12.0.3'
    assert post(client, '/add', data).status_code == 302
    data['ip'] = '10.12.0.2'
    assert post(client, '/edit/2', data).status_code == 400
    data['port'] = '1602'
    assert post(client, '/edit/2', data).status_code == 302
    assert db.session.get(MonitorConfig, 2).port == 1602


def test_worker_progress_and_fc04_results_without_touching_inventory(web, network, monkeypatch):
    _, client = web
    approve(client); queue(client)
    calls = []
    candidate = run_probe()[0]
    def probe(ip, scope, unit):
        calls.append(ip)
        return dict(candidate, ip=ip)
    monkeypatch.setattr(d, 'probe', probe)
    Worker(db.engine).run(once=True)
    db.session.expire_all()
    row = DiscoveryScan.query.one()
    assert row.state == 'completed' and row.processed == row.found == 1
    assert calls == ['10.12.0.2'] and DiscoveryResult.query.count() == 1
    assert Inverter.query.count() == 0
    assert CommandAudit.query.filter_by(result='completed').count() == 1


def test_worker_revoked_subnet_or_admin_cancels(web, network, monkeypatch):
    _, client = web
    approve(client); queue(client)
    db.session.delete(DiscoverySubnet.query.one()); db.session.commit()
    monkeypatch.setattr(d, 'probe', lambda *a: pytest.fail('revoked subnet scanned'))
    Worker(db.engine).run(once=True)
    db.session.expire_all()
    assert DiscoveryScan.query.one().state == 'cancelled'


def test_worker_recovers_interrupted_scan(web, network):
    _, client = web
    approve(client); queue(client)
    row = DiscoveryScan.query.one(); row.state = 'running'; db.session.commit()
    Worker(db.engine).run(once=True)
    db.session.expire_all()
    assert row.state == 'failed' and 'Попередній worker' in row.error


def test_worker_route_changed_no_connections(web, network, monkeypatch):
    _, client = web
    approve(client); queue(client)
    monkeypatch.setattr(d, 'route_to', lambda _: dict(dev='tailscale0', prefsrc='100.64.1.1'))
    monkeypatch.setattr(d, 'probe', lambda *a: pytest.fail('changed route scanned'))
    Worker(db.engine).run(once=True)
    db.session.expire_all()
    assert DiscoveryScan.query.one().state == 'failed'


def test_status_stale_and_no_passwords(web, network):
    _, client = web
    approve(client); queue(client)
    row = DiscoveryScan.query.one(); row.created_at -= 100; db.session.commit()
    data = client.get('/add/discovery/scans/1').json
    assert data['stale']
    assert 'password' not in data and 'token' not in data


def test_missing_discovery_schema_keeps_portal_manual_and_existing_data(web, network):
    _, client = web
    db.metadata.drop_all(db.engine, tables=list(reversed(DISCOVERY_TABLES)))
    assert client.get('/add').status_code == 200
    assert post(client, '/add', manual()).status_code == 302
    assert queue(client).status_code == 503
    assert Inverter.query.one().name == 'Mock'


def test_schema_backup_additive_preserves_all_real_rows(tmp_path):
    from discovery_schema import prepare
    path = tmp_path/'production.sqlite'
    engine = create_engine('sqlite:///'+str(path))
    tables = [table for table in db.metadata.sorted_tables if table not in DISCOVERY_TABLES]
    db.metadata.create_all(engine, tables=tables)
    with Session(engine) as session, session.begin():
        user = User(username='stewe', role='Administrator'); user.set_password('synthetic-password-only')
        session.add(user)
        session.add(Inverter(name='Keep me', group='A', ip_address='10.12.0.2'))
    engine.dispose()
    with sqlite3.connect(path) as connection:
        original = list(connection.iterdump())
    backup = prepare(path, tmp_path/'backups')
    with sqlite3.connect(backup) as connection:
        assert list(connection.iterdump()) == original
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT name FROM inverter').fetchall() == [('Keep me',)]
        assert connection.execute('SELECT username FROM user').fetchall() == [('stewe',)]
        assert len(connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'discovery_%' AND type='table'").fetchall()) == 3
    assert prepare(path, tmp_path/'backups') != backup


def test_large_scan_requires_confirmation_and_limits_sum(web, monkeypatch):
    _, client = web
    monkeypatch.setattr(d, 'local_inventory', lambda: [['enp1s0', '10.12.0.0/16', '10.12.0.1']])
    monkeypatch.setattr(d, 'route_to', direct)
    assert approve(client, '10.12.4.0/22').status_code == 302
    assert queue(client).status_code == 400
    assert queue(client, confirm_large='yes').status_code == 302
    assert DiscoveryScan.query.one().total == 1022
    assert post(client, '/add/discovery/scans/1/cancel').status_code == 302
    assert approve(client, '10.12.8.0/22').status_code == 302
    assert post(client, '/add/discovery/scans', dict(subnet_id=['1', '2'], confirm_large='yes')).status_code == 400


def test_overlapping_selected_ranges_are_deduplicated(web, network):
    _, client = web
    assert approve(client).status_code == 302
    assert approve(client, '10.12.0.0/29').status_code == 302
    assert post(client, '/add/discovery/scans', dict(subnet_id=['1','2'])).status_code == 302
    assert DiscoveryScan.query.one().total == 5


def test_routed_approval_and_queue_without_mac(web, network, monkeypatch):
    _, client = web
    monkeypatch.setattr(d, 'route_to', routed)
    monkeypatch.setattr(d, 'inventory', lambda: ([], ROUTES))
    assert approve(client, ROUTED.cidr).status_code == 302
    assert DiscoverySubnet.query.one().mode == 'routed'
    assert queue(client).status_code == 302
    original_probe = d.probe
    monkeypatch.setattr(d, 'probe', lambda ip, scope, unit: original_probe(ip, scope, unit,
        boundary=lambda *a: True, tcp=lambda _: True,
        mac_lookup=lambda *a: pytest.fail('routed search must not use gateway MAC'), reader=profile_reader([])))
    Worker(db.engine).run(once=True)
    db.session.expire_all()
    assert DiscoveryScan.query.one().found == 2
    assert all(result.mac is None for result in DiscoveryResult.query.all())


def test_active_scan_cancels_after_current_batch(web, network, monkeypatch):
    import threading
    from discovery_worker import ACTIVE
    _, client = web
    approve(client, '10.12.0.0/29'); queue(client)
    stop = threading.Event()
    calls = []
    def probe(ip, scope, unit):
        calls.append(ip)
        stop.set()
        return None
    monkeypatch.setattr(d, 'probe', probe)
    Worker(db.engine, stop).run(once=True)
    db.session.expire_all()
    scan = DiscoveryScan.query.one()
    assert scan.state == 'cancelled' and scan.processed == 4
    assert len(calls) == 4 and scan.state not in ACTIVE


@pytest.mark.parametrize('change', ['role', 'active', 'version'])
def test_worker_checks_current_administrator_before_io(web, network, monkeypatch, change):
    _, client = web
    approve(client); queue(client)
    user = User.query.one()
    if change == 'role': user.role = 'Viewer'
    if change == 'active': user.active = False
    if change == 'version': user.session_version += 1
    db.session.commit()
    monkeypatch.setattr(d, 'probe', lambda *a: pytest.fail('revoked administrator cannot scan'))
    Worker(db.engine).run(once=True)
    db.session.expire_all()
    assert DiscoveryScan.query.one().state == 'cancelled'


def test_worker_no_sqlite_transaction_held_while_network_io(web, network, monkeypatch):
    from sqlalchemy import event
    _, client = web
    approve(client); queue(client)
    active = set()
    def begun(connection): active.add(id(connection))
    def ended(connection): active.discard(id(connection))
    event.listen(db.engine, 'begin', begun)
    event.listen(db.engine, 'commit', ended)
    event.listen(db.engine, 'rollback', ended)
    def probe(*args):
        assert not active
        return None
    monkeypatch.setattr(d, 'probe', probe)
    try:
        Worker(db.engine).run(once=True)
    finally:
        event.remove(db.engine, 'begin', begun)
        event.remove(db.engine, 'commit', ended)
        event.remove(db.engine, 'rollback', ended)
    db.session.expire_all()
    assert DiscoveryScan.query.one().state == 'completed'


def test_worker_singleton_lock_prevents_second_instance(web, network):
    from operation_locks import resource_lock, OperationBusy
    with resource_lock('discovery-worker::memory:'):
        with pytest.raises(OperationBusy):
            Worker(db.engine).run(once=True)


def test_worker_refuses_root_before_any_io(monkeypatch, tmp_path):
    import discovery_worker as worker
    monkeypatch.setattr(worker.os, 'geteuid', lambda: 0)
    monkeypatch.setattr('sys.argv', ['discovery_worker', '--database', str(tmp_path/'none.sqlite'), '--run'])
    with pytest.raises(SystemExit) as error:
        worker.main()
    assert error.value.code == 2


def test_tcp_timeout_is_unavailable_without_real_socket(monkeypatch):
    def timeout(*a, **kw): raise TimeoutError('synthetic timeout')
    monkeypatch.setattr(d.socket, 'create_connection', timeout)
    assert d.tcp_open('10.12.0.2') is False


def test_reading_neighbor_uses_exact_ip_and_interface_without_shell(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps([dict(dst='10.12.0.2', lladdr='00:25:CA:2C:AA:BB', state=['REACHABLE'])]))
    monkeypatch.setattr(d.subprocess, 'run', run)
    assert d.neighbour('10.12.0.2', 'enp1s0') == '00:25:ca:2c:aa:bb'
    assert calls == [[d.IP, '-json', '-4', 'neigh', 'show', 'to', '10.12.0.2', 'dev', 'enp1s0']]


def test_very_long_or_multiple_transport_values_fail_without_server_error(web):
    from werkzeug.datastructures import MultiDict
    _, client = web
    data = manual(); data['port'] = '1'*5000
    assert post(client, '/add', data).status_code == 400
    data = MultiDict(manual()); data.add('port', '502')
    assert post(client, '/add', data).status_code == 400


def test_status_duplicate_detection_includes_transport(web, network):
    _, client = web
    approve(client); queue(client)
    db.session.add(DiscoveryResult(scan_id=1, ip='10.12.0.2', mac=None, cidr=LOCAL.cidr,
        port=502, device_id=1, tcp_ok=True, modbus_ok=True, confidence='profile', reason='test'))
    db.session.commit()
    assert post(client, '/add', manual()).status_code == 302
    assert client.get('/add/discovery/scans/1').json['results'][0]['registered'] is False


def test_schema_validation_fails_before_creating_tables(tmp_path):
    from discovery_schema import prepare
    path = tmp_path/'wrong.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sentinel (value TEXT)')
        connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        prepare(path, tmp_path/'backups')
    assert path.read_bytes() == original
