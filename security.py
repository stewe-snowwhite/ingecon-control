"""Session authentication and persistent, atomic SQLite login throttling."""
import hashlib
import ipaddress
import logging
import secrets
import time
from functools import wraps

from flask import abort, current_app, g, request, session
from sqlalchemy import case, delete
from sqlalchemy.dialects.sqlite import insert
from werkzeug.security import check_password_hash, generate_password_hash

from models import db, LoginThrottle, User

security_log = logging.getLogger('ingecon.auth')
# Used for non-existent/inactive accounts to avoid skipping password hashing.
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32), method='scrypt')


def client_ip():
    # Do not trust X-Forwarded-For without a separately configured trusted proxy.
    try:
        return str(ipaddress.ip_address(request.remote_addr))
    except (ValueError, TypeError):
        return 'unknown'


def log_failure(reason):
    # Only controlled values, never credentials or attacker-controlled usernames.
    security_log.warning('AUTH_FAILURE ip=%s reason=%s', client_ip(), reason)


def load_user():
    g.user = None
    uid = session.get('user_id')
    if isinstance(uid, int):
        user = db.session.get(User, uid)
        if user and user.active and user.role in ('Administrator', 'Operator', 'Viewer'):
            if session.get('session_version') == user.session_version:
                g.user = user
    if uid is not None and g.user is None:
        session.clear()


def roles_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                abort(401)
            if g.user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def reserve_login_attempt(username):
    """Count all attempts, including successes; no reset loophole or process-local state."""
    now = int(time.time())
    window = current_app.config['LOGIN_WINDOW_SECONDS']
    counts = []
    for scope, value, limit in (
        ('ip', client_ip(), current_app.config['LOGIN_IP_LIMIT']),
        ('account', username, current_app.config['LOGIN_ACCOUNT_LIMIT']),
    ):
        key = hashlib.sha256(f'{scope}:{value}'.encode()).hexdigest()
        stmt = insert(LoginThrottle).values(key=key, attempts=1, expires_at=now + window)
        expired = LoginThrottle.expires_at <= now
        stmt = stmt.on_conflict_do_update(
            index_elements=['key'],
            set_={'attempts': case((expired, 1), else_=LoginThrottle.attempts + 1),
                  'expires_at': case((expired, now + window), else_=LoginThrottle.expires_at)},
        ).returning(LoginThrottle.attempts, LoginThrottle.expires_at)
        count, expires = db.session.execute(stmt).one()
        counts.append((count, limit, expires))
    db.session.execute(delete(LoginThrottle).where(LoginThrottle.expires_at < now - 86400))
    db.session.commit()
    return max((expires-now for count, limit, expires in counts if count > limit), default=0)


def authenticate(username, password):
    user = db.session.scalar(db.select(User).where(User.username == username))
    candidate = user.password_hash if user and user.active else DUMMY_HASH
    valid = check_password_hash(candidate, password)
    return user if valid and user and user.active else None
