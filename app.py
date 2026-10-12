import ipaddress
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for, jsonify)
from flask.helpers import get_debug_flag
from flask_wtf.csrf import CSRFProtect

from models import db, Inverter, ROLES, User, MonitorConfig, InverterStatus
from monitoring import current_statuses, Target, explain_reason
from modbus_utils import modbus_limit_power, modbus_turn_off, modbus_turn_on
from security import authenticate, load_user, log_failure, reserve_login_attempt, roles_required

csrf = CSRFProtect()
ALL_ROLES = ROLES
CONTROL_ROLES = ('Administrator', 'Operator')
USERNAME = re.compile(r'[a-z0-9_.-]{1,64}', re.ASCII)


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('INGECON_SECRET_KEY'),
        SQLALCHEMY_DATABASE_URI=os.environ.get('INGECON_DATABASE_URI', 'sqlite:///database.db'),
        DEBUG=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=64 * 1024,
        MODBUS_WRITES_ENABLED=False,
        MONITOR_STALE_AFTER_SECONDS=None,
        LOGIN_IP_LIMIT=20,
        LOGIN_ACCOUNT_LIMIT=5,
        LOGIN_WINDOW_SECONDS=300,
        MODBUS_LOG_PATH=str(Path(app.root_path) / 'modbus.log'),
    )
    if config:
        app.config.update(config)
    if not isinstance(app.config['SECRET_KEY'], str) or len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('Set INGECON_SECRET_KEY to a random secret of at least 32 characters.')
    if app.debug or get_debug_flag():
        raise RuntimeError('Debug mode is forbidden.')
    db.init_app(app)
    app.before_request(load_user)
    csrf.init_app(app)
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
        # Existing Bootstrap CDN and inline JS retained until the dashboard stage.
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(401)
    def login_required(error):
        return render_template('unauthorized.html'), 401

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
        g.user.session_version += 1
        db.session.commit()
        session.clear()
        return redirect(url_for('login'))

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
        selected_group = request.args.get('group')
        groups = db.session.scalars(db.select(Inverter.group).distinct()).all()
        snapshot = current_statuses(db.session, group=selected_group,
                                    stale_after=app.config['MONITOR_STALE_AFTER_SECONDS'])
        return render_template('index.html', inverters=snapshot['inverters'], groups=groups,
                               selected_group=selected_group, monitor=snapshot['monitor'])

    @app.get('/api/statuses')
    @roles_required(*ALL_ROLES)
    def statuses():
        return jsonify(current_statuses(db.session, group=request.args.get('group'),
                                        stale_after=app.config['MONITOR_STALE_AFTER_SECONDS']))

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

    def inverter_fields():
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
        if len(fields['login']) > 100 or len(password) > 100:
            abort(400, 'Завеликі облікові дані інвертора.')
        return dict(name=fields['name'], group=fields['group'], ip_address=str(address),
                    login=fields['login'], password=password)

    @app.route('/add', methods=['GET', 'POST'])
    @roles_required('Administrator')
    def add_inverter():
        if request.method == 'POST':
            db.session.add(Inverter(**inverter_fields()))
            db.session.commit()
            flash('Інвертор додано без перевірки підключення.', 'info')
            return redirect(url_for('index'))
        groups = db.session.scalars(db.select(Inverter.group).distinct()).all()
        return render_template('add_inverter.html', groups=groups)

    @app.route('/edit/<int:inverter_id>', methods=['GET', 'POST'])
    @roles_required('Administrator')
    def edit_inverter(inverter_id):
        inverter = db.get_or_404(Inverter, inverter_id)
        if request.method == 'POST':
            fields = inverter_fields()
            # Blank password means retain current value; never send it back to HTML.
            if not fields['password']:
                fields.pop('password')
            for key, value in fields.items():
                setattr(inverter, key, value)
            db.session.commit()
            flash('Інвертор оновлено.', 'info')
            return redirect(url_for('index'))
        groups = db.session.scalars(db.select(Inverter.group).distinct()).all()
        return render_template('edit_inverter.html', inverter=inverter, groups=groups)

    @app.post('/delete/<int:inverter_id>')
    @roles_required('Administrator')
    def delete_inverter(inverter_id):
        remove_inverter(db.get_or_404(Inverter, inverter_id))
        db.session.commit()
        flash('Інвертор видалено.', 'info')
        return redirect(url_for('index'))

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
        if action == 'delete':
            for inv in inverters:
                remove_inverter(inv)
            db.session.commit()
            flash(f'Видалено {len(inverters)} інверторів.', 'info')
        elif action == 'test':
            for inv in inverters:
                request_check(inv)
            db.session.commit()
            flash('Перевірку заплановано на наступний цикл монітора.', 'info')
        else:
            if app.config['MODBUS_WRITES_ENABLED'] is not True:
                abort(403, 'Modbus-записи заблоковано.')
            for inv in inverters:
                if action == 'on':
                    result = modbus_turn_on(inv.ip_address, allow_write=True)
                elif action == 'off':
                    result = modbus_turn_off(inv.ip_address, allow_write=True)
                else:
                    result = modbus_limit_power(inv.ip_address, value=value, allow_write=True)
                flash(f'{inv.name}: {result.message}', 'info' if result.confirmed else 'danger')
        return redirect(url_for('index'))

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
    @click.option('--profile', type=click.Choice(['generic', 'legacy_ingeteam']), default='generic')
    def configure_monitor(inverter_id, port, device_id, read_address, read_count, profile):
        """Configure a documented read register for this specific inverter; no network."""
        inverter = db.session.get(Inverter, inverter_id)
        if inverter is None:
            raise click.ClickException('Інвертор не знайдено.')
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


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
