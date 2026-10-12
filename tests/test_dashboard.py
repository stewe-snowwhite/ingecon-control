import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest
from itsdangerous import TimestampSigner

from models import Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, StateEvent
from monitoring import target_for
from tests.helpers import post, confirmed_post


JSON = {'Accept': 'application/json'}


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def find(self, tag=None, **attrs):
        return [data for name, data in self.elements if (tag is None or name == tag) and
                all(data.get(key) == value for key, value in attrs.items())]


def seed(module, *, name='Сонце A', group='A', state='Online', ip='192.0.2.1'):
    now = time.time()
    inverter = Inverter(name=name, group=group, ip_address=ip, password='secret-device-sentinel')
    module.db.session.add(inverter)
    module.db.session.flush()
    config = MonitorConfig(inverter_id=inverter.id, read_address=10)
    module.db.session.add(config)
    module.db.session.flush()
    module.db.session.add(InverterStatus(inverter_id=inverter.id, target_key=target_for(inverter, config).key,
        state=state, checked_at=now-2, last_success_at=now-5, latency_ms=4,
        icmp_ok=False, tcp_ok=True, modbus_ok=True))
    if module.db.session.get(MonitorHeartbeat, 1) is None:
        module.db.session.add(MonitorHeartbeat(id=1, owner='synthetic', started_at=now-60,
            last_seen=now-1, interval_seconds=30, stopped=False))
    module.db.session.commit()
    return inverter


def event(module, *, name='Historical', state='Offline', previous='Online', at=1000, inverter_id=1):
    module.db.session.add(StateEvent(inverter_id=inverter_id, inverter_name=name, target_key='synthetic',
        previous_state=previous, state=state, occurred_at=at, reason='modbus_timeout'))


def set_role(module, role):
    user = module.db.session.get(module.User, 1)
    user.role = role
    module.db.session.commit()


def test_counts_are_global_but_list_uses_combined_filters(web, modbus):
    module, client = web
    for index, state in enumerate(('Online', 'Offline', 'Degraded', 'Unknown')):
        seed(module, name='Сонце '+state, group='A' if index % 2 == 0 else 'B', state=state)
    result = client.get('/api/statuses?group=A&state=Online&q=СОНЦЕ').json
    assert result['counts'] == dict(total=4, Online=1, Offline=1, Degraded=1, Unknown=1)
    assert len(result['inverters']) == 1
    assert result['groups'] == ['A', 'B'] and result['generated_at'] > 0
    html = client.get('/?group=A&state=Online&q=СОНЦЕ').get_data(as_text=True)
    assert 'Сонце Online' in html and 'Сонце Offline' not in html and modbus[1] == []


@pytest.mark.parametrize('path', ['/history', '/api/statuses'])
@pytest.mark.parametrize('role', ['Administrator', 'Operator', 'Viewer'])
def test_new_read_routes_roles_and_anonymous_json(web, modbus, path, role):
    module, client = web
    set_role(module, role)
    assert client.get(path).status_code == 200
    anonymous = module.app.test_client()
    result = anonymous.get(path, headers=JSON)
    assert result.status_code == 401 and 'error' in result.json
    assert modbus[1] == []


@pytest.mark.parametrize('path', ['/static/panel.css', '/static/theme.js', '/static/panel.js', '/static/dashboard.js'])
def test_local_assets_are_public_and_have_strict_csp(web, path):
    module, _ = web
    response = module.app.test_client().get(path)
    assert response.status_code == 200
    assert 'script-src \'self\'' in response.headers['Content-Security-Policy']
    assert 'unsafe-inline' not in response.headers['Content-Security-Policy']
    assert 'cdn.' not in response.get_data(as_text=True)


@pytest.mark.parametrize('path', ['/', '/history', '/add', '/edit/1', '/login', '/missing'])
def test_html_has_only_local_assets_and_no_inline_handlers(web, path):
    module, client = web
    seed(module)
    response = client.get(path)
    markup = Markup(response.get_data(as_text=True))
    assert markup.find('meta', name='viewport')
    assert markup.find('html', lang='uk')
    for tag, attrs in markup.elements:
        assert not any(key.startswith('on') or key == 'style' for key in attrs)
        if tag in ('script', 'link'):
            assert attrs.get('src', attrs.get('href', '')).startswith('/static/')
    assert '<script>' not in response.get_data(as_text=True)
    assert 'secret-device-sentinel' not in response.get_data(as_text=True)


@pytest.mark.parametrize('role', ['Administrator', 'Operator', 'Viewer'])
def test_operations_visible_according_to_role(web, role):
    module, client = web
    seed(module)
    set_role(module, role)
    html = client.get('/').get_data(as_text=True)
    markup = Markup(html)
    assert bool(markup.find('input', name='selected_ids')) == (role != 'Viewer')
    assert bool(markup.find('button', name='action', value='delete')) == (role == 'Administrator')
    assert bool(markup.find('a', href='/edit/1')) == (role == 'Administrator')
    for button in markup.find('button', name='action'):
        if button['value'] in ('on','off','limit','limit_max'):
            assert 'disabled' in button


def test_empty_and_stale_dashboard(web, modbus):
    module, client = web
    response = client.get('/api/statuses').json
    assert response['counts']['total'] == 0 and response['inverters'] == []
    assert 'Інвертори не знайдені' in client.get('/').get_data(as_text=True)
    seed(module)
    heartbeat = module.db.session.get(MonitorHeartbeat, 1)
    heartbeat.last_seen -= 1000
    module.db.session.commit()
    result = client.get('/api/statuses').json
    assert not result['monitor']['alive']
    assert result['counts']['Unknown'] == 1 and result['counts']['Online'] == 0
    assert result['inverters'][0]['recorded_state'] == 'Online'
    assert result['inverters'][0]['stale'] and result['inverters'][0]['reason_text']
    assert 'Застарілі дані' in client.get('/').get_data(as_text=True)
    assert modbus[1] == []


@pytest.mark.parametrize('query', ['state=bad', 'q='+('x'*101), 'group='+('x'*101)])
@pytest.mark.parametrize('path', ['/', '/api/statuses'])
def test_invalid_dashboard_filters(web, path, query):
    _, client = web
    assert client.get(path+'?'+query).status_code == 400


def test_history_filter_dates_ids_and_deleted_inventory(web, modbus):
    module, client = web
    at = datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()
    event(module, name='Deleted inverter', at=at, inverter_id=77)
    event(module, name='Other', state='Online', at=at, inverter_id=2)
    event(module, name='Before', at=at-86400, inverter_id=77)
    event(module, name='After', at=at+86400, inverter_id=77)
    module.db.session.commit()
    html = client.get('/history?state=Offline&inverter_id=77&since=2026-10-13&until=2026-10-13').get_data(as_text=True)
    assert 'Deleted inverter' in html and 'Other' not in html and 'Before' not in html and 'After' not in html
    assert 'Перевищено час відповіді Modbus' in html
    assert client.get('/history?q=Deleted').get_data(as_text=True).count('data-label="Інвертор"') == 1
    assert modbus[1] == []


def test_history_pagination_preserves_filters_and_bounds_batch(web):
    module, client = web
    for number in range(101):
        event(module, name='Filtered device', at=number)
    event(module, name='Not matching')
    module.db.session.commit()
    first = client.get('/history?q=Filtered&state=Offline').get_data(as_text=True)
    second = client.get('/history?q=Filtered&state=Offline&page=2').get_data(as_text=True)
    third = client.get('/history?q=Filtered&state=Offline&page=3').get_data(as_text=True)
    assert first.count('data-label="Інвертор"') == second.count('data-label="Інвертор"') == 50
    assert third.count('data-label="Інвертор"') == 1
    assert 'Not matching' not in first and 'Наступна' not in third
    links = Markup(first).find('a')
    assert any('page=2' in item.get('href','') and 'q=Filtered' in item['href'] for item in links)


@pytest.mark.parametrize('query', ['page=0', 'page=-1', 'page=abc', 'state=Bad', 'inverter_id=-1',
    'inverter_id=abc', 'since=invalid', 'since=2026-02-30', 'until=2026-10-9',
    'since=2026-10-15&until=2026-10-13'])
def test_invalid_history_filters(web, query):
    _, client = web
    assert client.get('/history?'+query).status_code == 400


def test_history_search_treats_sql_wildcards_literally_and_escapes_html(web):
    module, client = web
    event(module, name='Device %_ <script>alert(1)</script>')
    event(module, name='Other device')
    module.db.session.commit()
    html = client.get('/history', query_string={'q':'%_'}).get_data(as_text=True)
    assert 'Other device' not in html and '&lt;script&gt;' in html
    assert '<script>alert' not in html
    assert 'Подій за цими фільтрами немає' in client.get('/history?q=missing').get_data(as_text=True)


@pytest.mark.parametrize('path', ['/control', '/delete/1'])
def test_delete_requires_preview_then_confirmation_without_js(web, modbus, path):
    module, client = web
    seed(module, name='<img src=x onerror=alert(1)>')
    data = {'action':'delete', 'selected_ids':'1'}
    preview = post(client, path, data)
    assert preview.status_code == 200 and module.db.session.get(Inverter,1) is not None
    assert '&lt;img' in preview.get_data(as_text=True) and modbus[1] == []
    token = Markup(preview.get_data(as_text=True)).find('input', name='confirmation_token')[0]['value']
    assert post(client, path, dict(data, confirmation_token=token)).status_code == 200
    assert module.db.session.get(Inverter,1) is None


@pytest.mark.parametrize('action', ['on','off','limit','limit_max'])
def test_mock_commands_preview_then_json_results_only_after_confirmation(web, modbus, action):
    module, client = web
    seed(module)
    module.app.config['MODBUS_WRITES_ENABLED'] = True  # Fake client only.
    data = {'action':action, 'selected_ids':'1', 'limit_value':'25'}
    response = post(client, '/control', data, headers=JSON)
    assert response.json['confirmation_required'] and modbus[1] == []
    data['confirmation_token'] = response.json['confirmation_token']
    response = post(client, '/control', data, headers=JSON)
    assert response.status_code == 200 and len(response.json['results']) == 1
    row = response.json['results'][0]
    assert row['outcome'] == 'register_confirmed' and 'фізичний стан' in row['message']


@pytest.mark.parametrize('change', ['action','value','target','role','expired','forged'])
def test_changed_or_expired_confirmation_never_executes(web, modbus, monkeypatch, change):
    module, client = web
    seed(module)
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    clock = [int(time.time())]
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self:clock[0])
    data = {'action':'limit', 'selected_ids':'1', 'limit_value':'25'}
    preview = post(client, '/control', data, headers=JSON).json
    data['confirmation_token'] = preview['confirmation_token']
    if change == 'action': data['action'] = 'on'
    if change == 'value': data['limit_value'] = '26'
    if change == 'target':
        module.db.session.get(Inverter,1).ip_address = '192.0.2.2'
        module.db.session.commit()
    if change == 'role': set_role(module, 'Operator')
    if change == 'expired': clock[0] += 121
    if change == 'forged': data['confirmation_token'] = 'forged'
    response = post(client, '/control', data, headers=JSON)
    assert response.status_code == 400 and 'error' in response.json and modbus[1] == []


def test_json_per_device_queue_and_mixed_command_failures(web, modbus, monkeypatch):
    from tests.fake_modbus import FakeModbusTcpClient
    module, client = web
    seed(module, name='First')
    seed(module, name='Second', ip='192.0.2.2')
    data = {'action':'test','selected_ids':['1','2']}
    response = post(client, '/control', data, headers=JSON)
    assert [row['outcome'] for row in response.json['results']] == ['queued','queued']
    assert modbus[1] == []
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    def factory(host, **kwargs):
        fake = FakeModbusTcpClient(host, **kwargs)
        fake.online = host == '192.0.2.1'
        return fake
    monkeypatch.setattr(modbus[0], 'ModbusTcpClient', factory)
    result = confirmed_post(client, '/control', {'action':'on','selected_ids':['1','2']}, headers=JSON)
    assert [row['outcome'] for row in result.json['results']] == ['register_confirmed','unconfirmed']


def test_json_csrf_errors_and_write_block_never_connect(web, modbus):
    module, client = web
    seed(module)
    assert client.post('/control',data={'action':'delete','selected_ids':'1'},headers=JSON).status_code == 400
    result = post(client,'/control',{'action':'on','selected_ids':'1'},headers=JSON)
    assert result.status_code == 403 and 'Modbus-записи заблоковано' in result.json['error']
    assert modbus[1] == [] and module.db.session.get(Inverter,1) is not None


def test_large_html_results_do_not_overflow_session_cookie(web, modbus):
    import hashlib
    module, client = web
    for number in range(100):
        module.db.session.add(Inverter(name=hashlib.sha256(str(number).encode()).hexdigest(),
                                       group='A', ip_address='192.0.2.1'))
    module.db.session.commit()
    response = post(client, '/control', {'action':'test','selected_ids':[str(n) for n in range(1,101)]})
    assert response.status_code == 200
    markup = Markup(response.get_data(as_text=True))
    assert len(markup.find('li', **{'class':'queued'})) == 100
    with client.session_transaction() as sess:
        assert '_flashes' not in sess
    assert modbus[1] == []
