"""Presentation tests: synthetic SQLite only, no inverter queries or commands."""
import json
import time
from html.parser import HTMLParser

import pytest

from models import Inverter, InverterStatus, MonitorHeartbeat
from monitoring import target_for


LABELS = {
    0: 'Без обмеження',
    1: 'Без обмеження',
    2: 'Не визначено виробником',
    3: 'Не визначено виробником',
    4: 'Не визначено виробником',
    5: "Зовнішня команда через зв'язок",
    6: 'Частота електромережі',
    7: 'Напруга електромережі',
    8: 'Обмеження вихідного струму / пріоритет реактивної потужності',
    9: 'Налаштування інвертора',
    10: 'Початкове нарощування потужності',
    11: 'Власне споживання',
    12: 'Зарезервовано виробником',
    13: 'Залежність потужності від напруги',
}


class ReasonCells(HTMLParser):
    """Read actual escaped attributes and visible cell text, including apostrophes."""
    def __init__(self, html):
        super().__init__()
        self.cells = {}
        self.current = None
        self.inverter_id = None
        self.catalog = None
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'section' and attrs.get('id') == 'dashboard':
            self.catalog = json.loads(attrs['data-limit-reasons'])
        if tag == 'tr' and 'data-id' in attrs:
            self.inverter_id = int(attrs['data-id'])
        if tag == 'td' and attrs.get('data-label') == 'Причина обмеження':
            self.current = {'text': '', 'attrs': {}}
        if tag == 'span' and self.current is not None:
            self.current['attrs'] = attrs

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'td' and self.current is not None:
            self.cells[self.inverter_id] = self.current
            self.current = None


def seed_reasons(module):
    now = time.time()
    module.db.session.add(MonitorHeartbeat(id=1, owner='synthetic', started_at=now-30,
        last_seen=now, interval_seconds=30, stopped=False))
    codes = {}
    for index, code in enumerate([*LABELS, 65534, None], start=1):
        inv = Inverter(name=f'100TL-{index}', group='A', ip_address=f'192.0.2.{index}',
                       model='ingecon_sun_100tl')
        module.db.session.add(inv)
        module.db.session.flush()
        module.db.session.add(InverterStatus(inverter_id=inv.id, target_key=target_for(inv, None).key,
            state='Online', checked_at=now, last_success_at=now, latency_ms=3,
            icmp_ok=True, tcp_ok=True, modbus_ok=True, limit_reason_code=code))
        codes[inv.id] = code
    module.db.session.commit()
    return codes


@pytest.mark.parametrize('role', ['Administrator', 'Operator', 'Viewer'])
def test_all_reason_labels_hints_raw_codes_and_unchanged_api(web, modbus, role):
    module, client = web
    module.db.session.get(module.User, 1).role = role
    codes = seed_reasons(module)
    page = client.get('/')
    assert page.status_code == 200
    markup = ReasonCells(page.get_data(as_text=True))
    assert set(markup.cells) == set(codes)
    assert {int(code): value['label'] for code, value in markup.catalog.items()} == LABELS
    for inverter_id, code in codes.items():
        cell = markup.cells[inverter_id]
        if code is None:
            assert cell == {'text': '—', 'attrs': {}}
            continue
        expected = f'{LABELS[code]} ({code})' if code in LABELS else f'Невідома причина (код {code})'
        assert cell['text'] == expected
        assert cell['attrs']['data-limit-reason-code'] == str(code)
        assert cell['attrs']['title']
        if code in LABELS:
            # The polling script receives exactly the same names and hover hints as SSR.
            assert cell['attrs']['title'] == markup.catalog[str(code)]['hint']
    data = client.get('/api/statuses').json
    assert {row['id']: row['limit_reason_code'] for row in data['inverters']} == codes
    assert all('limit_reason_label' not in row and 'limit_reason_hint' not in row for row in data['inverters'])
    assert modbus[1] == []


def test_stale_reason_disappears_from_html_and_api_without_changing_stored_code(web, modbus):
    module, client = web
    codes = seed_reasons(module)
    module.db.session.get(MonitorHeartbeat, 1).last_seen -= 1000
    module.db.session.commit()
    markup = ReasonCells(client.get('/').get_data(as_text=True))
    assert all(cell == {'text': '—', 'attrs': {}} for cell in markup.cells.values())
    assert all(row['limit_reason_code'] is None for row in client.get('/api/statuses').json['inverters'])
    for inverter_id, code in codes.items():
        assert module.db.session.get(InverterStatus, inverter_id).limit_reason_code == code
    assert modbus[1] == []
