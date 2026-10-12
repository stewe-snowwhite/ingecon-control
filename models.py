"""Database models; existing inverter schema is preserved."""
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
ROLES = ('Administrator', 'Operator', 'Viewer')


class Inverter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    group = db.Column(db.String(100), nullable=False)
    ip_address = db.Column(db.String(100), nullable=False)
    login = db.Column(db.String(100), nullable=True)
    password = db.Column(db.String(100), nullable=True)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.CheckConstraint("role IN ('Administrator', 'Operator', 'Viewer')"),)

    def set_password(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError('Пароль має містити від 12 до 1024 символів.')
        self.password_hash = generate_password_hash(password, method='scrypt')
        self.session_version = (self.session_version or 0) + 1

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class LoginThrottle(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    attempts = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.Integer, nullable=False, index=True)


class MonitorConfig(db.Model):
    """Explicit read profile; no register is assumed safe for an unknown model."""
    inverter_id = db.Column(db.Integer, db.ForeignKey('inverter.id'), primary_key=True)
    port = db.Column(db.Integer, nullable=False, default=502)
    device_id = db.Column(db.Integer, nullable=False, default=1)
    read_address = db.Column(db.Integer, nullable=True)
    read_count = db.Column(db.Integer, nullable=False, default=1)
    profile = db.Column(db.String(32), nullable=False, default='generic')
    requested_at = db.Column(db.Float, nullable=True)


class InverterStatus(db.Model):
    inverter_id = db.Column(db.Integer, db.ForeignKey('inverter.id'), primary_key=True)
    target_key = db.Column(db.String(64), nullable=False)
    state = db.Column(db.String(16), nullable=False, default='Unknown')
    checked_at = db.Column(db.Float, nullable=False)
    last_success_at = db.Column(db.Float, nullable=True)
    latency_ms = db.Column(db.Float, nullable=True)
    error_reason = db.Column(db.String(256), nullable=True)
    successes = db.Column(db.Integer, nullable=False, default=0)
    failures = db.Column(db.Integer, nullable=False, default=0)
    icmp_ok = db.Column(db.Boolean, nullable=True)
    tcp_ok = db.Column(db.Boolean, nullable=True)
    modbus_ok = db.Column(db.Boolean, nullable=True)
    icmp_latency_ms = db.Column(db.Float, nullable=True)
    tcp_latency_ms = db.Column(db.Float, nullable=True)
    icmp_error = db.Column(db.String(64), nullable=True)
    tcp_error = db.Column(db.String(64), nullable=True)
    modbus_error = db.Column(db.String(64), nullable=True)
    power_limit_percent = db.Column(db.Float, nullable=True)
    __table_args__ = (db.CheckConstraint("state IN ('Online', 'Offline', 'Degraded', 'Unknown')"),)


class StateEvent(db.Model):
    """Transition outbox for a future Telegram consumer; no credentials."""
    id = db.Column(db.Integer, primary_key=True)
    # No FK: keep history even when an inverter is deleted.
    inverter_id = db.Column(db.Integer, nullable=False, index=True)
    inverter_name = db.Column(db.String(100), nullable=False)
    target_key = db.Column(db.String(64), nullable=False)
    previous_state = db.Column(db.String(16), nullable=False)
    state = db.Column(db.String(16), nullable=False)
    occurred_at = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(256), nullable=True)
    notified_at = db.Column(db.Float, nullable=True, index=True)


class MonitorHeartbeat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner = db.Column(db.String(64), nullable=False)
    started_at = db.Column(db.Float, nullable=False)
    last_seen = db.Column(db.Float, nullable=False)
    interval_seconds = db.Column(db.Float, nullable=False)
    stopped = db.Column(db.Boolean, nullable=False, default=False)
    last_error = db.Column(db.String(128), nullable=True)
    cycle_duration_seconds = db.Column(db.Float, nullable=False, default=0)
