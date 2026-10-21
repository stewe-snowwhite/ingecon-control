# Production release: Inverter Discovery

Цей сценарій застосовується до наявного `/opt/ingecon-control`, єдиної робочої гілки `main` і production SQLite `instance/production/ingecon.sqlite`. Він не запускає сканувань, не погоджує CIDR автоматично, не змінює керування, firewall, маршрути, SSH, Tailscale або Nginx. Фізичні Modbus-записи залишаються заблокованими. Правило commit/push/release-tag постійно закріплено в [AGENTS.md](AGENTS.md).

## Передумови й початковий стан

Перед змінами потрібні чистий Git, перевірені віддалені refs, успішні тести й достатнє місце. На початку підготовки цього розгортання: `c5583b2f309389210405466d352011918da211c2`, вебпанель і один Telegram працювали; служб монітора й discovery не було. У production було 0 інверторів, 1 користувач і 1 Telegram-прив'язка, без heartbeat монітора. Це початкове спостереження, а не результат активації. Старий працюючий worker не публікує SHA; точно визначено лише попередній commit на диску. Snapshot фіксує це обмеження.

Перевірка `sudo -n -l` показала вимогу пароля, тому непривілейований агент може підготувати, протестувати, закомітити, відправити код та зробити резервні копії, але встановлення systemd і перезапуск потребують виконання наведеної команди власником root-сесії. Пароль не передається в чат і не зберігається. Сам дозвіл на розгортання не надає процесу прав root.

## Підготовка й GitHub — користувач codex

```bash
cd /opt/ingecon-control
git status --short
git branch --show-current
git fetch origin --tags
.venv/bin/python -m pytest -q
node --test tests/js/dashboard.test.cjs tests/js/panel.test.cjs tests/js/discovery.test.cjs
.venv/bin/python -m pip --disable-pip-version-check --no-cache-dir check
bash -n deploy/activate_discovery.sh
systemd-analyze verify deploy/systemd/ingecon-web.service deploy/systemd/ingecon-monitor.service deploy/systemd/ingecon-discovery.service
git diff --check
```

Усі зміни мають бути закомічені в `main` перед наступними командами. Не додавати runtime-файли, резервні копії та секрети. Не створювати інших робочих гілок і не виконувати force push. `--previous-sha` має вказувати на попередню перевірену версію на диску; у наведеному прикладі це стан перед виправленням release-скриптів для `main`.

```bash
git push origin main
.venv/bin/python -m deploy.discovery_release prepare \
  --expected-sha "$(git rev-parse HEAD)" \
  --previous-sha d9031dea09448e29e960bb7b9ef83edcdb2aa0fa
```

Helper повторно перевіряє clean Git, поточну гілку `main`, SHA й відповідність GitHub `refs/heads/main`. Він вимагає не менше 256 MiB вільного місця або восьми розмірів БД, залежно від того, що більше. Потім:

- Online SQLite backup у новий приватний каталог `instance/production/backups/discovery-<UTC>-<id>/production.sqlite`.
- Відновлення **в окремий новий** `restore-verified.sqlite`, `integrity_check` і потокове порівняння SQL dump. Робоча БД не перезаписується.
- Приватні копії наявних portal.env, SECRET_KEY-файлу, Telegram-токена й Modbus JSON без виведення їх вмісту. Вони залишаються в захищеному backup-каталозі, недоступному вебслужбі.
- `git archive` попереднього SHA і його код у `instance/releases/discovery-<UTC>-<id>/previous-source`, окремо від секретів, для відкату без зміни Git-дерева.
- Приватний `manifest.json` із SHA, шляхами та контрольними сумами захищених конфігурацій.

У stdout буде **точний абсолютний шлях manifest.json**. Зберегти його для root-команди й звіту. Backup, секрети, source snapshot і manifest не потрапляють у Git (`instance/` ігнорується).

## Активація — наявна root-сесія

Підставити шлях, повернутий попередньою командою:

```bash
bash /opt/ingecon-control/deploy/activate_discovery.sh /opt/ingecon-control/instance/releases/discovery-<UTC>-<id>/manifest.json
```

Скрипт перевіряє SHA на GitHub і відсутність незакомічених змін, контрольні суми секретів, придатність backup, відсутність старих незавершених сканувань та стан веба/Telegram/Nginx. Він зберігає наявні базові unit-файли, drop-ins, effective configuration та active/enabled стани **до змін** у захищений `backup_dir/systemd/`.

Далі від `codex` викликається `discovery_schema`: ще один перевірений backup безпосередньо перед трьома адитивними таблицями. Жодні наявні таблиці чи записи користувачів, інверторів і Telegram не переписуються. Встановлюються лише `ingecon-web`, `ingecon-monitor`, `ingecon-discovery` з `User=codex`, малими лімітами RAM/потоків, `NoNewPrivileges` та без capabilities. Вебу додається тільки AF_NETLINK до дозволених сімейств сокетів, щоб `ip` міг читати локальні інтерфейси й маршрути. Це не зміна маршрутизації чи firewall.

Монітор стартує з інтервалом 30 с і обробляє лише вже зареєстровані інвертори; при порожньому списку записує heartbeat без мережевих перевірок. Discovery чекає ручних завдань, не має розкладу й сам не створює завдання. Вмикається автозапуск цих служб. Перезапускається тільки вебпанель; чинний Telegram-процес, Nginx, HTTP guard і Tailscale не перезапускаються. Наявна версія Telegram вже запускалася після коміту backend-блокування записів; root-скрипт додатково перевіряє одного poller та getMe без getUpdates/sendMessage.

## Перевірка та автоматичний відкат

Після запуску скрипт перевіряє live HTTP на 127.0.0.1:5000 і через Nginx :80: login, cookie policy, відмову анонімному API, CSRF rejection, авторизовані overview/add/ручну форму/history/Telegram settings, інвентар із БД та свіжий heartbeat монітора. Діагностична авторизована сесія підписується наявним ключем у пам'яті для наявного Administrator; пароль, роль, session_version і Telegram-прив'язки в БД не змінюються. Cookie й секрет не друкуються. Це перевірка авторизованого доступу; введення справжнього пароля в браузері власник перевіряє самостійно.

Потрібні active/enabled для веба, монітора, discovery та наявного Telegram, незмінні секрети та backend write lock. Нові помилки journal рівня err+ від початку активації зберігаються приватно й спричиняють відкат. Власник також може переглянути журнал локально, без копіювання токенів у чат.

Будь-яка помилка після початку змін запускає ERR/INT/TERM trap: нові служби зупиняються, базові unit-файли й стани відновлюються, веб запускається з попереднього source snapshot через окремий rollback drop-in. Початкові drop-ins не редагуються. Git-дерево й незакомічена робота не перезаписуються, `git reset --hard` не застосовується. Повторно перевіряється HTTP. Повернення до початкового відсутнього монітора означає його початковий inactive стан.

Три нові таблиці сумісні з попереднім кодом; тому при відкаті їх залишають, **БД автоматично не відновлюється поверх нових робочих даних**. Якщо виявиться інша несумісність, виконання зупиняється для окремого рішення щодо даних. Новий activation run потребує нового manifest; скрипт не перетирає rollback drop-in або попередні backups.

Сигнал успішної активації: `DEPLOYMENT_CHECKED commit=<SHA> backup=<path>`. Це ще не опублікований release tag.

## Тег після успішної production-перевірки — codex

Тільки після успішної активації й перевірок створити **новий** annotated tag, наприклад `production-discovery-YYYYMMDDTHHMMSSZ`:

```bash
git tag -a production-discovery-YYYYMMDDTHHMMSSZ -m 'Production inverter discovery: validated HTTP, monitor, Telegram and idle discovery worker' <перевірений-SHA>
git push origin refs/tags/production-discovery-YYYYMMDDTHHMMSSZ
git ls-remote origin refs/heads/main 'refs/tags/production-discovery-YYYYMMDDTHHMMSSZ*'
```

Не створювати production-тег для підготовленого, але не активованого або невдалого розгортання. Звіт повинен містити гілку, повний SHA, тег або причину його відсутності, push result, backup path, міграції, стани служб і факт відкату. Після успішного запуску система лишається працювати; адміністратор сам погоджує CIDR і запускає пошук зі сторінки «Додати інвертор».
