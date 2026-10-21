# Активація телеметрії INGECON SUN 100TL

Працюємо лише з `main` та наявною `/opt/ingecon-control/instance/production/ingecon.sqlite`.
Команди не сканують мережу і не виконують Modbus write. Зміна записів моделі
дозволена тільки для підтверджених інверторів. Спочатку прочитайте
[MONITORING.md](MONITORING.md) і [DISCOVERY_DEPLOY.md](DISCOVERY_DEPLOY.md).

1. Від `codex` перевірити clean Git, `git fetch origin`, збіг `main`/`origin/main`.
   Запустити `.venv/bin/python -m pytest -q`, JS-тести
   `node --test tests/js/dashboard.test.cjs tests/js/panel.test.cjs tests/js/discovery.test.cjs`,
   `bash -n deploy/activate_telemetry.sh` і `git diff --check`.
   Після змін зробити commit та `git push origin main` до будь-якої міграції.
2. Записати попередній робочий SHA та підготувати snapshot (від `codex`):

   ```bash
   cd /opt/ingecon-control
   .venv/bin/python -m deploy.discovery_release prepare \
     --expected-sha "$(git rev-parse HEAD)" \
     --previous-sha 8f151ba92ec6f436b40f8512776505e8ab2ab890
   ```

   При наступних релізах замінити `--previous-sha` фактичним попереднім робочим
   комітом. Helper перевіряє точний SHA у GitHub `main`, місце на диску,
   створює приватні копії SQLite/конфігурацій, перевіряє відновлення в окремий
   новий файл та зберігає попередній source snapshot без секретів у Git.
3. Від root виконати, підставивши точний повернутий manifest і підтверджені ID:

   ```bash
   bash /opt/ingecon-control/deploy/activate_telemetry.sh /absolute/path/to/manifest.json 1
   ```

   ID `1` — підтверджений на момент підготовки інвертор `192.168.1.12`.
   Перед виконанням перевірте, що ID і IP залишилися правильними. Для кількох
   перевірених 100TL додайте їхні ID через пробіл. Не додавайте неперевірені
   пристрої. Надалі модель обирається у вебформі; профіль на кожну IP не задається.

Скрипт зберігає поточні unit-файли та effective config, зупиняє **лише веб і
монітор**, створює ще один backup безпосередньо перед транзакційним DDL, додає
колонку `inverter.model` і чотири колонки телеметрії, позначає підтверджені ID
моделлю 100TL. Старі явно задані профілі `ingecon_100tl_input` також отримують
модель 100TL. Імена, IP, групи, порт, Unit ID, паролі, акаунти, Telegram-прив'язки,
історія та старі налаштування читання не переписуються. Невідомі пристрої
автоматично не класифікуються. Стартують існуючі веб і монітор; Telegram,
discovery, Nginx, firewall і Tailscale не перезапускаються і не змінюються.

Монітор читає тільки зареєстровані інвертори. Readiness чекає свіжого heartbeat,
двох успішних циклів і `Online`, перевіряє живі HTTP через Gunicorn/Nginx,
авторизацію, CSRF, потужність і код стану/причини для підтверджених ID.
Додаткова сесія підписується лише в пам'яті для існуючого адміністратора;
паролі не змінюються. Telegram перевіряється лише через getMe і один процес.
Фактичні показники зберігаються приватно в `backup_dir/telemetry-validated.json`.
Сигнал успіху: `TELEMETRY_DEPLOYED commit=... backup=...`.

При помилці після початку активації trap повертає веб **і монітор** на попередній
source snapshot через окремі `91-telemetry-rollback.conf`, не змінюючи Git HEAD.
Старий код сумісний з доданими колонками, тому вони залишаються в БД;
стара SQLite **не відновлюється поверх нових даних**. Конфігурація портів/Unit ID
не змінюється ні при міграції, ні при відкаті. Існуючі drop-ins не перетираються.
При виявленні вже наявного rollback override активація зупиняється до змін.
Після відкату перевіряються HTTP та служби. Не видаляйте backups або snapshot,
поки на них посилаються rollback-служби.

Без доступу root snapshot і код можна підготувати, але не можна вважати реліз
активованим. Не запускайте дублікати монітора або веба для обходу systemd.
Після підтвердженого production-успіху створити annotated production tag та
відправити його на GitHub згідно з AGENTS.md; до успіху production-тег не ставиться.
