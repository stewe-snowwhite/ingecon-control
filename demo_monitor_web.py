"""Administrator-only DEMO controls. Requests never poll a Modbus endpoint."""
from contextlib import contextmanager

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from demo_monitor_store import DIRECTORY, SCENARIOS, EVENT_TYPES, DemoStore
from demo_notifications import subscribe
from models import db, TelegramLink
from security import roles_required

demo_monitor_web = Blueprint('demo_monitor_web',__name__)


@contextmanager
def storage(initialize=False):
    store = None
    try:
        store = DemoStore(current_app.config.get('MONITOR_DEMO_ROOT',DIRECTORY),initialize=initialize)
        yield store
    except (OSError,SQLAlchemyError):
        abort(503,'Ізольована БД TASK-08 недоступна. Перевірте підготовку та права; production не змінено.')
    except ValueError:
        abort(400,'Некоректні дані або захист ізольованої БД TASK-08.')
    finally:
        if store: store.close()


@demo_monitor_web.get('/demo')
@roles_required('Administrator')
def page():
    root = current_app.config.get('MONITOR_DEMO_ROOT',DIRECTORY)
    from pathlib import Path
    data = None
    if (Path(root)/'monitor.sqlite').exists():
        with storage() as store:
            data = store.snapshot(g.user.id,request.args.get('type') or None)
    return render_template('monitor_demo.html',data=data,scenarios=SCENARIOS,event_types=EVENT_TYPES)


@demo_monitor_web.post('/demo/init')
@roles_required('Administrator')
def initialize():
    with storage(initialize=True): pass
    flash('Окрему БД TASK-08 підготовлено. Симулятор і монітор запускаються окремими командами.','info')
    return redirect(url_for('.page'))


@demo_monitor_web.post('/demo/scenario')
@roles_required('Administrator')
def scenario():
    if len(request.form.getlist('scenario'))!=1 or len(request.form.getlist('load'))!=1:
        abort(400,'Вкажіть один сценарій і одне навантаження.')
    with storage() as store:
        store.change(request.form['scenario'],request.form['load'],g.user.username)
    flash('Налаштування симулятора збережено. Виміряний стан оновиться лише після опитування.','info')
    return redirect(url_for('.page'))


@demo_monitor_web.post('/demo/notifications')
@roles_required('Administrator')
def notifications():
    if request.form.getlist('action') not in (['enable'],['disable']):
        abort(400,'Невідома дія зі сповіщеннями.')
    enabled = request.form['action']=='enable'
    if enabled and request.form.getlist('confirm')!=['demo-only']:
        abort(400,'Підтвердьте надсилання особистих [DEMO]-сповіщень.')
    link = db.session.scalar(select(TelegramLink).where(TelegramLink.user_id==g.user.id))
    with storage() as store:
        subscribe(store,g.user,link,enabled)
    flash('[DEMO]-сповіщення для вашої Telegram-прив’язки '+('увімкнено на одну годину.' if enabled else 'вимкнено.'),'info')
    return redirect(url_for('.page'))


@demo_monitor_web.get('/api/demo/status')
@roles_required('Administrator')
def status():
    with storage() as store:
        return jsonify(store.snapshot(g.user.id,request.args.get('type') or None))
