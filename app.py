from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from modbus_utils import check_modbus_connection
import os

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
db = SQLAlchemy(app)

# Модель інвертора
class Inverter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    group = db.Column(db.String(100), nullable=False)
    ip_address = db.Column(db.String(100), nullable=False)
    login = db.Column(db.String(100), nullable=True)
    password = db.Column(db.String(100), nullable=True)

# Головна сторінка
@app.route('/')
def index():
    selected_group = request.args.get('group')

    groups = db.session.query(Inverter.group).distinct().all()
    groups = [g[0] for g in groups]

    if selected_group:
        inverters = Inverter.query.filter_by(group=selected_group).all()
    else:
        inverters = Inverter.query.all()

    return render_template(
        "index.html",
        inverters=inverters,
        groups=groups,
        selected_group=selected_group
    )

# Додавання інвертора
@app.route('/add', methods=['GET', 'POST'])
def add_inverter():
    if request.method == 'POST':
        name = request.form['name']
        group = request.form['group']
        ip = request.form['ip']
        login = request.form['login']
        password = request.form['password']

        new_inv = Inverter(name=name, group=group, ip_address=ip, login=login, password=password)
        db.session.add(new_inv)
        db.session.commit()
        flash('Інвертор додано без перевірки підключення.', 'info')
        return redirect(url_for('index'))

    groups = db.session.query(Inverter.group).distinct().all()
    groups = [g[0] for g in groups]
    return render_template("add_inverter.html", groups=groups)

# Тест підключення до інвертора
@app.route('/test/<int:inverter_id>')
def test_connection(inverter_id):
    inverter = Inverter.query.get_or_404(inverter_id)
    result = check_modbus_connection(inverter.ip_address)
    if result:
        flash(f'Інвертор {inverter.name} ({inverter.ip_address}) доступний по Modbus TCP.', 'success')
    else:
        flash(f'Інвертор {inverter.name} ({inverter.ip_address}) недоступний по Modbus TCP.', 'danger')
    return redirect(url_for('index'))

# Обробка кнопок керування
@app.route('/control', methods=['POST'])
def control_inverters():
    selected_ids = request.form.getlist('selected_ids')
    action = request.form.get('action')

    if not selected_ids:
        flash("Не вибрано жодного інвертора.", "warning")
        return redirect(url_for('index'))

    if not action:
        flash("Не вказано дію. Можливо, форма була відправлена некоректно.", "danger")
        return redirect(url_for('index'))

    inverters = Inverter.query.filter(Inverter.id.in_(selected_ids)).all()

    if action == "test":
        for inv in inverters:
            result = check_modbus_connection(inv.ip_address)
            if result:
                flash(f'✅ {inv.name} ({inv.ip_address}) — доступний', 'success')
            else:
                flash(f'❌ {inv.name} ({inv.ip_address}) — недоступний', 'danger')
        return redirect(url_for('index'))

    if action == "delete":
        count = 0
        for inv in inverters:
            db.session.delete(inv)
            count += 1
        db.session.commit()
        flash(f"🗑️ Видалено {count} інвертор(ів).", "info")
        return redirect(url_for('index'))

    for inv in inverters:
        print(f"👉 {action.upper()} → {inv.name} ({inv.ip_address})")

        if action == "on":
            modbus_turn_on(inv.ip_address)
        elif action == "off":
            modbus_turn_off(inv.ip_address)
        elif action == "limit":
            modbus_limit_power(inv.ip_address, value=50)

    flash(f"Команда \"{action}\" виконана для {len(inverters)} інверторів.", "success")
    return redirect(url_for('index'))

# Редагування інвертора
@app.route('/edit/<int:inverter_id>', methods=['GET', 'POST'])
def edit_inverter(inverter_id):
    inverter = Inverter.query.get_or_404(inverter_id)

    if request.method == 'POST':
        inverter.name = request.form['name']
        inverter.group = request.form['group']
        inverter.ip_address = request.form['ip']
        inverter.login = request.form['login']
        inverter.password = request.form['password']
        db.session.commit()
        flash('Інвертор оновлено успішно.', 'success')
        return redirect(url_for('index'))

    groups = db.session.query(Inverter.group).distinct().all()
    groups = [g[0] for g in groups]
    return render_template('edit_inverter.html', inverter=inverter, groups=groups)


# Видалення інвертора окремо
@app.route('/delete/<int:inverter_id>', methods=['POST'])
def delete_inverter(inverter_id):
    inverter = Inverter.query.get_or_404(inverter_id)
    db.session.delete(inverter)
    db.session.commit()
    flash(f'Інвертор "{inverter.name}" видалено.', 'info')
    return redirect(url_for('index'))

# Заготовки для керування через Modbus TCP
def modbus_turn_on(ip):
    print(f"[Modbus] Увімкнути інвертор {ip}")

def modbus_turn_off(ip):
    print(f"[Modbus] Вимкнути інвертор {ip}")

def modbus_limit_power(ip, value):
    print(f"[Modbus] Обмежити потужність інвертора {ip} до {value}%")

if __name__ == '__main__':
    if not os.path.exists('database.db'):
        with app.app_context():
            db.create_all()
    app.run(debug=True, host='0.0.0.0', port=5000)
