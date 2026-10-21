"""Administrator-only discovery controls embedded in /add; web never probes."""
from dataclasses import asdict
import subprocess
import time

from flask import Blueprint, abort, g, jsonify, redirect, request, url_for

import discovery
from discovery_worker import ACTIVE, audit, schema_ready
from models import db, DiscoverySubnet, DiscoveryScan, DiscoveryResult, Inverter, MonitorConfig
from operation_locks import resource_lock, OperationBusy
from security import roles_required

web = Blueprint('discovery_web', __name__, url_prefix='/add/discovery')


def ready():
    if not schema_ready(db.engine):
        abort(503, 'Схему пошуку ще не підготовлено. Ручне додавання доступне.')


def context():
    available, warning = [], None
    try:
        available = discovery.local_inventory()
    except (OSError, ValueError, subprocess.SubprocessError):
        # Only local netlink discovery; no network requests or sensitive output.
        warning = 'Не вдалося прочитати локальні інтерфейси. Перевірте iproute2 та маршрути.'
    prepared = schema_ready(db.engine)
    return dict(discovery_ready=prepared, local_networks=available, discovery_warning=warning,
        subnets=db.session.scalars(db.select(DiscoverySubnet).order_by(DiscoverySubnet.id)).all() if prepared else [],
        scans=db.session.scalars(db.select(DiscoveryScan).order_by(DiscoveryScan.id.desc()).limit(20)).all() if prepared else [])


def integer(name, low, high, default=None):
    values = request.form.getlist(name)
    if not values and default is not None:
        return default
    if len(values) != 1 or len(values[0]) > 5 or not values[0].isascii() or not values[0].isdigit() or not low <= int(values[0]) <= high:
        abort(400, 'Некоректний параметр: '+name)
    return int(values[0])


@web.post('/subnets')
@roles_required('Administrator')
def subnets():
    ready()
    if request.form.getlist('approve') != ['yes'] or len(request.form.getlist('cidr')) != 1:
        abort(400, 'Погодьте приватний діапазон і перевірку його маршрутизації.')
    try:
        scope = discovery.checked_scope(request.form.get('cidr', ''))
        with resource_lock('discovery-queue'):
            if db.session.scalar(db.select(DiscoverySubnet).where(DiscoverySubnet.cidr == scope.cidr)):
                abort(400, 'Діапазон уже погоджено.')
            if db.session.scalar(db.select(db.func.count()).select_from(DiscoverySubnet)) >= 32:
                abort(400, 'Дозволено максимум 32 діапазони.')
            row = DiscoverySubnet(**asdict(scope), approved_by=g.user.id, approved_at=time.time())
            db.session.add(row)
            audit(db.session, g.user.id, 'scan_subnet', 'approved', asdict(scope))
            db.session.commit()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        db.session.rollback()
        abort(400, str(error) if isinstance(error, ValueError) else 'Не вдалося перевірити маршрут.')
    except OperationBusy:
        abort(409, 'Черга зайнята. Повторіть запит.')
    return redirect(url_for('add_inverter'))


@web.post('/subnets/<int:subnet_id>/delete')
@roles_required('Administrator')
def delete_subnet(subnet_id):
    ready()
    if request.form.get('confirm') != 'yes':
        abort(400, 'Потрібне підтвердження видалення діапазону.')
    with resource_lock('discovery-queue'):
        row = db.get_or_404(DiscoverySubnet, subnet_id)
        audit(db.session, g.user.id, 'scan_subnet', 'removed', dict(cidr=row.cidr))
        db.session.delete(row)
        db.session.commit()
    return redirect(url_for('add_inverter'))


@web.post('/scans')
@roles_required('Administrator')
def start():
    ready()
    unit = integer('device_id', 1, 247, 1)
    selected = request.form.getlist('subnet_id')
    if not selected or len(selected) > 32 or any(not item.isascii() or not item.isdigit() for item in selected) or len(set(selected)) != len(selected):
        abort(400, 'Виберіть один або кілька погоджених діапазонів.')
    try:
        with resource_lock('discovery-queue'):
            # Fresh DB snapshot under the common cross-process queue lock.
            db.session.rollback()
            if db.session.scalar(db.select(db.func.count()).select_from(DiscoveryScan)) >= 200:
                abort(400, 'Досягнуто ліміт 200 збережених пошуків. Погодьте архівацію історії пошуку з адміністратором.')
            if db.session.scalar(db.select(DiscoveryScan.id).where(DiscoveryScan.state.in_(ACTIVE)).limit(1)):
                abort(409, 'Інший пошук уже активний. Скасуйте його або дочекайтеся завершення.')
            rows = db.session.scalars(db.select(DiscoverySubnet).where(DiscoverySubnet.id.in_([int(item) for item in selected]))).all()
            if len(rows) != len(selected):
                abort(400, 'Непогоджений діапазон.')
            scopes, hosts = [], set()
            for row in rows:
                scope = discovery.checked_scope(row.cidr)
                if (scope.interface, scope.mode) != (row.interface, row.mode):
                    abort(400, 'Маршрут змінився. Видаліть діапазон і погодьте його повторно.')
                scopes.append(dict(id=row.id, **asdict(scope)))
                hosts.update(discovery.host_addresses(row.cidr, discovery.local_inventory()))
            if len(hosts) > discovery.MAX_HOSTS:
                abort(400, 'Максимум 1024 унікальні адреси на запуск. Виберіть менше діапазонів.')
            if len(hosts) > 254 and request.form.get('confirm_large') != 'yes':
                abort(400, 'Великий пошук: підтвердіть перевірку понад 254 адрес.')
            row = DiscoveryScan(user_id=g.user.id, user_version=g.user.session_version, subnets=scopes,
                                device_id=unit, total=len(hosts), created_at=time.time())
            db.session.add(row)
            db.session.flush()
            scan_id = row.id
            audit(db.session, g.user.id, 'discovery', 'queued', dict(scan_id=scan_id, subnets=scopes, total=len(hosts)))
            db.session.commit()
    except (ValueError, OSError, subprocess.SubprocessError):
        db.session.rollback()
        abort(400, 'Діапазон або маршрут недоступний для безпечного пошуку.')
    except OperationBusy:
        abort(409, 'Черга зайнята. Повторіть запит.')
    return redirect(url_for('add_inverter', scan=scan_id))


@web.post('/scans/<int:scan_id>/cancel')
@roles_required('Administrator')
def cancel(scan_id):
    ready()
    with resource_lock('discovery-queue'):
        row = db.get_or_404(DiscoveryScan, scan_id)
        if row.state not in ACTIVE:
            return redirect(url_for('add_inverter', scan=scan_id))
        if row.state == 'queued':
            row.state, row.finished_at = 'cancelled', time.time()
        elif row.state == 'running':
            row.state = 'cancelling'
        audit(db.session, g.user.id, 'discovery', 'cancel_requested', dict(scan_id=scan_id))
        db.session.commit()
    return redirect(url_for('add_inverter', scan=scan_id))


@web.get('/scans/<int:scan_id>')
@roles_required('Administrator')
def status(scan_id):
    ready()
    row = db.get_or_404(DiscoveryScan, scan_id)
    results = db.session.scalars(db.select(DiscoveryResult).where(DiscoveryResult.scan_id == scan_id).order_by(DiscoveryResult.ip)).all()
    inventory = db.session.execute(db.select(Inverter.ip_address, MonitorConfig.port, MonitorConfig.device_id)
        .outerjoin(MonitorConfig, MonitorConfig.inverter_id == Inverter.id)).all()
    from modbus_config import load_configuration
    config = load_configuration()
    known = {(ip, port or config.port, unit or config.device_id) for ip, port, unit in inventory}
    return jsonify(id=row.id, state=row.state, total=row.total, processed=row.processed, found=row.found,
        error=row.error, stale=row.state in ACTIVE and time.time()-(row.heartbeat or row.created_at) > 30,
        results=[dict(id=item.id, ip=item.ip, mac=item.mac, cidr=item.cidr, port=item.port, device_id=item.device_id,
             tcp_ok=item.tcp_ok, modbus_ok=item.modbus_ok, confidence=item.confidence, reason=item.reason,
             registered=(item.ip, item.port, item.device_id) in known,
             add_url=url_for('add_inverter', candidate=item.id, mode='manual')) for item in results])


@web.errorhandler(OperationBusy)
def busy(error):
    db.session.rollback()
    return jsonify(error='Черга зайнята. Повторіть запит.'), 409
