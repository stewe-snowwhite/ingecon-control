# Виправлення адреси POST у панелі керування

## Причина

Форма огляду містить кілька кнопок `name="action"`. Механізм named access
`HTMLFormElement` повертає ці елементи через `form.action`, перекриваючи
однойменну властивість адреси форми. Для кількох елементів це `RadioNodeList`.
Його перетворення на рядок у `fetch()` дає `[object RadioNodeList]`, звідси
`POST /[object%20RadioNodeList]` і 404. За одного елемента повертається сам елемент.
`id` також бере участь у named access. Це описано в
[MDN HTMLFormElement](https://developer.mozilla.org/en-US/docs/Web/API/HTMLFormElement#issues_with_naming_elements).

Попередня JS-фікстура задавала `form.action` звичайним рядком, тому не
відтворювала поведінку браузера. Нова фікстура моделює `RadioNodeList` та
окремий елемент. На старому JS регресійні перевірки падають.

## Зміна

`static/panel.js` читає `form.getAttribute('action')`, а перевірка ID —
`form.getAttribute('id')`. URL фіксується до першого POST і зберігається
для підтвердження: зміна DOM між етапами не перенаправляє команду.
Відсутній/порожній action не породжує запиту. Після видалення форми також
перевіряються через атрибут, без `form.action.includes(...)`.
Поля `name="action"` залишені для чинного Flask-контракту.

«Увімкнути», «Вимкнути», «Застосувати ліміт», «Максимум» надсилаються на
`POST /control`. Окрема сторінка Modbus використовує звичайний HTML-submit
на `/settings/modbus/control` і цього JS-обробника не має. Видалення з форми
редагування зберігає `POST /delete/<id>`.
CSRF, credentials same-origin, roles, signed confirmation, одноразовість
і повідомлення про невідомий результат збережені. HTTP-помилки не спричиняють
автоматичного повторення. Backend, Modbus-карта, параметри команд і дозвіл
фізичного керування не змінюються.

## Перевірки

Результат пакета для цього виправлення: **304 Python-тести та 44 JavaScript-тести пройшли**.
Регресійні перевірки перед виправленням відтворили неправильний тип URL.
Пакет Python повторено поза sandbox після зависання локального asyncio-waker;
IP-сокети, DNS, зовнішні процеси й реальний Modbus залишалися заблокованими
тестовими фікстурами. Таймаути діагностувалися через `faulthandler_timeout=30`.

```bash
node tests/js/panel.test.cjs
node tests/js/dashboard.test.cjs
node tests/js/discovery.test.cjs
.venv/bin/python -m pytest -q tests/test_panel_routes.py tests/test_panel_asset.py tests/test_dashboard.py tests/test_security.py tests/test_inverter_controls.py tests/test_modbus_settings.py
```

Покрито всі чотири операції, обидва етапи POST, CSRF та confirmation token,
один елемент і RadioNodeList, захоплення URL, скасування/подвійне підтвердження,
таймаут і HTTP 400/401/403/500. Python-тести перевіряють HTML-маршрут, усі ролі
та реальне виконання через mock. Жодного POST керування в production не потрібно.

## Контрольоване оновлення без перезапуску служб

Змінюється єдиний production-файл `static/panel.js`. Він читається Flask
при HTTP-запиті, тому заміна одразу впливає на нові завантаження сторінки.
Для цього виправлення не потрібні міграції, пакети, новий build, зміни Nginx,
systemd чи restart. Не запускайте активацію дозволів Modbus повторно.

1. Підготувати кандидат в окремій тимчасовій копії. Production JS не змінювати.
2. З чистої поточної main створити backup через `deploy.discovery_release prepare`,
   указавши поточний опублікований SHA як expected і previous. Це SQLite backup
   з перевіркою відновлення, конфігурація й архів попереднього source.
   Також скопіювати чинний приватний `inverter-controls.json` до backup і додати
   його SHA-256 до `protected`; не виводити вміст секретів.
3. Провести тести кандидата. Закомітити й опублікувати main. Для збереження
   активного JS до публікації дозволяється використати `git --work-tree` з
   тимчасовою копією та явним списком змінених файлів. Додаткова гілка не потрібна.
4. Скопіювати в checkout тільки нові тести, документацію й helper. До погодженої
   активації єдиною очікуваною локальною різницею залишається попередній
   `static/panel.js`. Це збережений production asset, не стороння зміна.
5. Підготувати приватний manifest, вказавши шлях до backup-manifest і SHA fix:

```bash
cd /opt/ingecon-control
.venv/bin/python -m deploy.panel_asset prepare --manifest /АБСОЛЮТНИЙ/ШЛЯХ/manifest.json --expected-sha SHA_FIX
```

Помічник звіряє GitHub/main, цілісність конфігурації та старого asset,
зберігає точний кандидат із Git, контрольні суми й поточні PID служб.
Він повертає шлях до `panel-asset.json`. Підготовка не змінює активний JS.
Після погодження застосування, від користувача codex:

```bash
.venv/bin/python -m deploy.panel_asset apply --manifest /АБСОЛЮТНИЙ/ШЛЯХ/panel-asset.json
```

Активація атомарно замінює лише JS, звіряє HTTP-вміст через Gunicorn/Nginx,
перевіряє login, anonymous API=401, відхилення login POST без CSRF,
контрольні суми конфігурації та незмінність PID служб. Команди інверторам,
додаткові Telegram-запити та сканування мережі не виконуються.
При помилці перевірок автоматично повертається старий asset, лише якщо
його не змінив інший процес. SQLite не відновлюється поверх нових даних.
Для ручного відкату без залежності від мережевого доступу GitHub:

```bash
.venv/bin/python -m deploy.panel_asset rollback --manifest /АБСОЛЮТНИЙ/ШЛЯХ/panel-asset.json
```

Сторонні зміни asset, source checksum, checkout, конфігурації або інших
робочих файлів зупиняють операцію. Відкат не використовує reset/clean і не
перезаписує інші файли. Після нього різниця JS із HEAD очікувана й записана
в manifest. Backup і source snapshot зберігаються для діагностики.
Після успішної production-перевірки створити annotated release tag і push тега.
У браузері після активації потрібне оновлення сторінки; відкрита вкладка може
ще виконувати старий уже завантажений JS. Перевіряти фізичні команди вручну
має власник обладнання.
