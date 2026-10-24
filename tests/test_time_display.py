"""Kyiv presentation/calendar boundaries; no device or Telegram network calls."""
from datetime import datetime, timezone

import pytest


@pytest.mark.parametrize('source,expected', [
    ('2026-01-15T12:00:00+00:00', '2026-01-15 14:00:00'),
    ('2026-07-15T12:00:00+00:00', '2026-07-15 15:00:00'),
    ('2026-07-15T22:00:00+00:00', '2026-07-16 01:00:00'),
    ('2026-03-29T00:59:59+00:00', '2026-03-29 02:59:59'),
    ('2026-03-29T01:00:00+00:00', '2026-03-29 04:00:00'),
    ('2026-10-25T00:59:59+00:00', '2026-10-25 03:59:59'),
    ('2026-10-25T01:00:00+00:00', '2026-10-25 03:00:00'),
])
def test_web_and_telegram_use_kyiv_including_dst(source, expected):
    from time_display import format_kyiv
    from telegram_ui import kyiv_time
    timestamp = datetime.fromisoformat(source).timestamp()
    assert format_kyiv(timestamp) == expected
    assert kyiv_time(timestamp) == expected+' (Київ)'


def test_missing_timestamp_is_dash():
    from time_display import format_kyiv
    from telegram_ui import kyiv_time
    assert format_kyiv(None) == kyiv_time(None) == '—'


@pytest.mark.parametrize('day,hours', [
    ('2026-01-15', 24), ('2026-07-15', 24),
    ('2026-03-29', 23), ('2026-10-25', 25),
])
def test_history_queries_include_exact_kyiv_day(web, day, hours):
    from dashboard import date_filter
    from models import StateEvent
    from time_display import KYIV
    module, client = web
    start, end = date_filter(day), date_filter(day, end=True)
    assert end-start == hours*3600
    assert datetime.fromtimestamp(start, KYIV).isoformat().startswith(day+'T00:00:00')
    for name, timestamp in [('outside-before', start-1), ('day-start', start),
                            ('day-end', end-1), ('outside-after', end)]:
        module.db.session.add(StateEvent(inverter_id=1, inverter_name=name, target_key='mock',
            previous_state='Online', state='Offline', occurred_at=timestamp, reason='modbus_timeout'))
    module.db.session.commit()
    result = client.get('/history', query_string={'since': day, 'until': day})
    assert result.status_code == 200
    html = result.get_data(as_text=True)
    assert 'day-start' in html and 'day-end' in html
    assert 'outside-before' not in html and 'outside-after' not in html
    assert day+' 00:00:00' in html and day+' 23:59:59' in html
    assert 'Час (Київ)' in html and 'Час (UTC)' not in html
    stored = module.db.session.scalars(module.db.select(StateEvent).order_by(StateEvent.id)).all()
    assert [row.occurred_at for row in stored] == [start-1, start, end-1, end]
    assert client.get('/api/statuses').json['generated_at'] == pytest.approx(datetime.now(timezone.utc).timestamp(), abs=5)


def test_templates_keep_utc_label_until_old_worker_is_restarted(web):
    module, client = web
    module.app.config.pop('DISPLAY_TIMEZONE')
    module.app.jinja_env.filters['utc_time'] = lambda value: '—'
    html = client.get('/history').get_data(as_text=True)
    assert 'Час: UTC.' in html and 'Від (UTC)' in html
