"""Presentation timezone only; persisted/API timestamps remain Unix seconds."""
from datetime import datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo('Europe/Kyiv')


def format_kyiv(value):
    return datetime.fromtimestamp(value, KYIV).strftime('%Y-%m-%d %H:%M:%S') if value is not None else '—'
