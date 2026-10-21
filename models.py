"""Database models; existing inverter schema is preserved."""
import time
import secrets
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
        # SQLite may reuse a deleted account's integer ID. A fresh generation
        # prevents its old signed cookies from authenticating as the new user.
        self.session_version = (self.session_version + 1 if self.session_version is not None
                                else secrets.randbits(62) + 1)

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


class TelegramLinkCode(db.Model):
    user_id = db.Column(db.Integer, primary_key=True)
    digest = db.Column(db.String(64), nullable=False, unique=True)
    user_version = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.Float, nullable=False)


class TelegramLink(db.Model):
    telegram_id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, unique=True)
    user_version = db.Column(db.Integer, nullable=False)
    linked_at = db.Column(db.Float, nullable=False)
    notifications = db.Column(db.Boolean, nullable=False, default=False)
    online = db.Column(db.Boolean, nullable=False, default=True)
    offline = db.Column(db.Boolean, nullable=False, default=True)
    degraded = db.Column(db.Boolean, nullable=False, default=True)
    group_filter = db.Column(db.String(100), nullable=True)
    last_event_id = db.Column(db.Integer, nullable=False, default=0)


class TelegramCommand(db.Model):
    id = db.Column(db.String(32), primary_key=True)
    telegram_id = db.Column(db.BigInteger, nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    payload = db.Column(db.JSON, nullable=False)
    group_name = db.Column(db.String(100), nullable=True)
    expires_at = db.Column(db.Float, nullable=False, index=True)
    state = db.Column(db.String(16), nullable=False, default='awaiting')


class CommandAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(64), nullable=False, unique=True)
    source = db.Column(db.String(16), nullable=False)
    telegram_id = db.Column(db.BigInteger, nullable=True, index=True)
    user_id = db.Column(db.Integer, nullable=True)
    username = db.Column(db.String(64), nullable=True)
    occurred_at = db.Column(db.Float, nullable=False, index=True)
    action = db.Column(db.String(16), nullable=False)
    targets = db.Column(db.JSON, nullable=False)
    result = db.Column(db.String(32), nullable=False)
    details = db.Column(db.JSON, nullable=False)


class TelegramDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, nullable=False)
    telegram_id = db.Column(db.BigInteger, nullable=False)
    state = db.Column(db.String(16), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.Float, nullable=False, default=0)
    sent_at = db.Column(db.Float, nullable=True)
    message_id = db.Column(db.BigInteger, nullable=True)
    error = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.Float, nullable=False, default=time.time, index=True)
    __table_args__ = (db.UniqueConstraint('event_id','telegram_id'),)


class TelegramUpdate(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    received_at = db.Column(db.Float, nullable=False, index=True)


class TelegramRate(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    attempts = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.Float, nullable=False, index=True)


TELEGRAM_TABLES = [model.__table__ for model in (TelegramLinkCode, TelegramLink, TelegramCommand,
                   CommandAudit, TelegramDelivery, TelegramUpdate, TelegramRate)]


class DiscoverySubnet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cidr = db.Column(db.String(18), nullable=False, unique=True)
    interface = db.Column(db.String(15), nullable=False)
    mode = db.Column(db.String(8), nullable=False)
    approved_by = db.Column(db.Integer, nullable=False)
    approved_at = db.Column(db.Float, nullable=False)


class DiscoveryScan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    user_version = db.Column(db.Integer, nullable=False)
    subnets = db.Column(db.JSON, nullable=False)
    device_id = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(16), nullable=False, default='queued', index=True)
    total = db.Column(db.Integer, nullable=False)
    processed = db.Column(db.Integer, nullable=False, default=0)
    found = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.Float, nullable=False)
    heartbeat = db.Column(db.Float, nullable=True)
    finished_at = db.Column(db.Float, nullable=True)
    error = db.Column(db.String(256), nullable=True)


class DiscoveryResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey('discovery_scan.id'), nullable=False, index=True)
    ip = db.Column(db.String(15), nullable=False)
    mac = db.Column(db.String(17), nullable=True)
    cidr = db.Column(db.String(18), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    device_id = db.Column(db.Integer, nullable=False)
    tcp_ok = db.Column(db.Boolean, nullable=False)
    modbus_ok = db.Column(db.Boolean, nullable=False)
    confidence = db.Column(db.String(16), nullable=False)
    reason = db.Column(db.String(256), nullable=False)
    __table_args__ = (db.UniqueConstraint('scan_id', 'ip'),)


DISCOVERY_TABLES = [model.__table__ for model in (DiscoverySubnet, DiscoveryScan, DiscoveryResult)]
