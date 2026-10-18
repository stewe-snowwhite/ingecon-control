"""Global Modbus settings, audited previews and a write-proof register panel."""
import logging
import re
import time
import uuid
from dataclasses import asdict, replace

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, url_for, flash
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from models import db, Inverter, CommandAudit
from modbus_config import ModbusConfiguration, load_configuration, save_configuration
from modbus_safety import WRITE_LOCK_REASON
from register_tools import FUNCTIONS, RegisterRequest, read_registers
from input_registers import DOCUMENT, INPUT_REGISTERS, SOURCE
from security import roles_required
from operation_locks import OperationBusy

modbus_web = Blueprint('modbus_web', __name__, url_prefix='/settings/modbus')
log = logging.getLogger('ingecon.modbus')
FIELDS = tuple(key for key in asdict(ModbusConfiguration()) if key != 'revision')
LABELS = dict(port='TCP-порт', device_id='Unit ID', timeout='Таймаут, с',
    start_address='Увімкнення · Holding Register', start_value='Увімкнення · значення',
    stop_address='Вимкнення · Holding Register', stop_value='Вимкнення · значення',
    limit_address='Обмеження · Holding Register (1–100 %)',
    limit_read_address='Читання обмеження · Holding Register',
    limit_reason_address='Причина обмеження · Holding Register')


def field(name):
    values = request.form.getlist(name)
    if len(values) != 1: raise ValueError('Неоднозначні або відсутні поля форми.')
    return values[0]


def number(name):
    value = field(name)
    if not re.fullmatch(r'[0-9]{1,19}', value, re.ASCII): raise ValueError('Потрібне ціле невід’ємне число: '+name)
    return int(value)


def known_fields(names):
    if set(request.form) - set(names) - {'csrf_token', 'confirmation_token'}:
        raise ValueError('Форма містить невідомі параметри Modbus.')


def serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='modbus-request-confirmation-v1')


def payload(target, operation, config):
    return dict(user=g.user.id, version=g.user.session_version, role=g.user.role,
        target=[target.id, target.name, target.group, target.ip_address],
        operation=operation, configuration=config.fingerprint)


def preview_or_execute(target, operation, config, *, reader=False):
    data = payload(target, operation, config)
    tokens = request.form.getlist('confirmation_token')
    if not tokens:
        intent = 'mb:'+uuid.uuid4().hex
        db.session.add(CommandAudit(request_id=intent, source='web', user_id=g.user.id,
            username=g.user.username, occurred_at=time.time(), action='register_test' if reader else operation['action'],
            targets=[data['target']], result='preview', details=dict(request=data)))
        db.session.commit()
        token = serializer().dumps(dict(intent=intent, payload=data))
        return render_template('modbus_confirm.html', target=target, operation=operation,
            confirmation_token=token, action_url=request.path, submitted=list(request.form.items(multi=True)))
    if len(tokens) != 1: abort(400, 'Неоднозначне підтвердження.')
    try:
        signed = serializer().loads(tokens[0], max_age=120)
        if signed['payload'] != data: raise ValueError
        intent = signed['intent']
    except (BadSignature, ValueError, KeyError, TypeError):
        abort(400, 'Підтвердження застаріло або параметри змінилися. Підготуйте запит знову.')
    claimed = db.session.execute(update(CommandAudit).where(CommandAudit.request_id == intent,
        CommandAudit.user_id == g.user.id, CommandAudit.result == 'preview').values(result='processing')).rowcount
    if claimed != 1:
        db.session.rollback()
        abort(400, 'Підтвердження вже використано або недійсне.')
    db.session.commit()  # Release SQLite before any optional read I/O.
    if not reader or operation['function'] in (6, 16):
        result = dict(outcome='blocked', message=WRITE_LOCK_REASON, registers=[])
    elif not current_app.config['REGISTER_READS_ENABLED']:
        result = dict(outcome='blocked', message='Фізичне читання вимкнене конфігурацією сервера.', registers=[])
    else:
        params = dict(operation)
        params['values'] = tuple(params['values'])
        result = read_registers(RegisterRequest(**params))
    db.session.execute(update(CommandAudit).where(CommandAudit.request_id == intent,
        CommandAudit.result == 'processing').values(result=result['outcome'],
        occurred_at=time.time(), details=dict(request=data, result=result)))
    db.session.commit()
    return render_template('modbus_result.html', target=target, result=result, operation=operation)


@modbus_web.errorhandler(ValueError)
def invalid(error):
    log.warning('MODBUS_REJECTED user_id=%s reason=validation', g.user.id if g.user else None)
    return render_template('error.html', code=400, message=str(error)), 400


@modbus_web.errorhandler(OSError)
@modbus_web.errorhandler(SQLAlchemyError)
@modbus_web.errorhandler(OperationBusy)
def unavailable(error):
    db.session.rollback()
    g.user = None
    return render_template('error.html', code=503,
        message='Не вдалося підтвердити результат операції. Перевірте конфігурацію та аудит; не повторюйте запит автоматично.'), 503


@modbus_web.route('', methods=['GET', 'POST'])
@roles_required('Administrator')
def settings():
    config = load_configuration()
    if request.method == 'POST':
        if set(request.form) != set(FIELDS) | {'csrf_token', 'config_fingerprint'}:
            raise ValueError('Форма містить невідомі налаштування. Записи не можна ввімкнути через конфігурацію.')
        updated = replace(config, **{name: number(name) for name in FIELDS})
        expected = field('config_fingerprint')
        # Validate all values before recording a pending configuration change.
        from modbus_config import validate_configuration
        updated = validate_configuration(asdict(updated))
        intent = 'cfg:'+uuid.uuid4().hex
        record = CommandAudit(request_id=intent, source='web', user_id=g.user.id, username=g.user.username,
            occurred_at=time.time(), action='modbus_config', targets=[], result='pending',
            details=dict(before=asdict(config), requested=asdict(updated)))
        db.session.add(record)
        db.session.commit()
        try:
            saved = save_configuration(updated, expected)
        except (ValueError, OSError):
            db.session.execute(update(CommandAudit).where(CommandAudit.request_id==intent).values(result='rejected'))
            db.session.commit()
            raise
        db.session.execute(update(CommandAudit).where(CommandAudit.request_id==intent).values(
            result='saved', details=dict(before=asdict(config), after=asdict(saved))))
        db.session.commit()
        flash('Спільну конфігурацію збережено. Команди залишаються непідтвердженими, фізичний запис заблоковано.', 'info')
        return redirect(url_for('.settings'))
    return render_template('modbus_settings.html', config_values=asdict(config), config_fingerprint=config.fingerprint,
        fields=FIELDS, labels=LABELS, input_registers=INPUT_REGISTERS, document=DOCUMENT, source=SOURCE)


@modbus_web.route('/control', methods=['GET', 'POST'])
@roles_required('Administrator', 'Operator')
def control():
    config = load_configuration()
    if request.method == 'POST':
        known_fields(('inverter_id', 'action', 'value'))
        target = db.get_or_404(Inverter, number('inverter_id'))
        action = field('action')
        value = number('value')
        if not 1 <= value <= 100: raise ValueError('Відсоток має бути цілим числом 1–100.')
        address, command_value = config.command(action, value)
        operation = dict(action=action, function=6, address=address, values=[command_value],
            port=config.port, device_id=config.device_id, timeout=config.timeout, percent=value)
        return preview_or_execute(target, operation, config)
    targets = db.session.execute(db.select(Inverter.id, Inverter.name, Inverter.ip_address).order_by(Inverter.name)).all()
    return render_template('modbus_control.html', targets=targets, modbus_config=config)


@modbus_web.route('/registers', methods=['GET', 'POST'])
@roles_required('Administrator')
def registers():
    config = load_configuration()
    if request.method == 'POST':
        known_fields(('inverter_id', 'ip', 'port', 'device_id', 'function', 'address', 'count', 'values'))
        target = db.get_or_404(Inverter, number('inverter_id'))
        text = field('values').strip()
        if text and not re.fullmatch(r'[0-9]{1,5}(?:\s*,\s*[0-9]{1,5}){0,122}', text, re.ASCII):
            raise ValueError('Значення: цілі числа, розділені комами, максимум 123.')
        values = tuple(int(value.strip()) for value in text.split(',')) if text else ()
        params = RegisterRequest(target.id, field('ip'), number('port'), number('device_id'),
            number('function'), number('address'), number('count'), values, config.timeout).validate()
        if params.ip != target.ip_address:
            raise ValueError('IP має збігатися з адресою вибраного зареєстрованого інвертора.')
        return preview_or_execute(target, params.payload(), config, reader=True)
    targets = db.session.execute(db.select(Inverter.id, Inverter.name, Inverter.ip_address).order_by(Inverter.name)).all()
    return render_template('modbus_registers.html', targets=targets, modbus_config=config, functions=FUNCTIONS)
