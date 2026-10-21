"""Saved-results bulk creation: synthetic DB, no discovery or device traffic."""
import json
import re
import time
from contextlib import contextmanager

import pytest
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

import discovery_bulk as bulk
from models import db, User, Inverter, MonitorConfig, DiscoveryScan, DiscoveryResult, CommandAudit
from monitoring import target_for
from operation_locks import OperationBusy
from tests.helpers import post

URL='/add/discovery/bulk'
SECRET='shared-bulk-password-sentinel'


def seed(count=2):
    user=db.session.get(User,1)
    scan=DiscoveryScan(user_id=user.id,user_version=user.session_version,subnets=[],device_id=7,
                       total=count,processed=count,found=count,created_at=time.time(),state='completed')
    db.session.add(scan); db.session.flush()
    for i in range(count):
        db.session.add(DiscoveryResult(scan_id=scan.id,ip=f'10.12.0.{i+2}',mac=None,cidr='10.12.0.0/24',
            port=1502,device_id=7,tcp_ok=True,modbus_ok=i%2==0,confidence='possible',reason='Mock only'))
    db.session.commit()
    return [r.id for r in db.session.scalars(db.select(DiscoveryResult).order_by(DiscoveryResult.id))]


def preview(client,keys):
    return post(client,URL,MultiDict([('stage','preview')]+[('candidate_id',str(k)) for k in keys]))


def save_data(client,keys):
    response=preview(client,keys)
    assert response.status_code==200
    token=re.search(r'name="confirmation_token" value="([^"]+)"',response.get_data(as_text=True)).group(1)
    return MultiDict([('stage','save'),('confirmation_token',token),('group','Shared group'),
        ('model','ingecon_sun_100tl'),('login','shared-login'),('password',SECRET),('confirm_unverified','yes')]+
        [('candidate_id',str(k)) for k in keys]+[(f'name_{k}',f'Inverter {k}') for k in keys])


def test_preview_does_not_add_or_echo_secrets(web,modbus):
    _,client=web; keys=seed()
    data=MultiDict([('stage','preview'),('password',SECRET)]+[('candidate_id',str(k)) for k in keys])
    response=post(client,URL,data)
    html=response.get_data(as_text=True)
    assert response.status_code==200 and SECRET not in html
    assert 'Спільний пароль' in html and '10.12.0.2' in html and '10.12.0.3' in html
    assert Inverter.query.count()==CommandAudit.query.count()==0 and modbus[1]==[]


def test_adds_common_model_credentials_transport_and_idempotent_replay(web,modbus):
    _,client=web; keys=seed(); data=save_data(client,keys)
    response=post(client,URL,data)
    assert response.status_code==200
    assert 'Додано: 2' in response.get_data(as_text=True) and SECRET not in response.get_data(as_text=True)
    assert Inverter.query.count()==2
    for inv in Inverter.query.all():
        assert (inv.group,inv.model,inv.login,inv.password)==('Shared group','ingecon_sun_100tl','shared-login',SECRET)
        target=target_for(inv,db.session.get(MonitorConfig,inv.id))
        assert (target.port,target.device_id,target.profile)==(1502,7,'ingecon_100tl_input')
    details=json.dumps(CommandAudit.query.one().details)
    assert SECRET not in details and 'shared-login' not in details
    assert len(CommandAudit.query.one().details['results'])==2
    again=post(client,URL,data)
    assert again.status_code==200 and 'Пропущено дублікатів: 2' in again.get_data(as_text=True)
    assert Inverter.query.count()==2 and modbus[1]==[]


def test_duplicate_created_after_preview_is_skipped_without_overwrite(web):
    _,client=web; keys=seed(); data=save_data(client,keys)
    old=Inverter(name='Do not change',group='Old',model='unknown',ip_address='10.12.0.2',login='old',password='keep-secret')
    db.session.add(old); db.session.flush()
    db.session.add(MonitorConfig(inverter_id=old.id,port=1502,device_id=7)); db.session.commit()
    response=post(client,URL,data)
    assert response.status_code==200 and 'Додано: 1' in response.get_data(as_text=True)
    assert 'keep-secret' not in response.get_data(as_text=True)
    db.session.refresh(old)
    assert (old.name,old.group,old.model,old.login,old.password)==('Do not change','Old','unknown','old','keep-secret')
    assert Inverter.query.count()==2


@pytest.mark.parametrize('role',[None,'Operator','Viewer'])
@pytest.mark.parametrize('stage',['preview','save'])
def test_roles_block_entire_bulk_workflow(web,role,stage):
    _,client=web; keys=seed()
    if role is None:
        with client.session_transaction() as session: session.clear()
    else:
        db.session.get(User,1).role=role; db.session.commit()
    response=post(client,URL,dict(stage=stage,candidate_id=str(keys[0])))
    assert response.status_code==(401 if role is None else 403) and Inverter.query.count()==0


def test_csrf_required_for_both_steps(web):
    _,client=web; keys=seed()
    for stage in ['preview','save']:
        assert client.post(URL,data=dict(stage=stage,candidate_id=str(keys[0]))).status_code==400


@pytest.mark.parametrize('keys',[[],['1','1'],['1','01'],['-1'],['１２'],['999'],['1']*101])
def test_bad_selection_rejected(web,keys):
    _,client=web; seed()
    assert preview(client,keys).status_code==400 and Inverter.query.count()==0


@pytest.mark.parametrize('field,value',[('group',''),('group','x'*101),('model','untrusted'),
    ('login','x'*101),('password','x'*101),('name_1',''),('confirm_unverified','no'),('confirmation_token','broken')])
def test_final_validation_does_not_leak_password_or_partially_save(web,field,value):
    _,client=web; keys=seed(); data=save_data(client,keys); data[field]=value
    response=post(client,URL,data)
    assert response.status_code==400 and SECRET not in response.get_data(as_text=True)
    assert Inverter.query.count()==0


@pytest.mark.parametrize('field',['group','model','login','password','name_1','stage','confirmation_token'])
def test_ambiguous_fields_rejected(web,field):
    _,client=web; keys=seed(); data=save_data(client,keys); data.add(field,data[field])
    assert post(client,URL,data).status_code==400 and Inverter.query.count()==0


def test_endpoint_mutation_invalidates_confirmation(web):
    _,client=web; keys=seed(); data=save_data(client,keys)
    db.session.get(DiscoveryResult,keys[0]).device_id=9; db.session.commit()
    assert post(client,URL,data).status_code==400 and Inverter.query.count()==0


def test_mixed_scans_and_changed_selection_rejected(web):
    _,client=web; keys=seed(); data=save_data(client,keys)
    scan=DiscoveryScan(user_id=1,user_version=db.session.get(User,1).session_version,subnets=[],device_id=1,total=0,created_at=time.time())
    db.session.add(scan); db.session.flush()
    db.session.get(DiscoveryResult,keys[1]).scan_id=scan.id; db.session.commit()
    assert post(client,URL,data).status_code==400
    data.setlist('candidate_id',[str(keys[0])])
    assert post(client,URL,data).status_code==400 and Inverter.query.count()==0


def test_commit_failure_rolls_back_whole_batch_without_secret_output(web,monkeypatch):
    _,client=web; keys=seed(); data=save_data(client,keys)
    def fail(*a,**kw): raise SQLAlchemyError('synthetic failure '+SECRET)
    monkeypatch.setattr(bulk,'audit',fail)
    response=post(client,URL,data)
    assert response.status_code==503 and SECRET not in response.get_data(as_text=True)
    assert Inverter.query.count()==MonitorConfig.query.count()==0


def test_inventory_lock_conflict_reports_retry(web,monkeypatch):
    _,client=web; keys=seed(); data=save_data(client,keys)
    @contextmanager
    def busy(*args):
        raise OperationBusy()
        yield
    monkeypatch.setattr(bulk,'resource_lock',busy)
    assert post(client,URL,data).status_code==409 and Inverter.query.count()==0


def test_bulk_controls_are_on_existing_add_page(web):
    _,client=web; seed()
    html=client.get('/add?scan=1').get_data(as_text=True)
    assert 'id="discovery-bulk-form"' in html and 'Додати вибрані' in html
    assert 'Автоматичний пошук' in html and 'Додати вручну' in html


def test_maximum_batch_of_100_is_saved_without_device_io(web,modbus):
    _,client=web; keys=seed(100)
    response=post(client,URL,save_data(client,keys))
    assert response.status_code==200 and 'Додано: 100' in response.get_data(as_text=True)
    assert Inverter.query.count()==MonitorConfig.query.count()==100 and modbus[1]==[]


def test_expired_confirmation_is_rejected_without_saving(web,monkeypatch):
    from itsdangerous.timed import TimestampSigner
    _,client=web; keys=seed(); data=save_data(client,keys)
    now=int(time.time())
    monkeypatch.setattr(TimestampSigner,'get_timestamp',lambda self:now+1000)
    response=post(client,URL,data)
    assert response.status_code==400 and 'застаріло' in response.get_data(as_text=True)
    assert Inverter.query.count()==0


@pytest.mark.parametrize('field,value',[('ip','8.8.8.8'),('port',0),('device_id',248)])
def test_corrupt_saved_endpoints_are_not_added(web,field,value):
    _,client=web; keys=seed()
    setattr(db.session.get(DiscoveryResult,keys[0]),field,value); db.session.commit()
    assert preview(client,keys).status_code==400 and Inverter.query.count()==0


def test_confirmation_cannot_be_reused_by_another_administrator(web):
    _,client=web; keys=seed(); data=save_data(client,keys)
    other=User(username='other-admin',role='Administrator')
    other.set_password('synthetic-other-password'); db.session.add(other); db.session.commit()
    with client.session_transaction() as session:
        session['user_id']=other.id; session['session_version']=other.session_version
    assert post(client,URL,data).status_code==400 and Inverter.query.count()==0
