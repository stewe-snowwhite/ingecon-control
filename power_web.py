"""Authenticated, CSRF-protected configuration; never executes inverter commands."""
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from models import db, ROLES
from operation_locks import OperationBusy
from power_budget import BudgetService, kw, status, watts
from security import roles_required

web=Blueprint('power_web',__name__,url_prefix='/power-budget')


def field(name):
    values=request.form.getlist(name)
    if len(values)!=1:abort(400,'Потрібне одне значення параметра.')
    return values[0]


@web.get('')
@roles_required(*ROLES)
def page():
    try:return render_template('power_budget.html',budget=status(),preview=None,kw=kw)
    except (ValueError,OSError):abort(503,'Конфігурація автоматизації недоступна. Команди заблоковані.')


@web.get('/status')
@roles_required(*ROLES)
def current():
    try:return jsonify(status())
    except (ValueError,OSError):abort(503,'Конфігурація автоматизації недоступна.')


@web.post('/preview')
@roles_required('Administrator')
def preview():
    action=field('action')
    if action not in ('enable','disable'):abort(400,'Невідома дія автоматизації.')
    try:
        if action=='enable' and field('acknowledge_nominal')!='yes':raise ValueError('Підтвердьте номінальну потужність кожного інвертора.')
        service=BudgetService(db.engine)
        intent,payload=service.preview(g.user.id,watts(field('target_kw')) if action=='enable' else None,
                                      watts(field('rated_kw')) if action=='enable' else 100000)
        return render_template('power_budget.html',budget=status(),preview=payload,intent=intent,kw=kw)
    except ValueError as error:abort(400,str(error))
    except (OSError,OperationBusy):abort(503,'Контролер зайнятий. Налаштування не підтверджено.')


@web.post('/confirm')
@roles_required('Administrator')
def confirm():
    decision=field('decision')
    if decision not in ('confirm','cancel'):abort(400,'Невідома дія підтвердження.')
    try:result=BudgetService(db.engine).confirm(g.user.id,field('intent'),cancel=decision=='cancel')
    except ValueError as error:abort(400,str(error))
    except (OSError,OperationBusy):abort(503,'Збереження не підтверджено. Перевірте поточний стан перед повторенням.')
    messages={'configured':'Налаштування збережено. Фактичне виконання відстежується окремим контролером.',
              'cancelled':'Зміни скасовано.','invalidated':'Підтвердження застаріло або дані змінилися. Підготуйте його заново.'}
    flash(messages[result],'info')
    return redirect(url_for('power_web.page'))
