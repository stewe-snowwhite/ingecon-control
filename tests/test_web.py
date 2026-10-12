import pytest
from tests.helpers import post, confirmed_post

def add(client,name='Mock A',group='A',ip='192.0.2.1'):
    return post(client, '/add',data=dict(name=name,group=group,ip=ip,login='',password=''))

def test_crud_groups_and_dashboard(web):
    module, client=web
    assert add(client).status_code==302
    add(client,'Mock B','B','192.0.2.2')
    inv=module.Inverter.query.filter_by(name='Mock A').one()
    body=client.get('/?group=A').get_data(as_text=True)
    assert 'Mock A' in body and 'Mock B' not in body
    assert 'Unknown' in body
    assert client.get(f'/edit/{inv.id}').status_code==200
    post(client, f'/edit/{inv.id}',data=dict(name='Changed',group='C',ip='192.0.2.3',login='',password=''))
    assert module.db.session.get(module.Inverter,inv.id).name=='Changed'
    assert confirmed_post(client, f'/delete/{inv.id}').status_code==200
    assert module.Inverter.query.count()==1

@pytest.mark.parametrize('action,value,expected', [('on',None,('write',1000,6,1)),
    ('off',None,('write',1000,5,1)),('limit','25',('write',46,25,1)),
    ('limit_max',None,('write',46,100,1))])
def test_selected_control(web,modbus,action,value,expected):
    module,client=web; _,clients=modbus
    module.app.config['MODBUS_WRITES_ENABLED'] = True
    add(client); add(client,'Mock B','B','192.0.2.2')
    inv=module.Inverter.query.filter_by(name='Mock A').one()
    data={'selected_ids':str(inv.id),'action':action}
    if value is not None: data['limit_value']=value
    assert confirmed_post(client, '/control',data=data).status_code==200
    assert expected in clients[0].calls
    assert all(c.host=='192.0.2.1' for c in clients)

def test_missing_selection_and_action(web,modbus):
    module,client=web; _,clients=modbus
    add(client); inv=module.Inverter.query.first()
    post(client, '/control',data={'action':'on'})
    post(client, '/control',data={'selected_ids':str(inv.id)})
    assert clients==[]

def test_test_and_bulk_delete(web,modbus):
    module,client=web; _,clients=modbus
    add(client); add(client,'Mock B')
    ids=[str(inv.id) for inv in module.Inverter.query.all()]
    post(client, '/control',data={'selected_ids':ids,'action':'test'})
    assert clients == []
    assert all(c.requested_at is not None for c in module.MonitorConfig.query.all())
    confirmed_post(client, '/control',data={'selected_ids':ids,'action':'delete'})
    assert module.Inverter.query.count()==0

def test_invalid_limit_must_not_write(web,modbus):
    module,client=web; _,clients=modbus
    add(client); inv=module.Inverter.query.first()
    post(client, '/control',data={'selected_ids':str(inv.id),'action':'limit','limit_value':'bad'})
    assert not any(call[0]=='write' for c in clients for call in c.calls)

def test_anonymous_control_must_be_denied(web):
    module,_=web
    client=module.app.test_client()
    assert post(client, '/control',data={'action':'on'}).status_code in (401,403)

@pytest.mark.parametrize('path', ['/add', '/edit/999'])
def test_form_and_missing_device_routes(web, path):
    _, client = web
    assert client.get(path).status_code == (200 if path == '/add' else 404)

def test_single_device_connection_test(web, modbus):
    module, client = web
    _, clients = modbus
    add(client)
    inv = module.Inverter.query.first()
    assert post(client, f'/test/{inv.id}').status_code == 302
    assert clients == []
    assert module.db.session.get(module.MonitorConfig, inv.id).requested_at is not None
