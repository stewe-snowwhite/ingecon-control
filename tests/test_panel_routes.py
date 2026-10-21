"""Rendered command form and all four route/role/CSRF boundaries, mock I/O only."""
import pytest
from html.parser import HTMLParser

from models import User,db
from tests.helpers import post
from tests.test_inverter_controls import authorized,seed


class Forms(HTMLParser):
    def __init__(self,html):
        super().__init__()
        self.forms=[]
        self.current=None
        self.feed(html)

    def handle_starttag(self,tag,attributes):
        attrs=dict(attributes)
        if tag=='form':
            self.current=dict(attributes=attrs,controls=[])
            self.forms.append(self.current)
        elif self.current is not None and tag in ('input','button'):
            self.current['controls'].append(attrs)

    def handle_endtag(self,tag):
        if tag=='form': self.current=None


@pytest.mark.parametrize('action',['on','off','limit','limit_max'])
def test_dashboard_form_names_actions_without_changing_backend_route(web,authorized,action):
    module,client=web
    seed()
    module.app.config['MODBUS_WRITES_ENABLED']=True
    form=next(form for form in Forms(client.get('/').text).forms if form['attributes'].get('id')=='control-form')
    assert form['attributes']['action']=='/control'
    assert form['attributes']['method']=='POST'
    assert 'data-command' in form['attributes']
    assert any(row.get('name')=='csrf_token' and row.get('value') for row in form['controls'])
    assert any(row.get('name')=='action' and row.get('value')==action and 'disabled' not in row for row in form['controls'])
    assert authorized==[]


@pytest.mark.parametrize('action',['on','off','limit','limit_max'])
@pytest.mark.parametrize('role',[None,'Viewer','Operator','Administrator'])
def test_each_control_requires_csrf_and_authorized_preview(web,authorized,action,role):
    module,client=web
    seed()
    module.app.config['MODBUS_WRITES_ENABLED']=True
    if role is None: client=module.app.test_client()
    else:
        db.session.get(User,1).role=role
        db.session.commit()
    data=dict(action=action,selected_ids='1',limit_value='37')
    headers={'Accept':'application/json'}
    assert client.post('/control',data=data,headers=headers).status_code==400
    response=post(client,'/control',data,headers=headers)
    assert response.status_code==(401 if role is None else 403 if role=='Viewer' else 200)
    if response.status_code==200: assert response.json['confirmation_required']
    assert authorized==[]
