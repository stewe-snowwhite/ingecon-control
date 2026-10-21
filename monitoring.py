"""Read-only probes, state machine, and DB-only status serialization."""
import hashlib
import ipaddress
import json
import math
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

from pymodbus.exceptions import ModbusException
from sqlalchemy import select, update

import modbus_utils
from models import Inverter, InverterStatus, MonitorConfig, MonitorHeartbeat, StateEvent
from operation_locks import OperationBusy, endpoint_key, resource_lock
from input_registers import INPUT_BLOCKS, telemetry, operating_state_label


@dataclass(frozen=True)
class Target:
    inverter_id: int
    host: str
    port: int = 502
    device_id: int = 1
    read_address: int | None = None
    read_count: int = 1
    profile: str = 'generic'

    @property
    def key(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()

    def validate(self):
        if any(type(value) is not int for value in (self.port, self.device_id, self.read_count)):
            raise ValueError('invalid_integer_configuration')
        if self.read_address is not None and type(self.read_address) is not int:
            raise ValueError('invalid_integer_configuration')
        address = ipaddress.ip_address(self.host)
        if address.is_multicast or address.is_unspecified or address.is_loopback:
            raise ValueError('invalid_address')
        if not 1 <= self.port <= 65535 or not 1 <= self.device_id <= 255:
            raise ValueError('invalid_endpoint')
        if not 1 <= self.read_count <= 125:
            raise ValueError('invalid_count')
        if self.read_address is not None and not 0 <= self.read_address <= 65536-self.read_count:
            raise ValueError('invalid_register_range')
        if self.profile not in ('generic', 'legacy_ingeteam', 'ingecon_100tl_input'):
            raise ValueError('invalid_profile')
        if self.profile == 'legacy_ingeteam' and (self.read_address, self.read_count) != (1000, 2):
            raise ValueError('legacy_profile_requires_1000_2')
        if self.profile == 'ingecon_100tl_input' and (self.read_address, self.read_count) != INPUT_BLOCKS[0]:
            raise ValueError('input_profile_requires_20_16')


def target_for(inverter, config):
    if getattr(inverter, 'model', None) == 'ingecon_sun_100tl':
        return Target(inverter.id, inverter.ip_address,
                      config.port if config else 502, config.device_id if config else 1,
                      *INPUT_BLOCKS[0], 'ingecon_100tl_input')
    if config is None:
        return Target(inverter.id, inverter.ip_address)
    return Target(inverter.id, inverter.ip_address, config.port, config.device_id,
                  config.read_address, config.read_count, config.profile)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool | None
    latency_ms: float | None = None
    error: str | None = None
    registers: tuple = ()


@dataclass(frozen=True)
class Observation:
    target: Target
    checked_at: float
    icmp: ProbeResult
    tcp: ProbeResult
    modbus: ProbeResult


class NetworkProbes:
    """Independent bounded FC03/FC04 probes. No Modbus write API is used."""
    def __init__(self, timeout=2, runner=None, clock=time.monotonic):
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 10:
            raise ValueError('Probe timeout must be 0.1–10 seconds')
        self.timeout = timeout
        self.runner = runner or subprocess.run
        self.clock = clock

    def elapsed(self, start):
        return round((self.clock()-start)*1000, 3)

    def icmp(self, target):
        executable = shutil.which('ping')
        if executable is None:
            return ProbeResult(None, error='icmp_unavailable')
        start = self.clock()
        try:
            response = self.runner(
                [executable, '-n', '-c', '1', '-W', str(self.timeout), '--', target.host],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.timeout+0.2, check=False,
            )
            if response.returncode == 0:
                return ProbeResult(True, self.elapsed(start))
            if response.returncode == 1:
                return ProbeResult(False, self.elapsed(start), 'icmp_no_reply')
            return ProbeResult(None, self.elapsed(start), 'icmp_error')
        except subprocess.TimeoutExpired:
            return ProbeResult(False, self.elapsed(start), 'icmp_timeout')
        except OSError:
            return ProbeResult(None, self.elapsed(start), 'icmp_unavailable')

    def tcp(self, target):
        start = self.clock()
        try:
            with resource_lock(endpoint_key(target.host, target.port)):
                with socket.create_connection((target.host, target.port), timeout=self.timeout):
                    return ProbeResult(True, self.elapsed(start))
        except OperationBusy:
            return ProbeResult(None, error='tcp_busy')
        except TimeoutError:
            return ProbeResult(False, self.elapsed(start), 'tcp_timeout')
        except OSError:
            return ProbeResult(False, self.elapsed(start), 'tcp_unreachable')

    def modbus(self, target):
        if target.read_address is None:
            return ProbeResult(None, error='modbus_not_configured')
        start = self.clock()
        client = None
        try:
            with resource_lock(endpoint_key(target.host, target.port)):
                try:
                    client = modbus_utils.ModbusTcpClient(host=target.host, port=target.port,
                                                         timeout=self.timeout, retries=0)
                    if not client.connect():
                        return ProbeResult(False, self.elapsed(start), 'modbus_unreachable')
                    if target.profile == 'ingecon_100tl_input':
                        values = []
                        for address, count in INPUT_BLOCKS:
                            reply = client.read_input_registers(address, count=count, device_id=target.device_id)
                            registers = getattr(reply, 'registers', None)
                            if reply is None or reply.isError() or not isinstance(registers, (list, tuple)) or len(registers) != count or any(
                                type(value) is not int or not 0 <= value <= 65535 for value in registers):
                                return ProbeResult(False, self.elapsed(start), 'modbus_invalid_response')
                            values.extend(registers)
                        return ProbeResult(True, self.elapsed(start), registers=tuple(values))
                    reply = client.read_holding_registers(target.read_address,
                                                         count=target.read_count, device_id=target.device_id)
                    if reply is None:
                        return ProbeResult(False, self.elapsed(start), 'modbus_no_response')
                    if reply.isError():
                        return ProbeResult(False, self.elapsed(start), 'modbus_exception_response')
                    registers = getattr(reply, 'registers', None)
                    if not isinstance(registers, (list, tuple)) or len(registers) != target.read_count:
                        return ProbeResult(False, self.elapsed(start), 'modbus_invalid_response')
                    if any(type(value) is not int or not 0 <= value <= 65535 for value in registers):
                        return ProbeResult(False, self.elapsed(start), 'modbus_invalid_response')
                    return ProbeResult(True, self.elapsed(start), registers=tuple(registers))
                finally:
                    if client is not None:
                        client.close()
        except OperationBusy:
            return ProbeResult(None, error='modbus_busy')
        except TimeoutError:
            return ProbeResult(False, self.elapsed(start), 'modbus_timeout')
        except (OSError, ModbusException):
            return ProbeResult(False, self.elapsed(start), 'modbus_transport_error')

    def poll(self, target):
        try:
            target.validate()
        except (ValueError, TypeError):
            unknown = ProbeResult(None, error='invalid_target_configuration')
            return Observation(target, time.time(), unknown, unknown, unknown)
        # Never short-circuit TCP or Modbus because ICMP/TCP failed.
        icmp = self.icmp(target)
        tcp = self.tcp(target)
        modbus = self.modbus(target)
        return Observation(target, time.time(), icmp, tcp, modbus)


def apply_observation(session, inverter, observation, stale_after):
    """Persist one current row; create outbox/history only on a state change."""
    target = observation.target
    status = session.get(InverterStatus, inverter.id)
    previous = status.state if status else 'Unknown'
    if status is None:
        status = InverterStatus(inverter_id=inverter.id, target_key=target.key, checked_at=observation.checked_at,
                                state='Unknown', successes=0, failures=0)
        session.add(status)
    old_age = observation.checked_at-status.checked_at
    changed_target = status.target_key != target.key
    if changed_target or old_age > stale_after or old_age < 0:
        status.successes = status.failures = 0
        if changed_target:
            status.last_success_at = None
    status.target_key = target.key
    status.checked_at = observation.checked_at
    status.latency_ms = observation.modbus.latency_ms
    reasons = [r.error for r in (observation.tcp, observation.modbus) if r.error]
    if observation.modbus.ok is None:
        status.state = 'Unknown'
        status.successes = status.failures = 0
    elif observation.modbus.ok:
        status.failures = 0
        status.last_success_at = observation.checked_at
        if observation.tcp.ok is True:
            status.successes = min(status.successes+1, 2)
            if status.successes >= 2:
                status.state = 'Online'
            else:
                status.state = 'Unknown' if previous == 'Unknown' or changed_target else 'Degraded'
                reasons.append('awaiting_second_success')
        else:
            status.successes = 0
            status.state = 'Degraded'
    else:
        status.successes = 0
        status.failures = min(status.failures+1, 3)
        status.state = 'Offline' if status.failures >= 3 else 'Degraded'
    status.error_reason = ';'.join(reasons) or None
    for name in ('icmp', 'tcp', 'modbus'):
        result = getattr(observation, name)
        setattr(status, name+'_ok', result.ok)
        setattr(status, name+'_error', result.error)
        if name != 'modbus':
            setattr(status, name+'_latency_ms', result.latency_ms)
    status.power_limit_percent = None
    for field in ('operating_state_code', 'active_power_raw', 'active_power_w', 'limit_reason_code'):
        setattr(status, field, None)
    if target.profile == 'ingecon_100tl_input' and observation.modbus.ok:
        for field, value in telemetry(observation.modbus.registers).items():
            setattr(status, field, value)
    if target.profile == 'legacy_ingeteam' and observation.modbus.ok:
        cmd, limit = observation.modbus.registers
        if cmd == 16 and 0 <= limit <= 32767:
            status.power_limit_percent = round(limit / 32767 * 100, 1)
    if status.state != previous:
        session.add(StateEvent(inverter_id=inverter.id, inverter_name=inverter.name, target_key=target.key,
                               previous_state=previous, state=status.state,
                               occurred_at=observation.checked_at, reason=status.error_reason))
    return status


def heartbeat_summary(heartbeat, now, stale_after=None):
    interval = heartbeat.interval_seconds if heartbeat else 30
    ttl = stale_after if stale_after is not None else interval*3
    age = now-heartbeat.last_seen if heartbeat else None
    alive = heartbeat is not None and not heartbeat.stopped and 0 <= age <= ttl
    return {'alive': alive, 'last_seen': heartbeat.last_seen if heartbeat else None,
            'interval_seconds': interval, 'stale_after_seconds': ttl,
            'reason': None if alive else 'monitor_stopped' if heartbeat and heartbeat.stopped else 'monitor_stale',
            'last_error': heartbeat.last_error if heartbeat else None,
            'cycle_duration_seconds': heartbeat.cycle_duration_seconds if heartbeat else None}


def current_statuses(session, *, group=None, now=None, stale_after=None):
    now = time.time() if now is None else now
    heartbeat = heartbeat_summary(session.get(MonitorHeartbeat, 1), now, stale_after)
    stmt = select(Inverter, InverterStatus, MonitorConfig).outerjoin(
        InverterStatus, Inverter.id == InverterStatus.inverter_id).outerjoin(
        MonitorConfig, Inverter.id == MonitorConfig.inverter_id).order_by(Inverter.id)
    if group:
        stmt = stmt.where(Inverter.group == group)
    items = []
    for inv, status, config in session.execute(stmt):
        same_target = status is not None and status.target_key == target_for(inv, config).key
        age = now-status.checked_at if status else None
        fresh = same_target and heartbeat['alive'] and 0 <= age <= heartbeat['stale_after_seconds']
        reason = status.error_reason if status else 'not_polled'
        if status is not None and not same_target:
            reason = 'target_changed'
        elif not heartbeat['alive']:
            reason = heartbeat['reason']
        elif status is not None and not fresh:
            reason = 'status_stale'
        items.append({'id': inv.id, 'name': inv.name, 'group': inv.group, 'ip_address': inv.ip_address,
                      'state': status.state if fresh else 'Unknown',
                      'check_requested': config is not None and config.requested_at is not None,
                      'recorded_state': status.state if status else 'Unknown', 'stale': not fresh,
                      'checked_at': status.checked_at if same_target else None,
                      'last_success_at': status.last_success_at if same_target else None,
                      'latency_ms': status.latency_ms if fresh else None, 'reason': reason,
                      'power_limit_percent': status.power_limit_percent if fresh else None,
                      'model': inv.model,
                      'operating_state_code': status.operating_state_code if fresh else None,
                      'operating_state_label': operating_state_label(status.operating_state_code) if fresh else None,
                      'active_power_raw': status.active_power_raw if fresh else None,
                      'active_power_w': status.active_power_w if fresh else None,
                      'limit_reason_code': status.limit_reason_code if fresh else None,
                      'checks': {name: {'ok': getattr(status, name+'_ok') if fresh else None,
                                       'latency_ms': (getattr(status, name+'_latency_ms') if name != 'modbus' else status.latency_ms) if fresh else None,
                                       'error': getattr(status, name+'_error') if fresh else None}
                                 for name in ('icmp', 'tcp', 'modbus')}})
    return {'monitor': heartbeat, 'inverters': items}


def pending_notifications(session, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError('Invalid batch size')
    return session.scalars(select(StateEvent).where(StateEvent.notified_at.is_(None)).order_by(StateEvent.id).limit(limit)).all()


def acknowledge_notification(session, event_id, *, now=None):
    """Call only after delivery; caller owns the transaction. Never sends messages."""
    now = time.time() if now is None else now
    result = session.execute(update(StateEvent).where(
        StateEvent.id == event_id, StateEvent.notified_at.is_(None)).values(notified_at=now))
    return result.rowcount == 1


REASON_LABELS = {
    'monitor_stopped': 'Монітор зупинено.',
    'monitor_stale': 'Немає актуального heartbeat монітора.',
    'not_polled': 'Перевірки ще не виконувалися.',
    'target_changed': 'Адресу або профіль змінено; очікується нова перевірка.',
    'status_stale': 'Дані перевірки застаріли.',
    'awaiting_second_success': 'Очікується друга успішна перевірка.',
    'modbus_not_configured': 'Потрібен підтверджений профіль читання Modbus.',
    'modbus_busy': 'Інша операція Modbus ще виконується.',
    'tcp_busy': 'Підключення зайняте іншою перевіркою.',
    'tcp_timeout': 'Перевищено час TCP-підключення.',
    'tcp_unreachable': 'TCP-порт недоступний.',
    'modbus_unreachable': 'Не вдалося встановити Modbus-підключення.',
    'modbus_no_response': 'Немає відповіді Modbus.',
    'modbus_timeout': 'Перевищено час відповіді Modbus.',
    'modbus_exception_response': 'Modbus відхилив читання; перевірте профіль регістрів.',
    'modbus_invalid_response': 'Відповідь Modbus некоректна.',
    'modbus_transport_error': 'Помилка зв’язку Modbus.',
    'invalid_target_configuration': 'Некоректні налаштування перевірки.',
}


def explain_reason(reason):
    return ' '.join(REASON_LABELS.get(code, 'Перевірку не завершено.') for code in (reason or '').split(';') if code)
