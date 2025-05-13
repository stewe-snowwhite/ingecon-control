from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from modbus_utils import check_modbus_connection  # ми створимо цей файл далі
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
    inverters = Inverter.query.all()
    return render_template("index.html", inverters=inverters)

# Додавання інвертора
@app.route('/add', methods=['GET', 'POST'])
def add_inverter():
    if request.method == 'POST':
        name = request.form['name']
        group = request.form['group']
        ip = request.form['ip']
        login = request.form['login']
        password = request.form['password']

        if check_modbus_connection(ip):
            new_inv = Inverter(name=name, group=group, ip_address=ip, login=login, password=password)
            db.session.add(new_inv)
            db.session.commit()
            flash('Інвертор успішно додано!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Не вдалося підключитися до інвертора по Modbus TCP.', 'danger')

    return render_template("add_inverter.html")

if __name__ == '__main__':
    if not os.path.exists('database.db'):
        db.create_all()
    app.run(debug=True, host='0.0.0.0', port=5000)
