"""Backend deployment interlock, intentionally not configurable by HTTP or env.

Changing this interlock requires a separate reviewed activation of verified maps.
Tests may override it only while all device transports are mocked.
"""
PHYSICAL_WRITES_LOCKED = True
WRITE_LOCK_REASON = 'Фізичний Modbus-запис заблоковано на backend; історичні команди непідтверджені.'


def writes_locked():
    return PHYSICAL_WRITES_LOCKED is not False
