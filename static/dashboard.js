'use strict';
(() => {
  const STATES = ['Online', 'Offline', 'Degraded', 'Unknown'];
  function formatUtc(value) {
    if (value === null || value === undefined) return '—';
    return new Date(value * 1000).toISOString().replace('T', ' ').slice(0, 19);
  }
  function filterRows(rows, filters) {
    const q = filters.q.trim().toLocaleLowerCase();
    return rows.filter(row => (!filters.group || row.group === filters.group) &&
      (!filters.state || row.state === filters.state) &&
      (!q || [row.name, row.ip_address, row.group].some(value => value.toLocaleLowerCase().includes(q))));
  }
  const SORT_LABELS = {name: 'Назва', group: 'Група', active_power_w: 'Потужність', power_limit_percent: 'Обмеження'};
  const textOrder = new Intl.Collator('uk', {numeric: true, sensitivity: 'base'});
  function sortRows(rows, key, direction = 'asc') {
    if (!Object.prototype.hasOwnProperty.call(SORT_LABELS, key) || !['asc', 'desc'].includes(direction)) return [...rows];
    const sign = direction === 'desc' ? -1 : 1;
    const numeric = key === 'active_power_w' || key === 'power_limit_percent';
    return [...rows].sort((a, b) => {
      let comparison;
      if (numeric) {
        const absentA = !Number.isFinite(a[key]), absentB = !Number.isFinite(b[key]);
        // Missing/stale values remain last in both directions. Zero is a real value.
        if (absentA !== absentB) return absentA ? 1 : -1;
        comparison = absentA ? 0 : a[key] - b[key];
      } else comparison = textOrder.compare(a[key], b[key]);
      return comparison * sign || a.id - b.id;
    });
  }
  function counts(rows) {
    const result = {total: rows.length, Online: 0, Offline: 0, Degraded: 0, Unknown: 0};
    rows.forEach(row => result[STATES.includes(row.state) ? row.state : 'Unknown']++);
    return result;
  }
  function formatPower(watts) {
    return Number.isFinite(watts) ? (watts / 1000).toFixed(2).replace('.', ',') + ' кВт' : '—';
  }
  function pollingPolicy(snapshot, now) {
    const policy = snapshot && snapshot.polling;
    const active = policy && policy.temporary === true &&
      Number.isInteger(policy.interval_seconds) && policy.interval_seconds >= 1 && policy.interval_seconds <= 86400 &&
      Number.isFinite(policy.expires_at) && policy.expires_at > now;
    return {interval_seconds: active ? policy.interval_seconds : 30,
      expires_at: active ? policy.expires_at : null, temporary: !!active};
  }
  function formatLimitReason(code, catalog) {
    if (!Number.isInteger(code)) return {label: '—', hint: ''};
    // Cached pre-upgrade HTML has no catalog until the web worker is restarted.
    if (!catalog) return {label: 'Код: ' + code, hint: ''};
    const entry = Object.prototype.hasOwnProperty.call(catalog, code) ? catalog[code] : null;
    return entry ? {label: entry.label + ' (' + code + ')', hint: entry.hint} :
      {label: 'Невідома причина (код ' + code + ')', hint: 'Цей код відсутній у довіднику причин для INGECON SUN 100TL.'};
  }
  function freshSnapshot(snapshot, now) {
    const monitor = {...snapshot.monitor};
    const ttl = monitor.stale_after_seconds;
    const heartbeatAge = now - monitor.last_seen;
    monitor.alive = monitor.alive && monitor.last_seen !== null && heartbeatAge >= 0 && heartbeatAge <= ttl;
    const inverters = snapshot.inverters.map(row => {
      const age = now - row.checked_at;
      if (monitor.alive && !row.stale && row.checked_at !== null && age >= 0 && age <= ttl) return row;
      return {...row, state: 'Unknown', stale: true, latency_ms: null, power_limit_percent: null,
        operating_state_code: null, operating_state_label: null, active_power_raw: null,
        active_power_w: null, limit_reason_code: null,
        reason_text: monitor.alive ? 'Дані перевірки застарілі.' : 'Немає актуального heartbeat монітора.',
        checks: {icmp: {ok: null}, tcp: {ok: null}, modbus: {ok: null}}};
    });
    return {monitor, inverters};
  }
  function validSnapshot(snapshot) {
    return snapshot && Number.isFinite(snapshot.generated_at) && snapshot.monitor &&
      typeof snapshot.monitor.alive === 'boolean' &&
      Number.isFinite(snapshot.monitor.interval_seconds) && snapshot.monitor.interval_seconds > 0 &&
      Number.isFinite(snapshot.monitor.stale_after_seconds) && snapshot.monitor.stale_after_seconds > 0 &&
      (snapshot.monitor.last_seen === null || Number.isFinite(snapshot.monitor.last_seen)) &&
      Array.isArray(snapshot.inverters) && snapshot.inverters.every(row =>
        Number.isInteger(row.id) && row.id > 0 && STATES.includes(row.state) &&
        ['name', 'group', 'ip_address'].every(key => typeof row[key] === 'string') &&
        [row.checked_at, row.last_success_at].every(value => value === null || Number.isFinite(value)) &&
        typeof row.stale === 'boolean' && row.checks &&
        ['icmp', 'tcp', 'modbus'].every(key => row.checks[key] &&
          [true, false, null].includes(row.checks[key].ok)));
  }
  const model = {formatUtc, formatPower, formatLimitReason, pollingPolicy, filterRows, sortRows, counts, freshSnapshot, validSnapshot};
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  if (typeof document === 'undefined') return;
  const root = document.getElementById('dashboard');
  if (!root) return;

  const el = id => document.getElementById(id);
  const role = root.dataset.role;
  const limitReasons = root.dataset.limitReasons ? JSON.parse(root.dataset.limitReasons) : null;
  const selected = new Set();
  let snapshot = null;
  let receivedAt = 0;
  let timer = null;
  let fetching = false;
  let refreshRequested = false;
  let stopped = false;
  let digest = '';
  let refreshEvery = 30000;
  let policyExpiry = null;
  let settingsPending = false;
  const inputs = {q: el('search'), group: el('group-filter'), state: el('state-filter')};
  const sorting = {key: '', direction: 'asc'};
  const sortButtons = [...root.querySelectorAll('[data-sort-key]')];
  const readFilters = () => Object.fromEntries(Object.entries(inputs).map(([key, node]) => [key, node.value]));
  const panel = window.IngeconPanel;
  function node(tag, text, className) {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (className) value.className = className;
    return value;
  }
  function cell(label) { const value = node('td'); value.dataset.label = label; return value; }

  function deviceRow(row) {
    const tr = node('tr'); tr.dataset.id = String(row.id);
    if (role !== 'Viewer') {
      const td = cell('Обрати'); td.className = 'select-cell';
      const checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.name = 'selected_ids'; checkbox.value = String(row.id);
      checkbox.checked = selected.has(row.id); checkbox.setAttribute('aria-label', 'Обрати ' + row.name);
      td.append(checkbox); tr.append(td);
    }
    const device = cell('Пристрій');
    const name = node(role === 'Administrator' ? 'a' : 'strong', row.name, 'device-name');
    if (role === 'Administrator') name.href = '/edit/' + row.id;
    device.append(name, node('small', row.ip_address, 'mono'));
    const group = cell('Група'); group.append(node('span', row.group, 'group-tag'));
    const status = cell('Доступність'); status.append(node('span', row.state, 'badge ' + row.state.toLowerCase()));
    if (row.stale) status.append(node('small', 'Застарілі дані', 'muted'));
    if (row.check_requested) status.append(node('small', 'Перевірку заплановано', 'muted'));
    if (row.reason_text) status.append(node('small', row.reason_text, 'muted'));
    const checks = cell('Перевірки'); const probes = node('div', undefined, 'probe-list');
    ['icmp', 'tcp', 'modbus'].forEach(key => {
      const result = row.checks[key];
      const kind = result.ok === true ? 'ok' : result.ok === false ? 'failed' : 'unavailable';
      const badge = node('span', key.toUpperCase() + ' ' + (result.ok === true ? '✓' : result.ok === false ? '×' : '—'), 'probe ' + kind);
      badge.title = result.error || 'Без помилки'; probes.append(badge);
    });
    checks.append(probes, node('small', 'Затримка Modbus (мс): ' + (row.latency_ms ?? '—'), 'muted'));
    const checked = cell('Час (UTC)');
    checked.append(node('span', formatUtc(row.checked_at), 'mono'),
      node('small', 'Остання успішна (UTC): ' + formatUtc(row.last_success_at), 'muted'));
    const operating = cell('Робочий стан'); operating.dataset.telemetry = '';
    operating.append(node('span', row.operating_state_label ?? '—'));
    if (row.operating_state_code != null) operating.append(node('small', 'Код: ' + row.operating_state_code, 'muted'));
    const power = cell('Активна потужність'); power.dataset.telemetry = ''; power.textContent = formatPower(row.active_power_w);
    const limit = cell('Ліміт'); limit.dataset.telemetry = ''; limit.textContent = row.power_limit_percent == null ? '—' : row.power_limit_percent + '%';
    const reason = cell('Причина обмеження'); reason.dataset.telemetry = '';
    const description = formatLimitReason(row.limit_reason_code, limitReasons);
    const reasonName = node('span', description.label, 'limit-reason');
    if (description.hint) reasonName.title = description.hint;
    if (Number.isInteger(row.limit_reason_code)) reasonName.dataset.limitReasonCode = String(row.limit_reason_code);
    reason.append(reasonName);
    tr.append(device, group, status, checks, checked, operating, power, limit, reason);
    return tr;
  }

  function selectionLabel() {
    if (role === 'Viewer') return;
    el('selection-count').textContent = 'Обрано: ' + selected.size;
    const boxes = [...el('device-rows').querySelectorAll('input[name="selected_ids"]')];
    const all = el('select-all');
    all.checked = boxes.length > 0 && boxes.every(box => box.checked);
    all.indeterminate = boxes.some(box => box.checked) && !all.checked;
  }
  function render() {
    if (!snapshot) return;
    // Use the server clock plus monotonic elapsed time, independent of the browser timezone/clock.
    const now = snapshot.generated_at + (performance.now() - receivedAt) / 1000;
    const policy = pollingPolicy(snapshot, now);
    policyExpiry = policy.expires_at;
    const nextInterval = policy.interval_seconds * 1000;
    if (refreshEvery !== nextInterval) {
      refreshEvery = nextInterval;
      if (!fetching && !stopped && !document.hidden) scheduleRefresh();
    }
    if (el('polling-status')) {
      const remaining = policy.temporary ? Math.ceil((policy.expires_at-now)/60) : 0;
      el('polling-status').textContent = 'Запитаний інтервал опитування: ' + policy.interval_seconds + ' с. ' +
        (policy.temporary ? 'Залишилося до ' + remaining + ' хв; потім — 30 с. ' : 'За замовчуванням — 30 с. ') +
        'Період монітора з урахуванням тривалості циклу: ' + Number(snapshot.monitor.interval_seconds.toFixed(1)) + ' с.';
      if (snapshot.polling?.error) el('polling-status').textContent += ' Помилка налаштування; використовується 30 с.';
    }
    if (el('polling-reset')) el('polling-reset').disabled = settingsPending || !policy.temporary;
    if (el('polling-seconds') && document.activeElement !== el('polling-seconds')) el('polling-seconds').value = String(policy.interval_seconds);
    if (panel.connected) el('refresh-label').textContent = 'Оновлено · ' + formatUtc(snapshot.generated_at) + ' UTC · кожні ' + policy.interval_seconds + ' с';
    const current = freshSnapshot(snapshot, now);
    const summary = counts(current.inverters);
    document.querySelectorAll('[data-count]').forEach(item => { item.textContent = summary[item.dataset.count]; });
    const monitor = current.monitor;
    el('heartbeat').textContent = monitor.alive ? 'Монітор активний' : 'Монітор неактивний або дані застарілі';
    el('heartbeat').className = 'badge ' + (monitor.alive ? 'online' : 'unknown');
    el('heartbeat-time').textContent = 'Heartbeat (UTC): ' + formatUtc(monitor.last_seen);
    el('monitor-detail').textContent = 'Інтервал ' + monitor.interval_seconds + ' с · Актуальність ' + monitor.stale_after_seconds + ' с';
    el('connection-warning').hidden = monitor.alive && !current.inverters.some(row => row.stale);
    el('connection-warning').textContent = !monitor.alive ?
      'Монітор неактивний або дані застарілі. Статуси показано як Unknown.' :
      'Частина результатів застаріла або ще не отримана; ці пристрої показано як Unknown.';
    const rows = sortRows(filterRows(current.inverters, readFilters()), sorting.key, sorting.direction);
    // Hidden/deleted devices are never retained in a command selection.
    const visible = new Set(rows.map(row => row.id));
    for (const id of selected) if (!visible.has(id)) selected.delete(id);
    el('list-count').textContent = 'Показано ' + rows.length + ' із ' + summary.total;
    el('empty-list').hidden = rows.length > 0;
    el('empty-list').textContent = summary.total ? 'За цими фільтрами нічого не знайдено.' :
      'Інвертори не знайдені. Додайте перший пристрій, щоб почати моніторинг.';
    const nextDigest = JSON.stringify(rows);
    if (nextDigest !== digest) {
      const focusId = document.activeElement && document.activeElement.name === 'selected_ids' ? document.activeElement.value : null;
      const fragment = document.createDocumentFragment(); rows.forEach(row => fragment.append(deviceRow(row)));
      el('device-rows').replaceChildren(fragment); digest = nextDigest;
      if (focusId) el('device-rows').querySelector('input[value="' + focusId + '"]')?.focus({preventScroll: true});
    } else el('device-rows').querySelectorAll('input[name="selected_ids"]').forEach(box => { box.checked = selected.has(Number(box.value)); });
    selectionLabel();
  }

  el('device-rows').addEventListener('change', event => {
    if (event.target.name !== 'selected_ids') return;
    const id = Number(event.target.value);
    if (event.target.checked && selected.size >= 100) {
      event.target.checked = false; el('page-alert').textContent = 'Можна обрати до 100 інверторів за одну дію.'; el('page-alert').hidden = false;
    } else if (event.target.checked) selected.add(id);
    else selected.delete(id);
    selectionLabel();
  });
  if (role !== 'Viewer') el('select-all').addEventListener('change', event => {
    selected.clear();
    const boxes = [...el('device-rows').querySelectorAll('input[name="selected_ids"]')];
    boxes.forEach((box, index) => { box.checked = event.target.checked && index < 100; if (box.checked) selected.add(Number(box.value)); });
    selectionLabel();
  });
  const filterChanged = () => {
    const params = new URLSearchParams();
    Object.entries(readFilters()).forEach(([key, value]) => { if (value) params.set(key, value); });
    window.history.replaceState(null, '', window.location.pathname + (params.size ? '?' + params.toString() : ''));
    if (snapshot) render();
    else refreshRequested = true;
  };
  el('filters').addEventListener('submit', event => { if (snapshot) { event.preventDefault(); filterChanged(); } });
  inputs.q.addEventListener('input', filterChanged);
  inputs.group.addEventListener('change', filterChanged); inputs.state.addEventListener('change', filterChanged);

  function setSort(key, direction) {
    if (key && !Object.prototype.hasOwnProperty.call(SORT_LABELS, key)) return;
    sorting.key = key; sorting.direction = direction === 'desc' ? 'desc' : 'asc';
    sortButtons.forEach(button => {
      const active = button.dataset.sortKey === key;
      button.closest('th').setAttribute('aria-sort', active ? (sorting.direction === 'asc' ? 'ascending' : 'descending') : 'none');
      button.querySelector('[data-sort-indicator]').textContent = active ? (sorting.direction === 'asc' ? '↑' : '↓') : '↕';
      const nextDirection = active && sorting.direction === 'asc' ? 'за спаданням' : 'за зростанням';
      const label = 'Сортувати ' + nextDirection + ': ' + button.dataset.sortLabel;
      button.setAttribute('aria-label', label); button.title = label;
    });
    if (el('sort-key')) el('sort-key').value = key;
    if (el('sort-direction')) {
      el('sort-direction').value = sorting.direction;
      el('sort-direction').disabled = !key;
    }
    if (el('sort-status')) el('sort-status').textContent = key ?
      'Сортування: ' + SORT_LABELS[key] + ', ' + (sorting.direction === 'asc' ? 'за зростанням.' : 'за спаданням.') :
      'Сортування: за порядком додавання.';
    render();
  }
  sortButtons.forEach(button => button.addEventListener('click', () => setSort(button.dataset.sortKey,
    sorting.key === button.dataset.sortKey && sorting.direction === 'asc' ? 'desc' : 'asc')));
  el('sort-key')?.addEventListener('change', () => setSort(el('sort-key').value, 'asc'));
  el('sort-direction')?.addEventListener('change', () => setSort(sorting.key, el('sort-direction').value));

  el('polling-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    if (settingsPending) return;
    const form = el('polling-form');
    const data = new FormData(form);
    data.set('intent', event.submitter?.value || 'temporary');
    const feedback = el('polling-feedback');
    settingsPending = true; el('polling-apply').disabled = true; el('polling-reset').disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(form.getAttribute('action'), {method:'POST',body:data,
        credentials:'same-origin',headers:{Accept:'application/json'},signal:controller.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Не вдалося зберегти інтервал.');
      if (!result.polling || !Number.isFinite(result.generated_at)) throw new Error('Некоректна відповідь сервера.');
      if (snapshot) { snapshot = {...snapshot, polling:result.polling}; render(); }
      feedback.textContent = result.message; feedback.className = 'notice info';
      refresh();
    } catch (error) {
      feedback.textContent = error.name === 'AbortError' || error instanceof TypeError || error instanceof SyntaxError ?
        'Не вдалося підтвердити зміну інтервалу. Оновіть сторінку, щоб перевірити налаштування.' : error.message;
      feedback.className = 'notice danger';
    } finally {
      clearTimeout(timeout); settingsPending = false; el('polling-apply').disabled = false;
      el('polling-reset').disabled = !snapshot || !pollingPolicy(snapshot, snapshot.generated_at+(performance.now()-receivedAt)/1000).temporary;
      feedback.hidden = false;
    }
  });

  function updateGroups() {
    const value = inputs.group.value;
    const groups = [...new Set(snapshot.inverters.map(row => row.group))].sort();
    if (value && !groups.includes(value)) groups.push(value);
    const options = groups.map(group => { const option = node('option', group); option.value = group; return option; });
    const any = node('option', 'Усі групи'); any.value = '';
    inputs.group.replaceChildren(any, ...options); inputs.group.value = value;
  }
  function markInitialUnknown() {
    // No API sample has been received: do not leave the initial HTML looking live indefinitely.
    let total = 0;
    document.querySelectorAll('[data-count]').forEach(item => {
      if (item.dataset.count === 'total') total = Number(item.textContent) || 0;
    });
    document.querySelectorAll('[data-count]').forEach(item => {
      if (item.dataset.count !== 'total') item.textContent = item.dataset.count === 'Unknown' ? total : 0;
    });
    el('heartbeat').textContent = 'Heartbeat не підтверджено';
    el('heartbeat').className = 'badge unknown';
    el('connection-warning').hidden = false;
    el('connection-warning').textContent = 'Актуальність початкового знімка не підтверджена. Статуси показано як Unknown.';
    el('device-rows').querySelectorAll('.badge').forEach(badge => {
      badge.textContent = 'Unknown'; badge.className = 'badge unknown';
    });
    el('device-rows').querySelectorAll('.probe').forEach(probe => {
      probe.textContent = probe.textContent.split(' ')[0] + ' —'; probe.className = 'probe unavailable';
    });
    el('device-rows').querySelectorAll('td[data-label="Ліміт"]').forEach(cell => { cell.textContent = '—'; });
    el('device-rows').querySelectorAll('[data-telemetry]').forEach(cell => { cell.textContent = '—'; });
  }
  function scheduleRefresh() {
    clearTimeout(timer);
    const now = snapshot ? snapshot.generated_at+(performance.now()-receivedAt)/1000 : 0;
    const delay = policyExpiry === null ? refreshEvery : Math.min(refreshEvery, Math.max(1, (policyExpiry-now)*1000));
    timer = setTimeout(refresh, refreshRequested ? 0 : delay); refreshRequested = false;
  }
  async function refresh() {
    if (document.hidden || stopped) return;
    if (fetching) { refreshRequested = true; return; }
    fetching = true; clearTimeout(timer);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(root.dataset.api, {credentials: 'same-origin', cache: 'no-store',
        headers: {'Accept': 'application/json'}, signal: controller.signal});
      if (response.status === 401 || response.status === 403) {
        stopped = true; panel.connected = false;
        throw new Error('Сесію завершено або доступ відкликано. Увійдіть знову.');
      }
      if (!response.ok) throw new Error('Сервер не повернув статуси. Повторна спроба через ' + refreshEvery/1000 + ' с.');
      const data = await response.json();
      if (!validSnapshot(data)) throw new Error('Некоректна відповідь API. Останній знімок може бути застарілим.');
      snapshot = data; receivedAt = performance.now(); panel.connected = true;
      updateGroups(); render(); el('api-warning').hidden = true;
    } catch (error) {
      panel.connected = false;
      el('api-warning').textContent = error instanceof SyntaxError ?
        'Сервер повернув некоректну відповідь. Показано останній знімок.' :
        error.name === 'AbortError' || error instanceof TypeError ?
        'Втрачено зв’язок із сервером. Показано останній знімок; повторна спроба через ' + refreshEvery/1000 + ' с.' : error.message;
      el('api-warning').hidden = false; el('refresh-label').textContent = 'Автооновлення недоступне';
      if (snapshot) render(); else markInitialUnknown();
    } finally {
      clearTimeout(timeout); fetching = false;
      if (!stopped && !document.hidden) {
        scheduleRefresh();
      }
    }
  }
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer); if (!document.hidden) refresh();
  });
  document.addEventListener('commandcomplete', () => { digest = ''; refresh(); });
  // An independent lightweight tick expires cached samples even while the API is unavailable.
  setInterval(() => { if (!document.hidden) render(); }, 1000);
  refresh();
})();
