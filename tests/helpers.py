import re
from werkzeug.datastructures import MultiDict


def token(client):
    html = client.get('/login').get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(client, path, data=None, **kwargs):
    data = MultiDict(data or {})
    data['csrf_token'] = token(client)
    return client.post(path, data=data, **kwargs)


def confirmed_post(client, path, data=None, **kwargs):
    """Follow the real two-step UI flow; invalid requests still fail at preview."""
    response = post(client, path, data, **kwargs)
    if response.status_code != 200:
        return response
    if response.is_json:
        confirmation = response.json.get('confirmation_token')
    else:
        match = re.search(r'name="confirmation_token" value="([^"]+)"', response.get_data(as_text=True))
        confirmation = match.group(1) if match else None
    if not confirmation:
        return response
    submitted = MultiDict(data or {})
    submitted['confirmation_token'] = confirmation
    return post(client, path, submitted, **kwargs)
