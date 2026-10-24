"""Measured-power feedback. No rated powers, device I/O or hard-cap claims."""
import math

MAX_DOWN_STEP = 20
MAX_UP_STEP = 2
ALGORITHM = 'measured_feedback_v1'
OUTAGE_MARGIN = 1.2


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fresh(row):
    return (not row.get('stale', True) and row.get('checks', {}).get('modbus', {}).get('ok') is True
            and number(row.get('active_power_w')))


def remember(history, row):
    """Retain observed peaks across outages; never age a missing generator to zero.

    Full-output extrapolation is an estimate, NOT a certified upper bound. A PV
    array can produce more later as irradiance changes, even without commands.
    """
    ident = str(row['id'])
    cap = row.get('effective_percent', row.get('power_limit_percent'))
    stamp = row.get('checked_at', 0)
    if not number(cap) or not 0 <= cap <= 100 or not number(stamp):
        return
    old = history.get(ident, {})
    if old and stamp <= old['at']:
        return
    power = max(0, row['active_power_w'])
    peak = max(power, old.get('peak_w', 0))
    scale = max(old.get('w_per_percent', 0), power / max(cap, 1), peak / 100)
    cap_peak = max(power, old.get('cap_peak_w', old.get('power_w', 0))) if old.get('percent') == cap else power
    flat = old.get('flat_increases', 0)
    if old:
        delta = cap-old['percent']
        gain = max(100, old['w_per_percent'])
        if delta > 0:
            flat = min(3, flat+1) if power-old['power_w'] <= max(100, delta*gain*.25) else 0
        elif delta < 0 or power-old['power_w'] > max(100, gain*.5):
            flat = 0
    history[ident] = dict(at=stamp, power_w=power, percent=cap, peak_w=peak, w_per_percent=scale,
                         cap_peak_w=cap_peak, reserve_w=cap_peak*OUTAGE_MARGIN, flat_increases=flat)


def allocation(target_w, rows, history=None, *, blocked=()):
    if (type(target_w) is not int or not 1 <= target_w <= 100_000_000 or not 1 <= len(rows) <= 100
            or len({row['id'] for row in rows}) != len(rows)):
        raise ValueError('Некоректна ціль або список інверторів.')
    history = {key: dict(value) for key, value in (history or {}).items() if key in {str(r['id']) for r in rows}}
    blocked = {str(value) for value in blocked}
    for row in rows:
        if fresh(row):
            remember(history, row)
    controlled, fixed, missing, unknown = [], [], [], []
    reserve = 0
    for row in rows:
        ident = str(row['id'])
        cap = row.get('effective_percent', row.get('power_limit_percent'))
        if (fresh(row) and row['state'] == 'Online' and ident not in blocked
                and number(cap) and 0 <= cap <= 100):
            controlled.append(row)
        elif fresh(row):
            # A reachable but uncontrolled unit is measured, never assumed stopped.
            fixed.append(ident)
            reserve += max(0, row['active_power_w'])
        else:
            missing.append(ident)
            sample = history.get(ident)
            estimate = sample.get('reserve_w', sample['power_w']*OUTAGE_MARGIN) if sample else 0
            if estimate <= 0:
                unknown.append(ident)
            reserve += estimate
    known = [row for row in rows if fresh(row)]
    actual = sum(max(0, row['active_power_w']) for row in known)
    offline_reserve = reserve - sum(max(0, row['active_power_w']) for row in rows if str(row['id']) in fixed)
    estimated = actual + offline_reserve
    remaining = max(0, target_w - reserve)
    tolerance = min(max(1000, target_w * .01), target_w * .05)
    per_unit = max(50, tolerance / max(1, len(controlled)))

    # Equal kW shares; a unit already at 100% cannot contribute hidden sunlight.
    # Restricted units are allowed gradual probes rather than declared weak forever.
    pool = controlled[:]
    assigned = {}
    def capacity(row):
        power = max(0, row['active_power_w'])
        sample = history[str(row['id'])]
        if row.get('effective_percent', row['power_limit_percent']) >= 100:
            return power
        if sample.get('flat_increases', 0) >= 2:
            # Two observed ineffective increases indicate scarce sunlight. Keep
            # gentle probes while giving the remaining kW share to other units.
            return power + max(100, sample['w_per_percent']) * MAX_UP_STEP
        return float('inf')
    while pool:
        equal = remaining / len(pool)
        weak = [r for r in pool if capacity(r) <= equal]
        if not weak:
            assigned.update((str(r['id']), equal) for r in pool)
            break
        for row in weak:
            value = capacity(row)
            assigned[str(row['id'])] = value
            remaining = max(0, remaining - value)
        weak_ids = {r['id'] for r in weak}
        pool = [r for r in pool if r['id'] not in weak_ids]

    desired, increases, reductions = {}, [], []
    for row in controlled:
        ident = str(row['id'])
        cap = min(100, max(1, math.ceil(row.get('effective_percent', row['power_limit_percent']))))
        power = max(0, row['active_power_w'])
        gain = max(100, history.get(ident, {}).get('w_per_percent', 0))
        error = power - assigned[ident]
        local_tolerance = max(per_unit, gain * 1.1)
        desired[ident] = cap
        if error > local_tolerance and cap > 1:
            desired[ident] = max(1, cap - min(MAX_DOWN_STEP, max(1, math.ceil(error / gain * .5))))
            reductions.append(ident)
        elif error < 0 and cap < 100:
            increases.append((ident, cap, gain, assigned[ident] - power))

    # Avoid declaring a small observed excess acceptable merely due to per-unit rounding.
    if estimated > target_w and not reductions:
        candidates = [r for r in controlled if desired[str(r['id'])] > 1 and r['active_power_w'] > 0]
        if candidates:
            row = max(candidates, key=lambda r: r['active_power_w'] - assigned[str(r['id'])])
            ident = str(row['id']); desired[ident] -= 1; reductions.append(ident)

    # Reduce first. Subsequent FC04 and generation measurements provide headroom.
    # Missing devices with NO observed reserve prevent increases, not reductions.
    headroom = max(0, target_w - estimated) if estimated < target_w-tolerance else 0
    if not reductions and not unknown and headroom > 0:
        for ident, cap, gain, deficit in sorted(increases, key=lambda item: (-item[3], int(item[0]))):
            steps = min(MAX_UP_STEP, 100-cap, max(1, math.ceil(deficit/gain*.5)), int(headroom/(gain*1.25)))
            if steps > 0:
                desired[ident] = cap + steps
                headroom -= steps * gain * 1.25

    return dict(percent=desired, history=history, assigned_w=assigned, reserved_w=reserve,
        offline_reserved_w=offline_reserve, missing_ids=missing, unknown_ids=unknown, fixed_ids=fixed,
        available_ids=[str(r['id']) for r in controlled], actual_w=actual if known else None,
        estimated_total_w=estimated if not unknown else None, known=len(known), tolerance_w=tolerance,
        within_target=not missing and target_w-tolerance <= actual <= target_w)
