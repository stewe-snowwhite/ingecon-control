# Очищення демонстраційних режимів

Пізніше додано окремий явний дозвіл Start/Stop через Telegram:
[TELEGRAM_CONTROL.md](TELEGRAM_CONTROL.md). Він не вмикається очищенням,
не стосується вебзаписів і не відновлює демонстраційні режими.

## Що підготовлено

З робочого коду вилучено TASK-08 та старий TASK-05: HTTP-сторінки й API,
симулятор TCP, віртуальний інвертор, сценарії, монітор, окрему чергу Telegram,
журнал, CLI-команди, JS/HTML, прапорці режиму та інструкції їх запуску.
`/demo`, `/api/demo/status` й колишні POST-маршрути після завантаження нового
коду повертають 404. Посилань на них у новій навігації немає.

Загальні `FakeModbusTcpClient`, `FakeTelegramSession`, синтетичні SQLite та
мережеве блокування тестів збережено. `device_safety.py` продовжує блокувати
пристроєві транспорти вебу й бота; моніторинг залишається окремим процесом.
Незнімний offline audit guard потрібен лише тестам і перенесений до `tests/`.
Старі функціональні тести вилучених режимів замінено регресіями відсутності
маршрутів, безпеки транспортів і безпечного очищення unit-конфігурації.

Захисна перевірка історичного маркера `demo_metadata` залишена в production
entry points: архівний тестовий файл не повинен випадково стати робочою БД
або джерелом реального опитування. Це тільки відмова, жодного режиму не вмикає.
Фікстури перевірки збереження сторонніх runtime-файлів також залишені.

Усі прямі Python-залежності потрібні production: Flask, Flask-SQLAlchemy,
PyModbus, Flask-WTF, aiogram, Gunicorn. Спеціальних пакетів симулятора не було;
requirements/lock-файли не потребують встановлення або видалення пакетів.

Production SQLite, stewe, Telegram-прив’язки, токен, постійний секрет,
реальна історія та резервні копії цим очищенням не редагуються.
Історія Git не переписується. Modbus writes залишаються заблокованими;
реальні ICMP/TCP/Modbus-перевірки інверторів не виконувалися. Тестування на
фізичних інверторах потребуватиме окремого налаштування й дозволу пізніше.

## Що зараз залишається на сервері

Підготовка коду **не перезапускала** production-служби і не змінювала `/etc`.
Nginx, firewall, SSH і Tailscale залишені без змін.
До погодженого перезапуску працюючі Python-процеси можуть зберігати старі
маршрути, шаблони та notifier у пам’яті. Очищений Git checkout сам їх не вимикає.

Виявлено активні вебпанель і Telegram та override:

```text
/etc/systemd/system/ingecon-web.service.d/10-demo-write.conf
```

Його поточний вміст:

```ini
[Service]
ReadWritePaths=/opt/ingecon-control/instance/monitor-demo
```

Окремих демонстраційних service/timer/socket units і процесів на момент
інвентаризації не знайдено. Невідомі служби за маскою не видаляються.
Після інвентаризації сторонні процеси можуть з’явитися; повторна перевірка
перед застосуванням обов’язкова. Не використовуйте `pkill -f` або wildcard stop.

## Runtime-файли: точний перелік, видалення не погоджене

На момент підготовки знайдено рівно ці кандидати:

```text
/opt/ingecon-control/instance/demo/secret.key
/opt/ingecon-control/instance/demo/demo.log
/opt/ingecon-control/instance/demo/demo.sqlite
/opt/ingecon-control/instance/demo/demo.lock
/opt/ingecon-control/instance/monitor-demo/monitor.sqlite
```

**Усі вони залишені на диску.** Вміст ключа не показувався; це ключ колишнього
тестового режиму, а не production `web-secret.key`. Команд видалення цих файлів
не виконано і в скрипті очищення їх немає. Для наступного видалення потрібно
окремо підтвердити саме цей перелік. До видалення знову перевірте перелік,
процеси та можливі нові SQLite WAL/SHM/journal-файли; нові файли не входять у
це підтвердження автоматично.

`instance/production/`, `instance/telegram/`, `instance/database.db`, усі
production-backups та `instance/locks/` не є кандидатами на видалення.
Спільні lock-файли з хешованими назвами не видаляються: частину використовують
production-процеси, а видалення зайнятого inode порушує взаємне блокування.

## Перегляд без root і без системних змін

У корені проєкту:

```bash
.venv/bin/python -m deploy.remove_demo_override
```

Це лише preview: він читає файли, перевіряє наявні основні units та вміст
конкретного override. Виводяться тільки заплановані конфігураційні шляхи й
дозволені production ReadWritePaths. Служби не зупиняються, БД не відкривається.
При додаткових налаштуваннях у `10-demo-write.conf` або symlink скрипт відмовляє,
щоб випадково не видалити важливий production override.

## Застосування конфігурації від root без перезапусків

Потрібен root shell; пароль у чат або аргументи не передається.
Спочатку перегляньте зміни й повторно перевірте unit-файли:

```bash
cd /opt/ingecon-control
systemd-analyze verify deploy/systemd/ingecon-web.service deploy/systemd/ingecon-telegram.service deploy/systemd/ingecon-http-guard.service
python3 -m deploy.remove_demo_override --apply
systemctl daemon-reload
systemctl show ingecon-web.service ingecon-telegram.service --property=ReadWritePaths --property=DropInPaths
```

Перші дві змінювані конфігурації — наявні web/bot units із `deploy/systemd`.
Вони зберігають User, ExecStart, секретні файли, SQLite та production writable
шляхи; вилучено доступ до непотрібних тестових каталогів. Скрипт перед записом
зберігає обидва чинні unit-файли та точний override у приватний каталог:

```text
/root/ingecon-unit-cleanup-XXXXXXXX/
```

Потім він атомарно встановлює unit-файли й видаляє **тільки перевірений**
`10-demo-write.conf`. Інші drop-in не змінюються. Якщо override вже відсутній,
повторний запуск залишається безпечним. Перевірте, що невідомі інші drop-in
не додають назад зайвих writable-шляхів; автоматично їх не видаляють.

Очікувані ReadWritePaths для обох служб:

```text
/opt/ingecon-control/instance/production
/opt/ingecon-control/instance/locks
```

`daemon-reload` не перезапускає процеси. Зміни mount sandbox й вилучення
Python-коду з пам’яті набудуть сили **після погодженого restart**. До цього
старий процес ще може мати колишній доступ на запис, навіть коли нова
systemd-конфігурація вже показує очищені шляхи.

Під час підготовки root недоступний через `sudo -n`; наведені команди не
виконані. Поточний override поки залишається до виконання команд власником.

## Перезапуск — тільки після окремого підтвердження

Цей крок не виконувався. Після дозволу на короткий перезапуск наявних служб:

```bash
systemctl restart ingecon-web.service ingecon-telegram.service
systemctl is-active ingecon-web.service ingecon-telegram.service nginx.service tailscaled.service ssh.service
systemctl is-enabled ingecon-web.service ingecon-telegram.service
```

Новий ручний Telegram polling не запускайте. Systemd зупиняє існуючий процес
перед його заміною; блокування за bot ID/SQLite залишається. Не перезапускайте
Nginx, Tailscale, SSH, nftables чи HTTP guard: їх конфігурація не змінювалася.

Після перезапуску, у тому самому корені проєкту:

```bash
runuser -u codex -- /opt/ingecon-control/.venv/bin/python -m deploy.check_portal
curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:5000/demo
curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:5000/api/demo/status
```

Очікуються `/login` → 200, анонімний production API → 401, POST без CSRF → 400,
обидва вилучені URL → 404. Авторизований Administrator бачить звичайну панель
без вилученого пункту. Власник перевіряє в браузері власний наявний пароль;
stewe не перевстановлюється. Telegram /status, /inverters, /history та штатні
сповіщення з production SQLite залишаються. Тестові повідомлення більше не
мають джерела в новому notification loop.

Не відновлюйте SQLite зі старого знімка й не очищуйте Telegram-прив’язки:
працюючі служби могли законно додати нові події та оновлення. При проблемі
конфігурації відновлюйте лише відповідні unit/drop-in із root-backup,
перевіряйте їх і погоджуйте потрібний restart. Runtime-файли та всі попередні
резервні копії залишаються недоторканими.

## Автоматичні перевірки

```bash
.venv/bin/python -m pytest -q
node tests/js/dashboard.test.cjs
node tests/js/panel.test.cjs
.venv/bin/python -m tests.device_guard_probe
.venv/bin/python -m pip check --disable-pip-version-check --no-cache-dir
git diff --check
```

Pytest працює із синтетичними SQLite та mock Modbus/Telegram. Socket/DNS та
справжній subprocess.run блокуються autouse-фікстурою. Перевірки збереження
історичних marker/сторонніх runtime-файлів не є підтримкою вилучених режимів.
Окремий `device_guard_probe` перевіряє незнімний test-only guard, заборону
пристроєвих клієнтів і неможливість обійти її прапорцем запису; жодного
мережевого пакета або фізичної команди він не надсилає.

Підсумок перевірок підготовки: **450 Python-тестів і 17 JS-тестів пройшли**,
без xfailed. Окремий offline/device guard probe, pip check, systemd verify,
перевірка локальних Markdown-посилань і git diff --check успішні.
Реальні інвертори, Telegram sendMessage та системні перезапуски для тестів
не використовувалися.
