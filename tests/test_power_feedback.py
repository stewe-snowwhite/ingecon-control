"""Model unknown/different rated powers exclusively inside a synthetic plant."""
import pytest

from power_feedback import allocation, MAX_DOWN_STEP, MAX_UP_STEP


def measurement(ident, power, cap, cycle=0, online=True):
    return dict(id=ident, active_power_w=power if online else None, power_limit_percent=cap,
        checked_at=cycle*30, stale=not online, state='Online' if online else 'Offline',
        checks={'modbus': {'ok':online}})


def plant(nominals, sunlight, target, cycles=60, outages=None):
    # Nominals are hidden from the controller; only measurements/caps are supplied.
    caps={i:100 for i in range(1,len(nominals)+1)}
    history={}; results=[]
    for cycle in range(cycles):
        rows=[measurement(i,min(nom*caps[i]/100,sunlight(cycle,i) if callable(sunlight) else sunlight[i-1]),caps[i],cycle,
            not outages or not outages(cycle,i)) for i,nom in enumerate(nominals,1)]
        plan=allocation(target,rows,history)
        history=plan['history']
        for ident,desired in plan['percent'].items():
            change=desired-caps[int(ident)]
            assert -MAX_DOWN_STEP <= change <= MAX_UP_STEP
            assert type(desired) is int and 1<=desired<=100
        caps.update({int(i):p for i,p in plan['percent'].items()})
        results.append(plan)
    return results


def test_26_mixed_unknown_ratings_converge_without_a_nominal_parameter():
    results=plant([80000,110000,150000]*8+[100000,120000],[1_000_000]*26,900000)
    assert all(result['within_target'] for result in results[-10:])
    assert all(891000<=result['actual_w']<=900000 for result in results[-10:])
    assert len(set(results[-1]['percent'].values()))>1


def test_low_sun_at_maximum_releases_share_and_does_not_cause_oscillation():
    results=plant([100000]*20,[5000]+[100000]*19,400000)
    assert all(result['within_target'] for result in results[-10:])
    assert results[-1]['percent']['1']==100
    assert results[-1]['assigned_w']['2']>20000
    assert len({tuple(result['percent'].items()) for result in results[-10:]})==1


def test_clouded_restricted_unit_releases_share_before_reaching_100_percent():
    results=plant([100000]*26,lambda cycle,i:5000 if i==1 and 15<=cycle<45 else 100000,
                  900000,cycles=70)
    assert all(result['within_target'] for result in results[30:45])
    assert results[30]['percent']['1']<100
    assert results[30]['assigned_w']['1']<15000
    assert results[30]['assigned_w']['2']>900000/26
    assert all(result['within_target'] for result in results[-10:])


def test_offline_unit_reserves_observed_generation_with_margin_and_rest_can_increase():
    before=allocation(200000,[measurement(1,30000,30),measurement(2,40000,40),measurement(3,40000,40)])
    rows=[measurement(1,None,30,1,False),measurement(2,40000,40,1),measurement(3,40000,40,1)]
    plan=allocation(200000,rows,before['history'])
    assert plan['offline_reserved_w']==36000
    assert plan['percent']['2']>40 and plan['percent']['3']>40
    assert '1' not in plan['percent'] and plan['missing_ids']==['1'] and not plan['within_target']
    assert plan['estimated_total_w']==116000 and plan['actual_w']==80000
    # Reserve is not silently released on timeout, restart or a new day.
    for row in rows:row['checked_at']+=86400
    later=allocation(200000,rows,plan['history'])
    assert later['offline_reserved_w']==36000


def test_known_outage_and_recovery_rejoin_distribution_without_start_stop():
    results=plant([80000,110000,150000]*8+[100000,120000],[1_000_000]*26,900000,
        cycles=70,outages=lambda cycle,ident:15<=cycle<40 and ident in (1,2))
    for plan in results[15:40]:
        assert '1' not in plan['percent'] and '2' not in plan['percent']
        assert plan['offline_reserved_w']>0 and not plan['within_target']
    assert len(results[40]['percent'])==26
    assert all(plan['within_target'] for plan in results[-10:])


def test_first_seen_offline_or_zero_has_no_invented_reserve_or_increases():
    rows=[measurement(1,None,50,online=False),measurement(2,10000,10)]
    result=allocation(100000,rows)
    assert result['unknown_ids']==['1'] and result['estimated_total_w'] is None
    assert result['percent']['2']==10
    history=allocation(100000,[measurement(1,0,100),measurement(2,10000,10)])['history']
    result=allocation(100000,rows,history)
    assert result['unknown_ids']==['1'] and result['percent']['2']==10


def test_no_generation_or_fresh_reads_is_never_successfully_within_target():
    assert not allocation(100000,[measurement(1,0,100)])['within_target']
    assert not allocation(100000,[measurement(1,None,100,online=False)])['within_target']
    assert not allocation(1,[measurement(1,0,100)])['within_target']


def test_stale_duplicate_or_out_of_order_data_cannot_lower_saved_reserve():
    history=allocation(100000,[measurement(1,50000,50,4)])['history']
    for cycle in (0,4):
        plan=allocation(100000,[measurement(1,1000,50,cycle)],history)
        assert plan['history']['1']==history['1']
    lost=allocation(100000,[measurement(1,None,50,5,False)],history)
    assert lost['offline_reserved_w']==60000


def test_fresh_fixed_fault_is_measured_but_not_commanded():
    plan=allocation(100000,[measurement(1,40000,40),measurement(2,20000,20)],blocked=['1'])
    assert '1' not in plan['percent'] and plan['reserved_w']==40000
    assert plan['actual_w']==plan['estimated_total_w']==60000
    assert plan['percent']['2']>20


def test_minimum_percent_does_not_falsely_claim_target_reached():
    plan=allocation(1,[measurement(1,100000,1)])
    assert plan['percent']=={'1':1} and not plan['within_target']


@pytest.mark.parametrize('value',[0,-1,True,1.5,float('nan'),100_000_001])
def test_invalid_targets_are_rejected(value):
    with pytest.raises(ValueError):allocation(value,[measurement(1,10000,10)])
