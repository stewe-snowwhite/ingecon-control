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
