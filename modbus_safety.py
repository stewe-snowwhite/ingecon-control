"""Legacy/raw backend interlock, not configurable by HTTP or env.

Owner-authorized fixed 3Play commands live in inverter_controls; they do not
unlock this legacy/raw path or accept arbitrary register addresses.
Tests may override it only while all device transports are mocked.
"""
PHYSICAL_WRITES_LOCKED = True
WRITE_LOCK_REASON = 'Фізичний Modbus-запис заблоковано на backend; історичні команди непідтверджені.'


def writes_locked():
    return PHYSICAL_WRITES_LOCKED is not False
