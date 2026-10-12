import re
from werkzeug.datastructures import MultiDict


def token(client):
    html = client.get('/login').get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(client, path, data=None, **kwargs):
    data = MultiDict(data or {})
    data['csrf_token'] = token(client)
    return client.post(path, data=data, **kwargs)
