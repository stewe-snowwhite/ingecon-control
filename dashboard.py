"""DB-only dashboard helpers; no monitoring or control transport calls."""
from datetime import datetime, timedelta, timezone
import secrets

from flask import abort, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from commands import command_payload

STATES = ('Online', 'Offline', 'Degraded', 'Unknown')
ACTIONS = {'on': 'Увімкнути', 'off': 'Вимкнути', 'limit': 'Обмежити потужність',
           'limit_max': 'Повернути максимум', 'delete': 'Видалити', 'test': 'Запланувати перевірку'}


def filters():
    result = {key: request.args.get(key, '').strip() for key in ('q', 'group', 'state')}
    if any(len(value) > 100 for value in result.values()) or result['state'] not in ('', *STATES):
        abort(400, 'Некоректні фільтри.')
    return result


def dashboard_snapshot(snapshot, selected):
    rows = snapshot['inverters']
    counts = {state: sum(row['state'] == state for row in rows) for state in STATES}
    counts['total'] = len(rows)
    groups = sorted({row['group'] for row in rows})
    query = selected['q'].casefold()
    matching = [row for row in rows if
                (not selected['group'] or row['group'] == selected['group']) and
                (not selected['state'] or row['state'] == selected['state']) and
                (not query or any(query in row[key].casefold() for key in ('name', 'ip_address', 'group')))]
    return dict(snapshot, inverters=matching, counts=counts, groups=groups)


def date_filter(value, end=False):
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if parsed.strftime('%Y-%m-%d') != value:
            raise ValueError
        return (parsed + timedelta(days=1) if end else parsed).timestamp()
    except (ValueError, OverflowError):
        abort(400, 'Некоректна дата; потрібен формат YYYY-MM-DD.')


def sign_confirmation(secret, payload):
    return URLSafeTimedSerializer(secret, salt='dashboard-command-confirmation').dumps(dict(payload, nonce=secrets.token_hex(16)))


def verify_confirmation(secret, token, payload):
    try:
        actual = URLSafeTimedSerializer(secret, salt='dashboard-command-confirmation').loads(token, max_age=120)
        actual.pop('nonce', None)
    except (BadSignature, SignatureExpired):
        abort(400, 'Підтвердження недійсне або застаріло. Перевірте список і підтвердьте заново.')
    if actual != payload:
        abort(400, 'Команду, права або дані інвертора змінено. Потрібне нове підтвердження.')
