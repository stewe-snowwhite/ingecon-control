# Внутрішній HTTP-портал: план переходу й конфігурація

## Стан підготовки

Файли в `deploy/` **підготовлені, але ще не застосовані до системи**.
Мережеві правила, `/etc/nginx`, systemd і працюючі процеси не змінювались.
Для активації потрібні підтвердження власника та root-доступ. На момент перевірки
`sudo -n` вимагає пароль; не передавайте його в чат або аргументах команд.

Перевірений поточний стан, 18 жовтня 2026:

- `enp1s0`: `192.168.10.234/24`, локальна підмережа `192.168.10.0/24`;
- Nginx 1.26.3, активний і enabled, поки зі стандартним Debian-сайтом;
- Nginx поки слухає `0.0.0.0:80` та `[::]:80`; LAN-обмеження ще не активовані;
- SSH слухає порт 22; його конфігурація й правила не змінюються;
- на порту 5000 слухача не було; demo не зупинялось і не змінювалось;
- на первинній перевірці Telegram polling мав PID **1397**, користувач Linux
  `codex` (UID 1001); на фінальній повторній перевірці цей PID уже відсутній і
  polling-процесів не знайдено. Наші команди його не зупиняли;
- бот використовує `/opt/ingecon-control/instance/production/ingecon.sqlite`
  та наявний `/opt/ingecon-control/instance/telegram/bot.key`;
- production-БД має повну схему з 14 таблиць і наявного користувача `stewe`;
- Gunicorn 26.2.0 додано лише у проєктне `.venv`; системні пакети не встановлювались.

PID, адреси й стан служб треба перевірити повторно безпосередньо перед переходом.
Не зупиняти процес лише за старим PID, не перевіривши його команду, користувача
та шлях БД. Не запускати ще один polling для перевірки.

## Резервна копія та збереження даних

До змін коду створено й перевірено SQLite backup:

```text
/opt/ingecon-control/instance/production/backups/pre-http-portal-20261018T073541.482130Z.sqlite
```

Підготовка приватної конфігурації створила також свіжішу перевірену копію:

```text
/opt/ingecon-control/instance/production/backups/pre-http-portal-20261018T074423.148388Z.sqlite
```

Використано SQLite backup API з read-only джерелом; копія включає committed WAL.
`PRAGMA quick_check` успішний. Файли 0600, каталог 0700; вміст копій секретний.
Таблиці, записи, пароль/роль/session_version `stewe` не редагуються, міграцій немає.
Чинний бот продовжує власну штатну роботу; копія є узгодженим знімком на момент backup.

Для повторної підготовки **без зупинки процесів і системних змін**:

```bash
cd /opt/ingecon-control
.venv/bin/python -m deploy.prepare_portal
```

Команда робить новий backup, перевіряє наявність `stewe`, створює приватні
конфігураційні файли тільки за їх відсутності. Наявний ключ не перезаписується.
Паролі користувачів, токен Telegram і дані `instance/demo/` не копіюються в
нову БД та не змінюються. Backup не означає дозвіл на наступну міграцію.

## Схема доступу

```text
Браузер з 192.168.10.0/24
    → 192.168.10.234:80 (Nginx, monitoring.local)
    → 127.0.0.1:5000 (Gunicorn, Flask)
    → instance/production/ingecon.sqlite

Telegram polling → та сама production-БД, той самий bot.key
```

VPN, SSL-сертифікати, HTTPS-listener, перенаправлення на HTTPS і публічний
webhook не створюються. Налаштування роутера й port forwarding не змінюються.
Telegram продовжує використовувати власний вихідний HTTPS до Telegram API;
це не HTTPS для вебпорталу.

Ім’я `server_name monitoring.local` не створює DNS-запис. На клієнтських
комп’ютерах потрібний локальний DNS/hosts-запис:

```text
192.168.10.234 monitoring.local
```

DNS, mDNS/Avahi та клієнтські hosts автоматично не змінюються. Після активації
також працюватиме `http://192.168.10.234`. Адреса `.234` має залишатися адресою
цього сервера; при її зміні конфігурацію потрібно переглянути.

## Cookie, ключ і авторизація

`INGECON_COOKIE_SECURE` має default **true**. Приймаються лише явні `true`
або `false`, без урахування регістру; порожнє чи невідоме значення зупиняє
створення застосунку. Для внутрішнього HTTP у приватному файлі явно задано false.
Програмне boolean-налаштування factory, зокрема демо, також є явною конфігурацією.

`deploy/portal.env.example` копіюється без секретних значень у
`instance/production/portal.env` (0600):

```ini
INGECON_DATABASE_URI=sqlite:////opt/ingecon-control/instance/production/ingecon.sqlite
INGECON_SECRET_KEY_FILE=/opt/ingecon-control/instance/production/web-secret.key
INGECON_COOKIE_SECURE=false
INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
```

Ключ `INGECON_SECRET_KEY` зберігається вмістом `web-secret.key` (0600), поза Git,
а не відкритим значенням у unit-файлі. Він створений один раз криптографічно
випадково; існуючий секрет із середовища при першій підготовці зберігається
замість генерації нового. Запуск/перезапуск лише читає файл, не генерує ключ.
Перевіряються власник, приватні права, звичайний файл, відсутність symlink і
довжина щонайменше 32 символи. Одночасні `INGECON_SECRET_KEY` і
`INGECON_SECRET_KEY_FILE` відхиляються як неоднозначна конфігурація.

HTTPOnly, SameSite=Lax, CSRF для всіх форм/команд, ролі, scrypt, ліміти входів
і перевірка session_version залишаються чинними. Debug/reloader вимкнено.
HTTP не шифрує дані; цей режим призначений для вказаної довіреної LAN.
Production factory `portal.create_portal_app` відхиляє іншу або непідготовлену
БД та demo-маркер; автоматичного `create_all` немає.

## Gunicorn і довіра до проксі

`deploy/gunicorn.conf.py`: `127.0.0.1:5000`, один gthread-worker, два потоки,
timeout 30 секунд, службові heartbeat-файли в `/run/ingecon-web`, без daemon mode.
Nginx вже журналює запити, тому дубль access log у Gunicorn вимкнений.
`secure_scheme_headers={}` не дозволяє вважати HTTP-запит HTTPS за заголовком.

WSGI entry point — `portal_wsgi:app`. Довірені Host: `monitoring.local`,
`192.168.10.234`, `localhost`, `127.0.0.1`; решта отримують 400.
ProxyFix приймає тільки один X-Forwarded-For і лише від loopback-підключення.
Host/proto/port/prefix із forwarded-заголовків не використовуються.
Nginx **перезаписує** X-Forwarded-For реальною адресою клієнта, тому ліміти
авторизації не зводяться до одного `127.0.0.1` та не довіряють заголовку браузера.

Конструктори Modbus/NetworkProbes у вебпроцесі блокуються; конфігурація
`MODBUS_WRITES_ENABLED=False`. Telegram зберігає свою безумовну заборону
керування. Новий монітор чи опитування інверторів не запускаються.

## Nginx і мережеві правила

`deploy/nginx/ingecon-control.conf`:

```nginx
server {
    listen 192.168.10.234:80 default_server;
    server_name monitoring.local;
    allow 127.0.0.1;
    allow 192.168.10.0/24;
    deny all;
    client_max_body_size 64k;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto http;
    }
}
```

Повний файл у Git додатково очищає forwarded-заголовки та задає timeout.
`127.0.0.1` — IPv4 localhost, узгоджений із bind Gunicorn без неоднозначності IPv6.
Чинний стандартний сайт із wildcard IPv4/IPv6 listener потрібно відключити
лише після підтвердження; інакше він продовжить слухати інші адреси.

`deploy/nftables/http-guard.nft` атомарно замінює **тільки** таблицю
`inet ingecon_http_portal`: loopback:80 дозволено, `enp1s0` + IPv4-джерело
`192.168.10.0/24` + TCP:80 дозволено, решта TCP:80 відкидається, включно
з віддаленим IPv6. Policy accept та відсутність інших drop-правил означають,
що SSH:22, вихідний Telegram HTTPS і сторонні правила не змінюються.

Глобального `flush ruleset` немає. Системний `/etc/nftables.conf` містить його,
тому цей файл не перезаписується й `nftables.service` тут не запускається.
Окремий guard-unit завантажує вузьке правило після наявного nftables.service,
якщо той бере участь у boot, і перед Nginx. Nginx Requires цей guard; при його
помилці новий HTTP listener не повинен запускатися. Guard не видаляє захист
при stop. У чужих base chains можуть існувати додаткові заборони: allow у
нашій таблиці їх не обходить. Чинний ruleset потребує читання й перевірки root
перед застосуванням; з поточними правами його не було прочитано.

## Systemd та єдиний Telegram polling

Підготовлено:

| Файл | Призначення |
|---|---|
| `deploy/systemd/ingecon-web.service` | Gunicorn, localhost:5000, приватний env-файл |
| `deploy/systemd/ingecon-telegram.service` | Той самий production SQLite і наявний bot.key |
| `deploy/systemd/ingecon-http-guard.service` | Вузьке правило TCP:80, постійне після reboot |
| `deploy/systemd/nginx.service.d/ingecon.conf` | Порядок guard/network-online перед Nginx, retry при помилці старту |

Служби застосунку працюють як існуючий Linux `codex`, власник БД/ключів.
Вебкористувач `stewe` не перейменовується і не стає іншим системним акаунтом.
UMask 0077, NoNewPrivileges, ProtectSystem=strict, ProtectHome, обмежені
ReadWritePaths, MemoryMax 256M для вебу й 384M для бота. Web не бачить bot.key,
бот не бачить web-secret.key; обидва не бачать demo й backup-каталог.

`INGECON_LOCK_DIR` однаковий із наявним polling. Lock-файли не видаляються.
Сам бот бере flock за БД та bot ID до Telegram polling. Проте запуск другого
процесу для отримання помилки блокування не використовується як перевірка:
спочатку погоджено зупиняємо існуючий процес, чекаємо його завершення, потім
запускаємо службу. Токен не переміщується, не переписується та не виводиться.

## Активація — тільки після підтвердження

Нижче привілейовані кроки для майбутнього погодженого виконання. Вони ще
**не виконані**. Потрібний root shell або дозволений sudo; паролі в чат не потрібні.

1. Повторно перевірити адреси, SSH:22, TCP:5000, поточний PID бота і всі інші
   процеси з цим токеном. Якщо на 5000 з’явився інший сервер, автоматично його
   не зупиняти. Зробити свіжий SQLite backup наведеною командою під користувачем
   `codex`, зберегти root-копії поточних Nginx/unit-файлів і `nft list ruleset`.
   Перевірити, що наявна таблиця `ingecon_http_portal`, якщо існує, належить цьому
   застосунку; не перезаписувати невідому політику.
2. Під root виконати dry-run:

   ```bash
   /usr/sbin/nft --check --file /opt/ingecon-control/deploy/nftables/http-guard.nft
   ```

3. Після успішних перевірок встановити підготовлені файли:

   ```bash
   cd /opt/ingecon-control
   install -d -m 0755 /etc/ingecon-control /etc/systemd/system/nginx.service.d
   install -m 0644 deploy/nftables/http-guard.nft /etc/ingecon-control/http-guard.nft
   install -m 0644 deploy/systemd/ingecon-web.service /etc/systemd/system/ingecon-web.service
   install -m 0644 deploy/systemd/ingecon-telegram.service /etc/systemd/system/ingecon-telegram.service
   install -m 0644 deploy/systemd/ingecon-http-guard.service /etc/systemd/system/ingecon-http-guard.service
   install -m 0644 deploy/systemd/nginx.service.d/ingecon.conf /etc/systemd/system/nginx.service.d/ingecon.conf
   install -m 0644 deploy/nginx/ingecon-control.conf /etc/nginx/sites-available/ingecon-control.conf
   systemctl daemon-reload
   systemctl start ingecon-http-guard.service
   ```

4. Після резервування Nginx-конфігурації відключити **лише** відомий symlink
   `/etc/nginx/sites-enabled/default`, увімкнути посилання на новий файл,
   виконати root `nginx -t`. При помилці не перезавантажувати Nginx, відновити
   його конфігурацію з копії. Наявний master не зупиняти.
5. Запустити `ingecon-web.service`, перевірити localhost, потім reload Nginx.
   Застосування ACL/listen і правила TCP:80 є саме погодженою мережевою зміною.
6. Повторно перевірити наявність polling. Якщо він знову працює — перевірити
   ідентичність його поточного PID, надіслати йому **лише погоджений SIGTERM**,
   дочекатись завершення. Старий PID 1397 не використовувати. Якщо процес
   не завершився — зупинити перехід, без автоматичного SIGKILL або нового polling.
   Перевірити відсутність іншого polling, потім стартувати `ingecon-telegram.service`.
7. Лише після перевірки служб активувати автозапуск:

   ```bash
   systemctl enable ingecon-web.service ingecon-telegram.service ingecon-http-guard.service nginx.service
   systemctl is-active ingecon-web.service ingecon-telegram.service ingecon-http-guard.service nginx.service
   systemctl is-enabled ingecon-web.service ingecon-telegram.service ingecon-http-guard.service nginx.service
   ```

Перезавантаження ОС не виконується без окремого запиту. Перевірка enable
підтверджує налаштування boot; реальний cold boot ще не перевірявся.

## Перевірки після активації й відкат

- `ss -ltnp`: Gunicorn тільки `127.0.0.1:5000`, Nginx тільки `192.168.10.234:80`,
  SSH:22 з попередніми адресами. Правило nftables охоплює лише TCP:80.
- `GET /login` → 200; анонімний `/api/statuses` → 401; POST без CSRF → 400.
  Авторизацію зі справжнім паролем `stewe` власник перевіряє у браузері;
  пароль не запитується в чаті й не скидається для тесту.
- Cookie без Secure лише за явним false, з HttpOnly/SameSite=Lax. Debug off,
  усі Modbus-команди → blocked. Інверторні мережеві перевірки не виконуються.
- Для Telegram — рівно один polling MainPID, та сама БД/токен, без conflict 409.
  Безпечний `getMe` уже пройшов: `@ingecon_bot`; getUpdates та sendMessage для
  цієї перевірки не викликались, другого polling не було.
- Доступ із LAN перевіряється клієнтським браузером; зовнішній TCP:80 має бути
  закритий правилом. Перевірка тільки з цього хоста не доводить зовнішню фільтрацію.
  Не використовувати реальні інвертори як тестові джерела трафіку.

При проблемі конфігурації відновлюємо тільки погоджені Nginx/unit-файли з копії.
Не відновлювати всю SQLite зі старого знімка автоматично: це втратить нові
штатні записи. Ключ, токен, `stewe` та demo не видаляються й не перегенеровуються.
Не завантажувати весь старий ruleset поверх чинного, не очищати SSH-правила.
LAN-захист TCP:80 зберігається; його зняття потребує окремого погодження.
При поверненні до ручного polling спочатку переконатись, що systemd polling зупинений.

## Автоматичні перевірки підготовки

```bash
.venv/bin/python -m pytest -q
node tests/js/dashboard.test.cjs
node tests/js/panel.test.cjs
.venv/bin/python -m pip check --disable-pip-version-check --no-cache-dir
systemd-analyze verify deploy/systemd/ingecon-web.service deploy/systemd/ingecon-telegram.service deploy/systemd/ingecon-http-guard.service
git diff --check
```

Тести HTTP-порталу використовують **окремі синтетичні SQLite**, штучного
користувача з ім’ям stewe та mock Modbus/Telegram, без реальних інверторів і
без HTTP-слухача. Gunicorn `--check-config` пройшов із синтетичним WSGI/SQLite;
systemd verify успішний. Парсер Nginx повідомив `syntax is ok`, але повний
nginx -t на привілейованому порту 80 не завершився без root. `nft --check`
не пройшов через відсутність NET_ADMIN/root; застосування правил не виконувалось.
Повна root-перевірка Nginx, ruleset, active/enabled і єдиного systemd polling
залишається обов’язковою частиною погодженої активації.

Підсумок автоматичних тестів: **433 Python-тести пройшли**, з них **25 тестів
HTTP-порталу**, без xfailed і попереджень pytest; **17 JS-тестів пройшли**.
`pip check`, Gunicorn `--check-config`, systemd verify і `git diff --check`
успішні. Порівняння повного запису `stewe` з перевіреним backup підтвердило,
що він не змінився. Жодного Modbus-запиту чи write під час перевірок не було.

Джерела для конфігурації:
[Gunicorn settings](https://gunicorn.org/reference/settings/),
[Nginx proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[Nginx access](https://nginx.org/en/docs/http/ngx_http_access_module.html),
[Debian 13 nft](https://manpages.debian.org/trixie/nftables/nft.8.en.html),
[systemd.exec](https://www.freedesktop.org/software/systemd/man/257/systemd.exec.html).
