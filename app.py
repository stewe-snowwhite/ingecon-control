import ipaddress
import os
import re
import time
import hashlib
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for, jsonify)
from flask.helpers import get_debug_flag
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException, SecurityError

from models import db, Inverter, ROLES, User, MonitorConfig, InverterStatus, StateEvent, INVERTER_MODELS, CommandAudit
from dashboard import (ACTIONS, STATES, command_payload, dashboard_snapshot, date_filter,
                       filters, sign_confirmation, verify_confirmation)
from monitoring import current_statuses, Target, explain_reason
from commands import block_reason, execute_devices, transports_for, ControlBlocked
import inverter_controls
from security import USERNAME, authenticate, load_user, log_failure, reserve_login_attempt, roles_required
from telegram_web import telegram_web
from users_web import users_web
from modbus_web import modbus_web
from discovery_web import web as discovery_web, context as discovery_context, integer as form_integer, ready as discovery_ready
import discovery_bulk  # Registers the saved-results bulk handler before blueprint registration.
from operation_locks import resource_lock, OperationBusy
from modbus_config import load_configuration
from configuration import cookie_secure_from_env, secret_from_env
import monitor_schedule
from monitor_web import web as monitor_web
from telegram_store import TelegramStore
from power_web import web as power_web

csrf = CSRFProtect()
ALL_ROLES = ROLES
CONTROL_ROLES = ('Administrator', 'Operator')


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=config['SECRET_KEY'] if config and 'SECRET_KEY' in config else secret_from_env(),
        SQLALCHEMY_DATABASE_URI=os.environ.get('INGECON_DATABASE_URI', 'sqlite:///database.db'),
        DEBUG=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=(config['SESSION_COOKIE_SECURE'] if config and 'SESSION_COOKIE_SECURE' in config
                               else cookie_secure_from_env()),
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=64 * 1024,
        MODBUS_WRITES_ENABLED=inverter_controls.enabled(),
        REGISTER_READS_ENABLED=os.environ.get('INGECON_REGISTER_READS_ENABLED', 'false').lower() == 'true',
        MONITOR_STALE_AFTER_SECONDS=None,
        LOGIN_IP_LIMIT=20,
        LOGIN_ACCOUNT_LIMIT=5,
        LOGIN_WINDOW_SECONDS=300,
        MODBUS_LOG_PATH=str(Path(app.root_path) / 'modbus.log'),
    )
    if config:
        app.config.update(config)
    if type(app.config['SESSION_COOKIE_SECURE']) is not bool:
        raise RuntimeError('SESSION_COOKIE_SECURE must be a boolean.')
    if type(app.config['REGISTER_READS_ENABLED']) is not bool or os.environ.get('INGECON_REGISTER_READS_ENABLED', 'false').lower() not in ('true', 'false'):
        raise RuntimeError('INGECON_REGISTER_READS_ENABLED must explicitly be true or false.')
    if not isinstance(app.config['SECRET_KEY'], str) or len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('Set INGECON_SECRET_KEY to a random secret of at least 32 characters.')
    if app.debug or get_debug_flag():
        raise RuntimeError('Debug mode is forbidden.')
    db.init_app(app)
    app.before_request(load_user)
    csrf.init_app(app)
    app.register_blueprint(telegram_web)
    app.register_blueprint(users_web)
    app.register_blueprint(modbus_web)
    app.register_blueprint(discovery_web)
    app.register_blueprint(monitor_web)
    app.register_blueprint(power_web)
    app.config['POWER_BUDGET_AVAILABLE'] = True
    app.config['MONITOR_POLLING_AVAILABLE'] = True
    app.config['DISCOVERY_AVAILABLE'] = True
    app.config['MODBUS_TOOLS_AVAILABLE'] = True
    app.config['MODEL_SELECTION_AVAILABLE'] = True
    app.config['BULK_ADD_AVAILABLE'] = True
    # Keep existing form/bookmark URLs protected by the same handlers.
    app.register_blueprint(telegram_web, url_prefix='/telegram', name='legacy_telegram')
    app.register_blueprint(users_web, url_prefix='/users', name='legacy_users')
    # Shared templates remain safe until existing workers load the new routes.
    app.config['SETTINGS_AVAILABLE'] = True
    app.jinja_env.filters['monitor_reason'] = explain_reason
    app.jinja_env.filters['utc_time'] = lambda value: (
        datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        if value is not None else '—'
    )

    @app.after_request
    def security_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self'; font-src 'self'; frame-ancestors 'none'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        return response

    def wants_json():
        return request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json'

    @app.errorhandler(401)
    def login_required(error):
        if wants_json():
            return jsonify(error='Сесію завершено. Увійдіть знову.'), 401
        return render_template('unauthorized.html'), 401

    @app.errorhandler(HTTPException)
    def http_error(error):
        if isinstance(error, SecurityError):
            # An untrusted Host has no URL adapter; templates using url_for
            # cannot render safely here. Do not reflect the supplied Host.
            if wants_json():
                return jsonify(error='Недозволений Host.'), 400
            return 'Недозволений Host.', 400, {'Content-Type': 'text/plain; charset=utf-8'}
        messages = {400: 'Некоректний запит або недійсний CSRF. Оновіть сторінку й повторіть дію.',
                    403: 'Недостатньо прав або операцію заблоковано.', 404: 'Сторінку або запис не знайдено.',
                    405: 'Цей метод запиту не дозволений.', 500: 'Не вдалося обробити запит. Спробуйте пізніше.'}
        message = messages.get(error.code, 'Запит не вдалося виконати.')
        # Application validation messages are Ukrainian; do not expose internals.
        if error.code in (400, 403) and any('\u0400' <= char <= '\u04ff' for char in error.description):
            message = error.description
        response = error.get_response()
        if wants_json():
            response.data = app.json.dumps({'error': message})
            response.content_type = 'application/json'
        else:
            response.data = render_template('error.html', code=error.code, message=message)
            response.content_type = 'text/html; charset=utf-8'
        return response

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            return render_template('login.html')
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        # Bound input before expensive hashing; malformed names use a fixed bucket.
        if not USERNAME.fullmatch(username):
            username = '<invalid>'
        retry_after = reserve_login_attempt(username)
        if retry_after:
            log_failure('rate_limited')
            return render_template('login.html', error='Забагато спроб. Спробуйте пізніше.'), 429, {'Retry-After': str(retry_after)}
        user = authenticate(username, password) if len(password) <= 1024 else None
        if user is None:
            log_failure('invalid_credentials')
            return render_template('login.html', error='Неправильні облікові дані.'), 401
        session.clear()
        session.permanent = True
        session['user_id'] = user.id
        session['session_version'] = user.session_version
        return redirect(url_for('index'))

    @app.post('/logout')
    @roles_required(*ALL_ROLES)
    def logout():
        # Revoke signed cookies for every session of this user, including copies.
        # The compare-and-swap prevents a concurrent password change from being
        # mistaken for this logout and reviving a revoked Telegram pairing.
        previous_version = session['session_version']
        try:
            next_version = db.session.scalar(update(User).where(User.id==g.user.id,
                User.active.is_(True), User.role.in_(ALL_ROLES), User.session_version==previous_version)
                .values(session_version=User.session_version+1).returning(User.session_version))
            if next_version is None:
                db.session.rollback()
                session.clear()
                g.user = None
                abort(401)
            TelegramStore.preserve_on_web_logout(db.session, g.user.id, previous_version, next_version)
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            g.user = None
            abort(503, 'Не вдалося завершити вихід. Повторіть дію пізніше.')
        session.clear()
        return redirect(url_for('login'))

    @app.get('/settings')
    @roles_required(*ALL_ROLES)
    def settings():
        return redirect(url_for('telegram_web.settings'))

    @app.get('/download-log')
    @roles_required('Administrator')
    def download_log():
        path = Path(app.config['MODBUS_LOG_PATH'])
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True)

    @app.get('/')
    @roles_required(*ALL_ROLES)
    def index():
        selected = filters()
        snapshot = dashboard_snapshot(current_statuses(db.session,
            stale_after=app.config['MONITOR_STALE_AFTER_SECONDS']), selected)
        return render_template('index.html', **snapshot, selected=selected, states=STATES,
                               polling=monitor_schedule.current())

    @app.get('/api/statuses')
    @roles_required(*ALL_ROLES)
    def statuses():
        snapshot = dashboard_snapshot(current_statuses(db.session,
            stale_after=app.config['MONITOR_STALE_AFTER_SECONDS']), filters())
        for row in snapshot['inverters']:
            row['reason_text'] = explain_reason(row['reason'])
        return jsonify(dict(snapshot, generated_at=time.time(), polling=monitor_schedule.current()))

    @app.get('/history')
    @roles_required(*ALL_ROLES)
    def history():
        state = request.args.get('state', '')
        query = request.args.get('q', '').strip()
        inverter_id = request.args.get('inverter_id', '')
        since, until = request.args.get('since', ''), request.args.get('until', '')
        page = request.args.get('page', '1')
        if (state not in ('', *STATES) or len(query) > 100 or
            not re.fullmatch(r'[1-9][0-9]{0,6}', page, re.ASCII) or
            (inverter_id and not re.fullmatch(r'[1-9][0-9]{0,9}', inverter_id, re.ASCII))):
            abort(400, 'Некоректні фільтри історії.')
        criteria = []
        if state: criteria.append(StateEvent.state == state)
        if query: criteria.append(StateEvent.inverter_name.icontains(query, autoescape=True))
        if inverter_id: criteria.append(StateEvent.inverter_id == int(inverter_id))
        start = date_filter(since) if since else None
        end = date_filter(until, end=True) if until else None
        if start is not None: criteria.append(StateEvent.occurred_at >= start)
        if end is not None: criteria.append(StateEvent.occurred_at < end)
        if start is not None and end is not None and start >= end:
            abort(400, 'Початок періоду не може бути пізніше кінця.')
        stmt = db.select(StateEvent).where(*criteria).order_by(StateEvent.id.desc())
        events = db.session.scalars(stmt.offset((int(page)-1)*50).limit(51)).all()
        selected = dict(state=state, q=query, inverter_id=inverter_id, since=since, until=until)
        return render_template('history.html', events=events[:50], selected=selected,
            states=STATES, page=int(page), has_next=len(events)>50,
            previous_url=url_for('history', **selected, page=int(page)-1) if int(page)>1 else None,
            next_url=url_for('history', **selected, page=int(page)+1) if len(events)>50 else None)

    def request_check(inverter):
        config = db.session.get(MonitorConfig, inverter.id)
        if config is None:
            config = MonitorConfig(inverter_id=inverter.id)
            db.session.add(config)
        config.requested_at = time.time()

    def remove_inverter(inverter):
        # Keep StateEvent history/outbox, remove only current configuration/status.
        for model in (MonitorConfig, InverterStatus):
            db.session.execute(db.delete(model).where(model.inverter_id == inverter.id))
        db.session.delete(inverter)

    def inverter_fields(existing=None):
        if any(len(request.form.getlist(key)) > 1 for key in ('name', 'group', 'ip', 'login', 'password', 'model')):
            abort(400, 'Неоднозначні поля інвертора.')
        fields = {key: request.form.get(key, '').strip() for key in ('name', 'group', 'ip', 'login')}
        if any(not fields[key] or len(fields[key]) > 100 for key in ('name', 'group', 'ip')):
            abort(400, 'Назва, група та IP обов’язкові; максимум 100 символів.')
        try:
            address = ipaddress.ip_address(fields['ip'])
            if address.is_multicast or address.is_unspecified or address.is_loopback:
                raise ValueError
        except ValueError:
            abort(400, 'Некоректна адреса інвертора.')
        password = request.form.get('password', '')
        model = request.form.get('model', existing.model if existing else 'unknown')
        if model not in INVERTER_MODELS:
            abort(400, 'Невідома модель інвертора.')
        if len(fields['login']) > 100 or len(password) > 100:
            abort(400, 'Завеликі облікові дані інвертора.')
        return dict(name=fields['name'], group=fields['group'], ip_address=str(address), model=model,
                    login=fields['login'], password=password)

    def transport_fields(existing=None):
        config = existing or load_configuration()
        return dict(port=form_integer('port', 1, 65535, config.port),
                    device_id=form_integer('device_id', 1, 247, config.device_id))

    def check_duplicate(fields, transport, exclude=None):
        config = load_configuration()
        rows = db.session.execute(db.select(Inverter.id, MonitorConfig.port, MonitorConfig.device_id)
            .outerjoin(MonitorConfig, MonitorConfig.inverter_id == Inverter.id)
            .where(Inverter.ip_address == fields['ip_address'])).all()
        if any(key != exclude and (port or config.port, unit or config.device_id) ==
               (transport['port'], transport['device_id']) for key, port, unit in rows):
            abort(400, 'Інвертор із такими IP, портом і Unit ID вже зареєстрований.')

    @app.route('/add', methods=['GET', 'POST'])
    @roles_required('Administrator')
    def add_inverter():
        if request.method == 'POST':
            if request.form.getlist('confirm_unverified') != ['yes']:
                abort(400, 'Підтвердьте збереження без перевірки доступності інвертора.')
            fields, transport = inverter_fields(), transport_fields()
            try:
                with resource_lock('inverter-inventory'):
                    db.session.rollback()
                    check_duplicate(fields, transport)
                    inverter = Inverter(**fields)
                    db.session.add(inverter)
                    db.session.flush()
                    # Explicit model selects the shared confirmed read profile.
                    db.session.add(MonitorConfig(inverter_id=inverter.id, **transport))
                    db.session.commit()
            except OperationBusy:
                abort(409, 'Облік пристроїв зайнятий. Повторіть запит.')
            flash('Інвертор додано без перевірки підключення. Для INGECON SUN 100TL профіль FC04 обирається за моделлю.', 'info')
            return redirect(url_for('index'))
        defaults = load_configuration()
        candidate = None
        if request.args.get('candidate'):
            discovery_ready()
            from models import DiscoveryResult
            candidate = db.get_or_404(DiscoveryResult, request.args['candidate'])
        groups = db.session.scalars(db.select(Inverter.group).distinct()).all()
        return render_template('add_inverter.html', groups=groups, defaults=defaults, candidate=candidate,
                               **discovery_context())

    @app.route('/edit/<int:inverter_id>', methods=['GET', 'POST'])
    @roles_required('Administrator')
    def edit_inverter(inverter_id):
        inverter = db.get_or_404(Inverter, inverter_id)
        config = db.session.get(MonitorConfig, inverter_id)
        if request.method == 'POST':
            fields, transport = inverter_fields(inverter), transport_fields(config)
            try:
                with resource_lock('inverter-inventory'):
                    check_duplicate(fields, transport, exclude=inverter.id)
                    # Blank password retains the existing secret, never returned in HTML.
                    if not fields['password']:
                        fields.pop('password')
                    for key, value in fields.items():
                        setattr(inverter, key, value)
                    if config is None:
                        config = MonitorConfig(inverter_id=inverter_id)
                        db.session.add(config)
                    for key, value in transport.items():
                        setattr(config, key, value)
                    db.session.commit()
            except OperationBusy:
                abort(409, 'Облік пристроїв зайнятий. Повторіть запит.')
            flash('Інвертор оновлено.', 'info')
            return redirect(url_for('index'))
        groups = db.session.scalars(db.select(Inverter.group).distinct()).all()
        return render_template('edit_inverter.html', inverter=inverter, groups=groups,
                               transport=config or load_configuration())

    @app.post('/delete/<int:inverter_id>')
    @roles_required('Administrator')
    def delete_inverter(inverter_id):
        return execute_control('delete', [db.get_or_404(Inverter, inverter_id)], 100)

    @app.post('/test/<int:inverter_id>')
    @roles_required(*CONTROL_ROLES)
    def test_connection(inverter_id):
        inv = db.get_or_404(Inverter, inverter_id)
        request_check(inv)
        db.session.commit()
        flash(f'{inv.name}: перевірку заплановано на наступний цикл монітора.', 'info')
        return redirect(url_for('index'))

    @app.post('/control')
    @roles_required(*CONTROL_ROLES)
    def control_inverters():
        actions = request.form.getlist('action')
        if len(actions) != 1 or actions[0] not in ('on', 'off', 'limit', 'limit_max', 'test', 'delete'):
            abort(400, 'Невідома або неоднозначна команда.')
        action = actions[0]
        if action == 'delete' and g.user.role != 'Administrator':
            abort(403)
        raw_ids = request.form.getlist('selected_ids')
        if not raw_ids or len(raw_ids) > 100 or any(not re.fullmatch(r'[1-9][0-9]{0,9}', x, re.ASCII) for x in raw_ids):
            abort(400, 'Некоректний список інверторів.')
        ids = [int(x) for x in raw_ids]
        if len(ids) != len(set(ids)):
            abort(400, 'Повторні ID не дозволені.')
        inverters = db.session.scalars(db.select(Inverter).where(Inverter.id.in_(ids)).order_by(Inverter.id)).all()
        if len(inverters) != len(ids):
            abort(400, 'Список містить невідомі інвертори.')
        value = 100
        if action == 'limit':
            values = request.form.getlist('limit_value')
            if len(values) != 1 or not re.fullmatch(r'(?:[1-9][0-9]?|100)', values[0], re.ASCII):
                abort(400, 'Відсоток має бути цілим числом від 1 до 100.')
            value = int(values[0])
        return execute_control(action, inverters, value)

    def execute_control(action, inverters, value):
        reason = block_reason(writes_enabled=app.config['MODBUS_WRITES_ENABLED'])
        if action not in ('delete', 'test') and reason:
            abort(403, reason)
        if action != 'test':
            payload = command_payload(g.user, action, value, inverters)
            transports = transports_for(inverters)
            if action != 'delete' and inverter_controls.enabled():
                try:
                    for inv in inverters: inverter_controls.plan(inv, transports[inv.id], action, value)
                except ValueError as error:
                    abort(400, str(error))
            tokens = request.form.getlist('confirmation_token')
            if not tokens:
                confirmation = sign_confirmation(app.config['SECRET_KEY'], payload)
                if action != 'delete':
                    db.session.add(CommandAudit(request_id='web:'+hashlib.sha256(confirmation.encode()).hexdigest()[:60],
                        source='web', user_id=g.user.id, username=g.user.username, occurred_at=time.time(),
                        action=action, targets=payload['targets'], result='preview', details=payload))
                    db.session.commit()
                preview = [dict(id=inv.id, name=inv.name, ip_address=inv.ip_address,
                    command=inverter_controls.plan(inv, transports[inv.id], action, value)
                    if action != 'delete' and inverter_controls.enabled() else None) for inv in inverters]
                if wants_json():
                    return jsonify(confirmation_required=True, confirmation_token=confirmation,
                                   action=action, label=ACTIONS[action], value=value, inverters=preview)
                return render_template('confirmation.html', confirmation_token=confirmation,
                    inverters=preview, action=action, label=ACTIONS[action], value=value)
            if len(tokens) != 1:
                abort(400, 'Неоднозначне підтвердження.')
            verify_confirmation(app.config['SECRET_KEY'], tokens[0], payload)
            if action != 'delete':
                intent = 'web:'+hashlib.sha256(tokens[0].encode()).hexdigest()[:60]
                claimed = db.session.execute(update(CommandAudit).where(CommandAudit.request_id==intent,
                    CommandAudit.user_id==g.user.id, CommandAudit.result=='preview').values(result='processing')).rowcount
                if claimed != 1:
                    db.session.rollback()
                    abort(400, 'Підтвердження вже використано або недійсне.')
                db.session.commit()  # Claim before I/O; never repeat after a timeout/DB failure.
        results = []
        if action == 'delete':
            for inv in inverters:
                results.append(dict(id=inv.id, name=inv.name, outcome='deleted', message='Інвертор видалено.'))
                remove_inverter(inv)
            db.session.commit()
        elif action == 'test':
            for inv in inverters:
                request_check(inv)
                results.append(dict(id=inv.id, name=inv.name, outcome='queued',
                                    message='Перевірку заплановано на наступний цикл монітора.'))
            db.session.commit()
        else:
            try:
                results = execute_devices(g.user, action, inverters, value,
                    writes_enabled=app.config['MODBUS_WRITES_ENABLED'], transports=transports)
            except (ControlBlocked, ValueError) as error:
                results = [dict(id=inv.id, name=inv.name, outcome='blocked', message=str(error)) for inv in inverters]
            except Exception:
                results = [dict(id=inv.id, name=inv.name, outcome='unknown', message='Результат невідомий; не повторюйте команду без перевірки.') for inv in inverters]
            try:
                db.session.execute(update(CommandAudit).where(CommandAudit.request_id==intent).values(
                    result='finished', details=results, occurred_at=time.time()))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                abort(503, 'Результат не збережено в аудиті. Перевірте стан інверторів; не повторюйте команду.')
        if wants_json():
            return jsonify(results=results)
        return render_template('results.html', results=results)

    @app.cli.command('init-db')
    def init_db():
        """Explicit schema initialization; back up existing databases first."""
        db.create_all()
        click.echo('Схему створено. Наявні записи не змінено.')

    @app.cli.command('configure-monitor')
    @click.argument('inverter_id', type=int)
    @click.option('--port', type=click.IntRange(1, 65535), default=502)
    @click.option('--device-id', type=click.IntRange(1, 255), default=1)
    @click.option('--read-address', type=click.IntRange(0, 65535), required=True)
    @click.option('--read-count', type=click.IntRange(1, 125), default=1)
    @click.option('--profile', type=click.Choice(['generic', 'legacy_ingeteam', 'ingecon_100tl_input']), default='generic')
    def configure_monitor(inverter_id, port, device_id, read_address, read_count, profile):
        """Configure a documented read register for this specific inverter; no network."""
        inverter = db.session.get(Inverter, inverter_id)
        if inverter is None:
            raise click.ClickException('Інвертор не знайдено.')
        if inverter.model == 'ingecon_sun_100tl' and profile != 'ingecon_100tl_input':
            raise click.ClickException('Для INGECON SUN 100TL використовується спільний профіль ingecon_100tl_input.')
        target = Target(inverter_id, inverter.ip_address, port, device_id, read_address, read_count, profile)
        try:
            target.validate()
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        config = db.session.get(MonitorConfig, inverter_id)
        if config is None:
            config = MonitorConfig(inverter_id=inverter_id)
            db.session.add(config)
        for field in ('port', 'device_id', 'read_address', 'read_count', 'profile'):
            setattr(config, field, getattr(target, field))
        if profile == 'ingecon_100tl_input':
            inverter.model = 'ingecon_sun_100tl'
        db.session.commit()
        click.echo('Профіль читання збережено без підключення до інвертора.')

    @app.cli.command('create-user')
    @click.argument('username')
    @click.option('--role', type=click.Choice(ROLES), default='Viewer', show_default=True)
    @click.password_option(confirmation_prompt=True)
    def create_user(username, role, password):
        username = username.strip().lower()
        if not USERNAME.fullmatch(username):
            raise click.ClickException('Ім’я: 1–64 символи a-z, 0-9, _, ., -.')
        if db.session.scalar(db.select(User).where(User.username == username)):
            raise click.ClickException('Користувач уже існує.')
        user = User(username=username, role=role)
        try:
            user.set_password(password)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        db.session.add(user)
        db.session.commit()
        click.echo('Користувача створено.')

    return app


def __getattr__(name):
    # Preserve app:app WSGI compatibility without opening/configuring the default
    # application when a separate factory caller imports create_app.
    if name == 'app':
        application = globals()['app'] = create_app()
        return application
    raise AttributeError(name)

if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5000, debug=False)
