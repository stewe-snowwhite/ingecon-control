"""Administrator polling controls; the monitor alone contacts inverters."""
import logging
import re
import time

from flask import Blueprint, abort, flash, g, jsonify, redirect, request, url_for

import monitor_schedule
from operation_locks import OperationBusy
from security import roles_required

web = Blueprint('monitor_web', __name__, url_prefix='/monitor')
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.propagate = False  # Keep administrative events even with Gunicorn's warning level.


@web.post('/polling')
@roles_required('Administrator')
def polling():
    if request.form.getlist('intent') not in (['temporary'], ['default']):
        abort(400, 'Некоректна дія налаштування опитування.')
    interval = None
    if request.form['intent'] == 'temporary':
        values = request.form.getlist('interval_seconds')
        if len(values) != 1 or not re.fullmatch(r'[1-9][0-9]{0,4}', values[0], re.ASCII):
            abort(400, 'Вкажіть цілий інтервал від 1 до 86400 секунд.')
        interval = int(values[0])
    try:
        policy = monitor_schedule.configure(interval)
    except ValueError as error:
        abort(400, str(error))
    except (OSError, OperationBusy):
        abort(503, 'Не вдалося зберегти інтервал. Налаштування не підтверджене.')
    log.info('monitor_polling_changed user_id=%s interval=%s expires_at=%s',
             g.user.id, policy['interval_seconds'], policy['expires_at'])
    message = ('Інтервал збережено на 30 хвилин. Монітор застосує його після поточної операції.'
               if policy['temporary'] else 'Відновлено інтервал 30 секунд. Монітор застосує його після поточної операції.')
    if request.accept_mimetypes.best == 'application/json':
        return jsonify(polling=policy, generated_at=time.time(), message=message)
    flash(message, 'info')
    return redirect(url_for('index'))
