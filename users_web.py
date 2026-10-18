"""Administrator-only account management; uses the existing schema and no network."""
import re
import time
import uuid

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import HTTPException

from models import CommandAudit, ROLES, User, db
from security import USERNAME, roles_required
from telegram_store import TelegramStore

users_web = Blueprint('users_web', __name__, url_prefix='/users')


def field(name):
    values = request.form.getlist(name)
    if len(values) != 1:
        abort(400, 'Некоректні поля форми.')
    return values[0]


def serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='user-delete-confirmation')


def deletion_payload(user):
    return dict(actor=[g.user.id, g.user.session_version],
                target=[user.id, user.username, user.role, user.active, user.session_version])


def lock_administrator():
    # Acquire SQLite's writer lock before checking/deleting accounts. Concurrent
    # administrative requests must recheck the actor after the preceding commit.
    count = db.session.execute(update(User).where(
        User.id == g.user.id, User.active.is_(True), User.role == 'Administrator',
        User.session_version == g.user.session_version
    ).values(session_version=User.session_version)).rowcount
    if count != 1:
        # A concurrent deletion can invalidate the ORM object used by templates.
        g.user = None
        session.clear()
        abort(403, 'Права адміністратора змінилися. Увійдіть знову.')


def protect_account(user):
    if user.id == g.user.id:
        abort(400, 'Не можна видалити власний обліковий запис.')
    if user.active and user.role == 'Administrator':
        remaining = db.session.scalar(db.select(func.count()).select_from(User).where(
            User.active.is_(True), User.role == 'Administrator', User.id != user.id))
        if not remaining:
            abort(400, 'Не можна видалити останнього активного адміністратора.')


def audit(action, user):
    db.session.add(CommandAudit(request_id='user:' + uuid.uuid4().hex, source='web',
        user_id=g.user.id, username=g.user.username, occurred_at=time.time(), action=action,
        targets=[dict(id=user.id, username=user.username, role=user.role)],
        result='success', details=[]))


@users_web.get('')
@roles_required('Administrator')
def index():
    page = request.args.get('page', '1')
    if not re.fullmatch(r'[1-9][0-9]{0,6}', page, re.ASCII):
        abort(400, 'Некоректна сторінка списку користувачів.')
    page = int(page)
    users = db.session.execute(db.select(User.id, User.username, User.role, User.active)
        .order_by(User.username).offset((page - 1) * 50).limit(51)).all()
    return render_template('users.html', users=users[:50], page=page, has_next=len(users) > 50)


@users_web.route('/add', methods=['GET', 'POST'])
@roles_required('Administrator')
def add():
    username, role, error = '', 'Viewer', None
    if request.method == 'POST':
        username, role = field('username').strip().lower(), field('role')
        password, confirmation = field('password'), field('password_confirmation')
        if not USERNAME.fullmatch(username):
            error = 'Ім’я: 1–64 символи a-z, 0-9, _, ., -.'
        elif role not in ROLES:
            error = 'Оберіть допустиму роль.'
        elif not 12 <= len(password) <= 1024:
            error = 'Пароль має містити від 12 до 1024 символів.'
        elif password != confirmation:
            error = 'Паролі не збігаються.'
        if error is None:
            user = User(username=username, role=role)
            user.set_password(password)
            try:
                lock_administrator()
                db.session.add(user)
                db.session.flush()
                audit('user_create', user)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                error = 'Користувач із таким іменем уже існує.'
            except OperationalError:
                db.session.rollback()
                g.user = None
                abort(503, 'База даних зайнята. Повторіть дію пізніше.')
            except HTTPException:
                db.session.rollback()
                raise
            else:
                flash('Користувача створено.', 'info')
                return redirect(url_for('.index'))
    # Never send passwords/hashes back to HTML, including validation failures.
    return render_template('user_form.html', username=username, selected_role=role,
                           roles=ROLES, error=error), (400 if error else 200)


@users_web.route('/<int:user_id>/delete', methods=['GET', 'POST'])
@roles_required('Administrator')
def delete(user_id):
    if request.method == 'GET':
        user = db.get_or_404(User, user_id)
        protect_account(user)
        return render_template('user_delete.html', target=user,
                               confirmation_token=serializer().dumps(deletion_payload(user)))
    try:
        signed = serializer().loads(field('confirmation_token'), max_age=120)
    except BadSignature:
        abort(400, 'Підтвердження недійсне або застаріло. Відкрийте його знову.')
    try:
        lock_administrator()
        user = db.session.get(User, user_id, populate_existing=True)
        if user is None:
            abort(404)
        protect_account(user)
        if signed != deletion_payload(user) or field('username_confirmation') != user.username:
            abort(400, 'Дані користувача або підтвердження змінилися. Перевірте їх знову.')
        # One transaction: no orphan link/code can grant access if SQLite reuses ID.
        TelegramStore.revoke_user(db.session, user.id)
        audit('user_delete', user)
        db.session.delete(user)
        db.session.commit()
    except OperationalError:
        db.session.rollback()
        g.user = None
        abort(503, 'База даних зайнята. Повторіть дію пізніше.')
    except HTTPException:
        db.session.rollback()
        raise
    flash('Користувача видалено. Його доступ до порталу та Telegram відкликано.', 'info')
    return redirect(url_for('.index'))
