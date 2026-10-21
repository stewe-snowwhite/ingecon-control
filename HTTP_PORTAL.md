# TASK-07 — внутрішній HTTP-портал із динамічною LAN та Tailscale

## Фактичний стан і межі виконання

Власник підтвердив активацію TASK-07. Конфігурацію перероблено для зміни
локальної адреси, підмережі й мережі провайдера, включно зі Starlink.
Фіксовані LAN-адреси та підмережі в активних конфігураціях відсутні.

Основний доступ — **`http://<поточна DHCP IPv4-адреса сервера>/`** з локального
сегмента. Локальним користувачам Tailscale не потрібний; активація вебпанелі
й Telegram не залежить від наявності або стану `tailscaled`.
**Telegram — основний дистанційний канал** моніторингу, сповіщень і вже
дозволених команд бота: /status, /inverters, /groups, /history та інших
реалізованих дій. Фізичні команди потребують окремого дозволу. Він використовує вихідний доступ до Telegram
API та ту саму SQLite, не слухає DHCP-адресу і не потребує вхідного порту.
**Tailscale — додатковий доступ до вебпанелі**, незалежний від основного LAN.
Історичні/довільні записи заблоковані незалежно від старої політики Telegram.
Окрема активація тестового Start/Stop і ліміту для всіх зареєстрованих 100TL:
[CONTROL_ACTIVATION.md](CONTROL_ACTIVATION.md). Глобальна історична конфігурація: [MODBUS.md](MODBUS.md).

**Вебпанель, Telegram і Nginx вже працюють як systemd-служби.** Поточне
очищення коду не перезапускало їх і не змінювало мережеві правила.
Для вилучення застарілого override використовуйте окремий порядок
[DEPLOYMENT_CLEANUP.md](DEPLOYMENT_CLEANUP.md), а не повторну мережеву активацію.
Наявні процеси оновляться лише після погодженого restart.
Поточний користувач не має безпарольного sudo; пароль у чат не передається.

Перевірено 18 жовтня 2026 року:

- `tailscale0` активний, поточна IPv4-адреса `100.84.37.60`; є також IPv6;
- LAN має фізичний Ethernet-інтерфейс, приватну IPv4-адресу та відповідний
  безпосередній DHCP-маршрут `scope link`; read-only автоматичне визначення успішне;
- Nginx активний, HTTP-порт 80 збережено;
- Tailscale активний і enabled; його налаштування не редагуються;
- SSH слухає порт 22; Gunicorn працює лише на 127.0.0.1:5000;
- служби `ingecon-web` і `ingecon-telegram` активні;
- новий ручний Telegram polling під час очищення не запускався;
- production SQLite, існуючі ключ і токен збережено, міграцій немає.
- одноразовий Telegram `getMe` із наявним токеном успішний; polling і
  повідомлення цією перевіркою не запускалися. Це підтверджує доступ до API
  й чинність токена; сама перевірка getMe не замінює перевірку polling.

Це знімок стану, а не гарантія для майбутнього запуску. Сценарій активації
повторно перевіряє процеси, порти й наявні Nginx-сайти. Він не завершує невідомі
процеси й не запускає другий polling поверх виявленого ручного бота.

## Схема

```text
Клієнт поточної перевіреної LAN ─┐
Клієнт наявного Tailscale ──────┼─ nftables: TCP 80 ─ Nginx :80
Loopback ──────────────────────┘                        │
                                        Gunicorn 127.0.0.1:5000
                                                       │
                      /opt/ingecon-control/instance/production/ingecon.sqlite
                                                       │
                                       Telegram long polling, один процес
```

Gunicorn має тільки `127.0.0.1:5000`. Nginx має `listen 80` і `listen [::]:80`,
`server_name monitoring.local`, upstream `http://127.0.0.1:5000`.
Сертифікати, HTTPS listener, новий VPN, публічний webhook і port forwarding не
створюються. Telegram використовує власний вихідний HTTPS до Telegram API.

`monitoring.local` потрібно вирішувати в поточну адресу сервера засобами
локального DNS/mDNS/клієнтських hosts. `server_name` сам цього не налаштовує.
DNS, Avahi, роутер і клієнтські hosts тут не змінюються. При зміні мережі DNS
може потребувати окремого оновлення; прямий доступ за поточною IP працює без
зміни конфігурації застосунку. Основний URL беріть із поточної DHCP-адреси;
переглянути адреси й прийнятний LAN можна без мережевих запитів:

```bash
ip -4 -brief address
cd /opt/ingecon-control
.venv/bin/python -m deploy.http_guard --inspect
```

Поле `lan` має формат `[інтерфейс, поточний префікс, локальна адреса]`.
Відкривайте `http://<локальна адреса>/`, без порту 5000.
Поточний додатковий VPN URL: **http://100.84.37.60/**.
Ця адреса є прикладом фактичного доступу, а не значенням firewall або bind.

## Як визначається LAN

Привілейований процес `ingecon-http-guard` раз на 5 секунд читає локальні
netlink-дані через `ip -details -json address` і `ip -json -4 route show table main`.
Це не ICMP/TCP/Modbus-перевірки та не звернення до інверторів.

LAN дозволяється лише за одночасного виконання умов:

1. Фізичний Ethernet/WiFi NIC має `UP`, `BROADCAST`, стан `UP` і sysfs `device`.
   Віртуальні bridge/veth/tun/PPP та неактивні інтерфейси не приймаються.
2. IPv4-адреса global scope належить саме RFC1918, а весь префікс лежить у
   `10/8`, `172.16/12` або `192.168/16`. Ці діапазони — класи приватних адрес,
   а не зафіксована локальна підмережа сервера.
3. Префікс має довжину від /16 до /30. Надто широкі та /31-/32 мережі
   вважаються неоднозначними; LAN-доступ для них автоматично не відкривається.
4. Є рівно один відповідний unicast-маршрут `scope link` на тому самому NIC,
   без gateway/via/nexthops/nhid, без linkdown; prefsrc узгоджений з адресою.
   DHCP-маршрут підтримується, обмеження лише на `protocol kernel` немає.
5. Немає перекриття цього LAN-префікса між різними прийнятними інтерфейсами.

Публічний IPv4, IPv6 LAN, link-local та Starlink CGNAT `100.64/10` через фізичний
інтерфейс не отримують LAN-дозволу. За прямого WAN/CGNAT-підключення сервер
залишається доступним через налаштований Tailscale. За Starlink-роутером із
звичайною приватною LAN відповідний перевірений LAN-префікс визначається сам.

Набір nftables містить трійки **інтерфейс + префікс джерела + локальна адреса
призначення**. Додатково перевіряються зворотний маршрут через вхідний інтерфейс
і локальний тип адреси призначення. Стара адреса, якої вже немає на хості,
не стає дозволом для іншої адреси на новому інтерфейсі.

LAN-елементи мають timeout **15 секунд**, поновлюються кожні 5 секунд. При
відмові визначення встановлюється порожній LAN-набір; при відмові nft/процесу
попередні LAN-елементи самі спливають. Першим кроком служба встановлює порожній
захисний набір, і лише потім намагається визначити LAN. Початковий `READY=1`
повідомляється після успішного встановлення правил.

Дозвіл для **tailscale0** залежить від вхідного інтерфейсу, а не конкретної адреси
`100.x` чи IPv6. Діапазон `100.64/10` загалом не відкривається.

## Межа довіри та безпечний окремий варіант

Автоматично встановити довіреність довільної мережі лише за IP неможливо.
LAN-режим припускає довірений локальний сегмент і роутер, який не підміняє
адреси перенаправлених з інтернету HTTP-клієнтів на власну LAN-адресу.
Звичайний port forwarding зі збереженням публічної адреси джерела буде
заблокований. Якщо роутер робить також source NAT до довіреної LAN-адреси,
сервер не може відрізнити цей трафік від локального клієнта. Налаштування
роутера тут не перевірялись і не змінювались.

Для недовіреної LAN, готелю, публічної мережі або невідомого NAT використовуйте
окремий варіант **Tailscale + loopback, без LAN HTTP**. Перед активацією під root:

```bash
install -d -o root -g root -m 0755 /etc/ingecon-control
printf '%s\n' 'INGECON_HTTP_LAN=false' > /etc/ingecon-control/http-guard.env
chown root:root /etc/ingecon-control/http-guard.env
chmod 0644 /etc/ingecon-control/http-guard.env
```

Сценарій активації зберігає наявний `http-guard.env`. Після активації зміна
цього параметра застосовується перезапуском guard-служби; `BindsTo` може
зупинити Nginx, тому порядок такий:

```bash
systemctl restart ingecon-http-guard.service
systemctl start nginx.service
```

Для довіреної LAN значення `INGECON_HTTP_LAN=true` вмикає описане динамічне
визначення. Невідома або неприйнятна підмережа не замінюється широким allow.

## Firewall і збереження доступу

`deploy/nftables/http-guard.nft` замінює атомарно тільки власну таблицю
`inet ingecon_http_portal`; `destroy` підтримується Debian 13 nftables 1.1.3.
Стороння таблиця з таким самим ім’ям, але без маркера власності, відхиляється.
Немає глобального `flush ruleset`, змін policy інших таблиць, output/forward
hooks або правил для SSH/UDP/Tailscale transport. Наш input-chain має policy
accept і відкидає **лише TCP destination port 80**, крім дозволених джерел.

Loopback і `tailscale0` допускаються до HTTP; інші адреси проходять лише за
перевіреним LAN-набором. Решта IPv4/IPv6 HTTP відкидається. Allow у нашій
base chain не обходить заборони в інших таблицях. Якщо чинна політика вже
забороняє LAN/Tailscale HTTP, її не послаблюють автоматично: потрібно окремо
перевірити root ruleset і причину недоступності.

До застосування сценарій зберігає текстовий та JSON ruleset у новий каталог
`/root/ingecon-task07-XXXXXXXX` із правами 0700; також копіює Nginx, наявні
unit/drop-in та `/etc/ingecon-control`. Виконується `nft --check`.
`nftables.service` не перезапускається: системний `/etc/nftables.conf` може
містити глобальний flush, що зачепив би Tailscale/SSH.

Якщо TCP:80 зайнятий стороннім процесом, сценарій зупиняється до змін.
SSH:22, tailscaled, маршрути, DNS, вихідні з’єднання та роутер не змінюються.
З поточними правами правила ядра прочитати/перевірити не вдалося; root-кроки
не можна вважати виконаними за результатами offline-тестів.

## БД, авторизація, секрети та заблоковане керування

Єдина production SQLite: `/opt/ingecon-control/instance/production/ingecon.sqlite`.
Архівні тестові БД й `instance/database.db` не підключаються. Factory перевіряє точний шлях,
наявність повної схеми, відсутність маркера архівної тестової БД; автоматичного DDL немає.

До змін TASK-07 зроблено нову перевірену SQLite backup API-копію:

```text
/opt/ingecon-control/instance/production/backups/pre-http-portal-20261018T083016.206719Z.sqlite
```

Перед уточненням архітектури зроблено також свіжу перевірену копію:

```text
/opt/ingecon-control/instance/production/backups/pre-http-portal-20261018T090412.724830Z.sqlite
```

Джерело відкривалось read-only, `PRAGMA quick_check` копії успішний.
Backup-файли 0600, каталог 0700. Сценарій root перед активацією робить ще одну
свіжу копію від імені `codex`, включно зі committed WAL. Старі копії не видаляються.
Записи користувача `stewe`, паролі, ролі, токен і сторонні runtime-дані не перезаписуються.
Порівняння після розробки підтвердило однакову схему й вміст усіх 14 таблиць
із цією копією, включно з повним записом `stewe`. Побайтове порівняння файлів
SQLite не використовується як доказ: backup API може змінювати службовий
заголовок копії, зберігаючи ті самі дані.

`instance/production/portal.env` — приватний файл 0600:

```ini
INGECON_DATABASE_URI=sqlite:////opt/ingecon-control/instance/production/ingecon.sqlite
INGECON_SECRET_KEY_FILE=/opt/ingecon-control/instance/production/web-secret.key
INGECON_COOKIE_SECURE=false
INGECON_LOCK_DIR=/opt/ingecon-control/instance/locks
PYTHONDONTWRITEBYTECODE=1
PYTHONUNBUFFERED=1
```

Наявний `web-secret.key` 0600 залишається постійним і не потрапляє в Git.
Перевіряються власник, права, довжина та відсутність symlink. Підготовка створює
ключ лише за відсутності; перезапуск застосунку його ніколи не генерує.
Наявний Telegram-токен використовується за старим шляхом
`/opt/ingecon-control/instance/telegram/bot.key`, без копіювання й показу значення.

`INGECON_COOKIE_SECURE` за замовчуванням true, false дозволено лише явно;
невідоме значення припиняє запуск. HTTPOnly, SameSite=Lax, CSRF, авторизація,
scrypt, ролі та ліміти входу збережені. HTTP у LAN не шифрує пароль і cookie;
Tailscale шифрує трафік між своїми вузлами.

Nginx перезаписує X-Forwarded-For адресою клієнта. ProxyFix довіряє цьому
заголовку тільки від 127.0.0.1; forwarded Host/proto/port/prefix ігноруються.
Дозволені Host: monitoring.local, localhost, loopback і поточні адреси сервера
з root-owned `/run/ingecon-http-guard/state.json`. Перевірка йде до завантаження
користувача; файл перечитується при запиті, зміна DHCP не потребує restart Flask.
Відсутній/пошкоджений/небезпечний файл не дозволяє довільні Host.

Без приватного дозволу `MODBUS_WRITES_ENABLED=False`; конструктори пристроєвих
транспортів у вебі/боті блокуються. Після окремої активації дозволено лише
фіксований 3Play-профіль, без розблокування довільних регістрів.
Окремо дозволене читання з тестової панелі не вмикає write-шляхи: [MODBUS.md](MODBUS.md).
Запуск монітора й інверторні перевірки не включено в активацію порталу.

## Systemd та один Telegram polling

- `ingecon-web`: один Gunicorn gthread-worker, два потоки, MemoryMax 256M;
  bind loopback, heartbeat worker-файли у `/run/ingecon-web`, debug off.
- `ingecon-telegram`: той самий production SQLite і bot.key, MemoryMax 384M;
  long polling без нового екземпляра поверх ручного.
- `ingecon-http-guard`: stdlib Python, 5-секундне оновлення, MemoryMax 64M,
  Type=notify, watchdog 30 секунд, Restart=on-failure. Запускає root-owned
  `/usr/local/libexec/ingecon-http-guard.py` із `python3 -I`; не виконує код
  із доступного користувачу застосунку каталогу. Мережеві сімейства тільки
  AF_NETLINK/AF_UNIX, capability лише CAP_NET_ADMIN.
- Nginx `BindsTo` guard і стартує після його готовності. При відмові guard
  Nginx зупиняється; правила не видаляються. Після відновлення guard перевірте
  стан Nginx і за потреби виконайте `systemctl start nginx`.

Веб і бот працюють як існуючий Linux `codex`. Вебкористувач `stewe` не змінюється.
ProtectSystem=strict, UMask 0077, приватні ReadWritePaths збережені. Web не бачить
токен, бот не бачить вебключ, обидва не бачать резервні копії.
Обидва використовують існуючий `instance/locks`; lock-файли не видаляються.
Бот бере flock за БД і bot ID до Telegram polling.

## Точні команди активації від root

Не встановлюються пакети й не змінюється SSH. Потрібні вже наявні Debian
Nginx/nftables/systemd та проєктне `.venv`. Tailscale для LAN і Telegram
не обов’язковий; якщо він активний до переходу, сценарій перевірить, що він
залишився активним. Так само перевіряється вже активна SSH-служба.
Перегляньте конфігурацію та сценарій перед запуском:

```bash
cd /opt/ingecon-control
less deploy/activate_portal.sh
less deploy/nftables/http-guard.nft
less deploy/nginx/ingecon-control.conf
```

У **root shell**, після вибору довіреної LAN або варіанта Tailscale-only вище:

```bash
cd /opt/ingecon-control
bash /opt/ingecon-control/deploy/activate_portal.sh
runuser -u codex -- /opt/ingecon-control/.venv/bin/python -m deploy.check_portal
```

Це точні команди; пароль у чаті або аргументах не потрібний. Сценарій робить
backup до змін, перевіряє candidate nft, встановлює root-owned guard/config,
unit-файли, запускає захист перед Nginx, перевіряє `nginx -t`, запускає веб і
бота, вмикає автозапуск чотирьох служб. Стандартний Debian symlink сайту
відключається після його backup. Невідомі сайти не видаляються: перехід
зупиняється для ручного аналізу. При помилці сценарій припиняється без
автоматичного rollback усієї БД або глобального ruleset.

Типова активація має `--require-lan`: якщо поточний локальний сегмент не
визначено безпечно, сценарій зупиняється **до зміни firewall і служб** із
поясненням. У такій ситуації перевірте DHCP, фізичний NIC і прямий маршрут
у довіреній LAN/за локальним роутером Starlink. Не замінюйте відсутній LAN
широким allow. Якщо локальний сегмент об’єктивно недовірений або неприйнятний,
окремо оберіть описаний вище `INGECON_HTTP_LAN=false`; сценарій поважає цей
явний варіант, але не вмикає його автоматично замість основного LAN-доступу.

Якщо знайдено ручний бот, сценарій друкує лише PID і завершується **до змін**.
Перевірте локально команду, користувача, БД й token-file цього процесу; після
підтвердження ідентичності можна виконати `kill -TERM <перевірений_PID>`,
дочекатися завершення й повторити сценарій. Старі PID зі звітів не
використовувати, SIGKILL та `pkill -f` не потрібні. За невідомого polling з
тим самим ботом на іншій машині спочатку зупиніть саме той polling.

Повторний запуск сценарію може перезапустити guard, через BindsTo також
короткочасно зупинити Nginx. Він знову запускає Nginx після готовності guard.
Активні Gunicorn/Telegram не перезапускаються автоматично при повторному
`systemctl start`; оновлення їх коду потребує окремого контрольованого restart.

## Перевірки після активації

`deploy.check_portal` не запускає polling і не звертається до інверторів.
Він перевіряє active/enabled вебу, бота, Nginx і guard, локальний Gunicorn,
локальний Nginx та **HTTP за поточною визначеною LAN-адресою**, використовуючи
її як справжній Host без підміни на monitoring.local. Потім перевіряє Telegram
і додатковий Tailscale, якщо він активний, через поточні IPv4/IPv6-адреси:

- GET `/login` → 200;
- анонімний GET `/api/statuses` → 401;
- POST `/login` без CSRF → 400, до перевірки облікових даних;
- cookie без Secure тільки для явного HTTP, з HttpOnly і SameSite=Lax;
- рівно один Telegram poller, його PID відповідає MainPID systemd;
- тільки Telegram `getMe`, токен і помилки з URL ніколи не друкуються.

Вимкнений/відсутній Tailscale повідомляється як недоступний додатковий канал,
а не помилка основних LAN/Telegram-перевірок. Активний, але несправний VPN
повідомляється як помилка; незалежні перевірки LAN і Telegram все одно
виконуються. Помилка будь-якого каналу не приховує перевірку інших.
За відсутності безпечно визначеної LAN звичайна перевірка завершується
помилкою. Лише для явно обраного окремого fallback-режиму можна виконати:

```bash
runuser -u codex -- /opt/ingecon-control/.venv/bin/python -m deploy.check_portal --allow-no-lan
```

Параметр дозволяє тільки діагностику без LAN; він не змінює firewall і не
підтверджує працездатність основного локального доступу.

Додатково root-команди:

```bash
ss -ltnp
systemctl status --no-pager ingecon-http-guard ingecon-web ingecon-telegram nginx tailscaled
systemctl is-enabled ingecon-http-guard ingecon-web ingecon-telegram nginx
nft list table inet ingecon_http_portal
journalctl -u ingecon-http-guard -u ingecon-web -u ingecon-telegram -u nginx -n 80 --no-pager
```

Gunicorn повинен слухати **тільки 127.0.0.1:5000**, Nginx — wildcard :80.
Перевірте, що журнали бота не містять polling conflict 409; за його наявності
не запускайте додаткові екземпляри. Не публікуйте журнали без перегляду секретів.

Насамперед з іншого пристрою **локальної мережі без Tailscale** відкрийте
`http://<поточна DHCP-адреса сервера>/`. Додатково з пристрою Tailscale відкрийте
**http://100.84.37.60/** або його нову поточну VPN-адресу. Monitoring.local
перевіряйте лише за наявності відповідного локального DNS/hosts.
Власник входить як stewe своїм існуючим паролем у браузері; пароль не
запитується в чаті та не скидається для тесту. Перевірте статуси, історію,
прив’язку Telegram і меню; команди керування мають залишатись blocked.

Перевірка власної VPN-адреси з цього сервера не доводить прохід через
вхідний tailscale0: потрібний окремий клієнт. Так само localhost не доводить
зовнішню фільтрацію. Не використовуйте інвертори для цих тестів. Reboot не
виконується: enabled підтверджує boot-конфігурацію, реальний cold boot треба
перевірити у погоджене вікно, маючи збережений SSH/Tailscale-доступ.

## Відмова й відкат

При незрозумілій LAN використовуйте Tailscale-only, а не ширше правило.
При помилці Nginx відновлюйте лише потрібні конфігураційні файли з root-backup,
перевіряйте `nginx -t`, зберігаючи HTTP guard. Не виконуйте глобальний
`flush ruleset`, не завантажуйте весь старий snapshot поверх чинних правил
Tailscale й не змінюйте SSH. Збережений ruleset призначений для аналізу та
вибіркового відновлення відповідних правил, а не сліпого replay.

Не відновлюйте production SQLite автоматично: це зітре нові штатні записи.
Не видаляйте ключі, токен, stewe, runtime-дані або lock-файли. Перед поверненням до
ручного Telegram спочатку зупиніть відповідну systemd-службу й перевірте
завершення її процесу. Зупинка guard не видаляє захист; LAN-оренди спливають.

## Автоматичні перевірки підготовки

```bash
.venv/bin/python -m pytest -q
node tests/js/dashboard.test.cjs
node tests/js/panel.test.cjs
.venv/bin/python -m pip check --disable-pip-version-check --no-cache-dir
bash -n deploy/activate_portal.sh
systemd-analyze verify deploy/systemd/ingecon-web.service deploy/systemd/ingecon-telegram.service deploy/systemd/ingecon-http-guard.service
git diff --check
```

Пройшли **476 Python-тестів**, зокрема 43 додаткові перевірки TASK-07
(10 додано для уточненої архітектури);
**17 JS-тестів**, pip check, bash syntax і systemd verify також успішні.
Тести використовують синтетичні SQLite та mock Telegram/Modbus; socket і
subprocess для зовнішніх запитів заблоковані fixture. Перевірено зміну LAN,
DHCP, відмову маршруту, CGNAT/public WAN, віртуальні NIC, перекриття підмереж,
Tailscale IPv4/IPv6, Host validation без restart, збереження SSH/output-scope,
fail-closed baseline до READY, помилку discovery, приватність runtime state,
авторизацію, CSRF, заблоковані команди та послідовність root-backup.
Додатково перевірено роботу LAN і Telegram без Tailscale, зміну DHCP-адреси
в діагностиці без DNS/restart, IPv4/IPv6 додаткового VPN, незалежність перевірок
при його відмові, відмову активації за невизначеної LAN та явний fallback.

Неможливо підтвердити повну перевірку/завантаження nft без NET_ADMIN: поточний
`nft --check` повертає `cache initialization failed: Operation not permitted`.
Root syntax/kernel check, реальні active/enabled після переходу, один polling,
повторний getMe у складі перевірки активованої служби й доступ
із віддаленого Tailscale-клієнта залишаються кроками активації, а не виконаними
результатами тестів. Жодного Modbus-запиту або Modbus write не було.

Джерела: [nftables, FIB, sets і timeout](https://www.netfilter.org/projects/nftables/manpage.html),
[systemd.service, notify/watchdog](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html),
[systemd.unit, BindsTo](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html),
[Nginx proxy headers](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

Поточне очищення застарілих режимів і системного override описано в
[DEPLOYMENT_CLEANUP.md](DEPLOYMENT_CLEANUP.md). Runtime-файли зберігаються до окремого підтвердження.
