"""Authenticated CSRF-protected pairing. Never auto-create a schema in a request."""
from flask import Blueprint, abort, g, redirect, render_template, request, url_for
from sqlalchemy import inspect

from models import db, TELEGRAM_TABLES
from security import roles_required
from telegram_store import TelegramAccess, TelegramStore

telegram_web = Blueprint('telegram_web',__name__, url_prefix='/settings/telegram')


@telegram_web.route('',methods=['GET','POST'])
@roles_required('Administrator','Operator','Viewer')
def settings():
    if not {table.name for table in TELEGRAM_TABLES}<=set(inspect(db.engine).get_table_names()):
        return render_template('error.html',code=503,
            message='Telegram ще не підготовлено. Потрібна явна підготовка схеми з резервною копією за TELEGRAM.md.'),503
    store = TelegramStore(db.engine)
    code = None
    if request.method=='POST':
        actions = request.form.getlist('action')
        if actions==['code']:
            try:
                code = store.issue_code(g.user.id)
            except TelegramAccess as error:
                abort(400,str(error))
        elif actions==['unlink']:
            store.unlink(g.user.id)
            return redirect(url_for('telegram_web.settings'))
        else:
            abort(400,'Некоректна дія Telegram.')
    return render_template('telegram.html',link=store.web_link(g.user.id),code=code)
