"""Confirmed bulk inventory creation from saved discovery results, never probes."""
import ipaddress

from flask import abort, current_app, g, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError

from discovery import private_network
from discovery_web import web, ready
from discovery_worker import audit
from models import db, DiscoveryResult, Inverter, MonitorConfig, INVERTER_MODELS
from modbus_config import load_configuration
from operation_locks import resource_lock, OperationBusy
from security import roles_required

MAX_BATCH = 100


def single(name, *, default=None):
    values = request.form.getlist(name)
    if not values and default is not None:
        return default
    if len(values) != 1:
        abort(400, 'Неоднозначні або відсутні поля масового додавання.')
    return values[0]


def selected_ids():
    values = request.form.getlist('candidate_id')
    if not 1 <= len(values) <= MAX_BATCH or any(
            not v.isascii() or not v.isdigit() or len(v) > 19 or not 1 <= int(v) < 2**63 for v in values):
        abort(400, 'Оберіть від 1 до 100 результатів пошуку.')
    keys = sorted(int(v) for v in values)
    if len(set(keys)) != len(keys):
        abort(400, 'Результати пошуку не можуть повторюватися.')
    return keys


def candidates(keys):
    rows = db.session.scalars(db.select(DiscoveryResult).where(DiscoveryResult.id.in_(keys))
                              .order_by(DiscoveryResult.id)).all()
    if len(rows) != len(keys) or len({r.scan_id for r in rows}) != 1:
        abort(400, 'Оберіть наявні результати одного пошуку.')
    for row in rows:
        try:
            network = private_network(row.cidr)
            address = ipaddress.IPv4Address(row.ip)
            if address not in network or address in (network.network_address, network.broadcast_address):
                raise ValueError
            if type(row.port) is not int or not 1 <= row.port <= 65535 or type(row.device_id) is not int or not 1 <= row.device_id <= 247:
                raise ValueError
        except ValueError:
            abort(400, 'Некоректні збережені параметри кандидата.')
    return rows


def payload(rows):
    # No credentials, or their hashes, ever enter an HTML confirmation token.
    return dict(user_id=g.user.id, user_version=g.user.session_version,
                candidates=[dict(id=r.id, scan=r.scan_id, ip=r.ip, port=r.port, unit=r.device_id) for r in rows])


def serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='discovery-bulk-add')


def known_endpoints():
    config = load_configuration()
    rows = db.session.execute(db.select(Inverter.ip_address, MonitorConfig.port, MonitorConfig.device_id)
        .outerjoin(MonitorConfig, MonitorConfig.inverter_id == Inverter.id)).all()
    return {(ip, port or config.port, unit or config.device_id) for ip, port, unit in rows}


@web.post('/bulk')
@roles_required('Administrator')
def bulk_add():
    ready()
    stage, keys = single('stage'), selected_ids()
    if stage == 'preview':
        rows = candidates(keys)
        return render_template('discovery_bulk.html', candidates=rows, known=known_endpoints(),
            confirmation_token=serializer().dumps(payload(rows)),
            groups=db.session.scalars(db.select(Inverter.group).distinct().order_by(Inverter.group)).all(),
            back_url=url_for('add_inverter', scan=rows[0].scan_id))
    if stage != 'save' or request.form.getlist('confirm_unverified') != ['yes']:
        abort(400, 'Потрібне підтвердження додавання вибраних пристроїв.')
    group, model = single('group').strip(), single('model')
    login, password = single('login', default='').strip(), single('password', default='')
    if not group or len(group) > 100 or model not in INVERTER_MODELS or len(login) > 100 or len(password) > 100:
        abort(400, 'Перевірте групу, модель і довжину облікових даних (до 100 символів).')
    names = {key:single(f'name_{key}').strip() for key in keys}
    if any(not name or len(name) > 100 for name in names.values()):
        abort(400, 'Кожному інвертору потрібна назва до 100 символів.')
    try:
        signed = serializer().loads(single('confirmation_token'), max_age=900)
    except BadSignature:
        abort(400, 'Підтвердження недійсне або застаріло. Оберіть пристрої повторно.')
    try:
        with resource_lock('inverter-inventory'):
            db.session.rollback()
            if not g.user.active or g.user.role != 'Administrator':
                abort(403)
            rows = candidates(keys)
            if signed != payload(rows):
                abort(400, 'Вибрані пристрої або сесію змінено. Оберіть пристрої повторно.')
            known = known_endpoints()
            results = []
            for row in rows:
                endpoint = (row.ip, row.port, row.device_id)
                result = dict(candidate_id=row.id, ip=row.ip, port=row.port, device_id=row.device_id,
                              name=names[row.id], outcome='duplicate')
                if endpoint not in known:
                    inv = Inverter(name=names[row.id], group=group, model=model, ip_address=row.ip,
                                   login=login, password=password)
                    db.session.add(inv)
                    db.session.flush()
                    db.session.add(MonitorConfig(inverter_id=inv.id, port=row.port, device_id=row.device_id))
                    known.add(endpoint)
                    result.update(outcome='added', inverter_id=inv.id)
                results.append(result)
            # The inventory and its audit commit atomically. Never log credentials.
            audit(db.session, g.user.id, 'bulk_add', 'completed', dict(model=model, results=results))
            db.session.commit()
    except OperationBusy:
        db.session.rollback()
        abort(409, 'Облік пристроїв зайнятий. Повторіть запит.')
    except SQLAlchemyError:
        db.session.rollback()
        abort(503, 'Додавання не завершено. Жодної частини цього набору не збережено.')
    return render_template('discovery_bulk_results.html', results=results,
                           added=sum(r['outcome']=='added' for r in results))
