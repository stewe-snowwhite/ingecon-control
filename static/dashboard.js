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
  function counts(rows) {
    const result = {total: rows.length, Online: 0, Offline: 0, Degraded: 0, Unknown: 0};
    rows.forEach(row => result[STATES.includes(row.state) ? row.state : 'Unknown']++);
    return result;
  }
  function formatPower(watts) {
    return Number.isFinite(watts) ? (watts / 1000).toFixed(2).replace('.', ',') + ' кВт' : '—';
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
  const model = {formatUtc, formatPower, formatLimitReason, filterRows, counts, freshSnapshot, validSnapshot};
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
  const inputs = {q: el('search'), group: el('group-filter'), state: el('state-filter')};
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
    const current = freshSnapshot(snapshot, snapshot.generated_at + (performance.now() - receivedAt) / 1000);
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
    const rows = filterRows(current.inverters, readFilters());
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
      if (!response.ok) throw new Error('Сервер не повернув статуси. Повторна спроба через 10 секунд.');
      const data = await response.json();
      if (!validSnapshot(data)) throw new Error('Некоректна відповідь API. Останній знімок може бути застарілим.');
      snapshot = data; receivedAt = performance.now(); panel.connected = true;
      updateGroups(); render(); el('api-warning').hidden = true;
      el('refresh-label').textContent = 'Оновлено · ' + formatUtc(data.generated_at) + ' UTC';
    } catch (error) {
      panel.connected = false;
      el('api-warning').textContent = error instanceof SyntaxError ?
        'Сервер повернув некоректну відповідь. Показано останній знімок.' :
        error.name === 'AbortError' || error instanceof TypeError ?
        'Втрачено зв’язок із сервером. Показано останній знімок; перевіряємо знову через 10 секунд.' : error.message;
      el('api-warning').hidden = false; el('refresh-label').textContent = 'Автооновлення недоступне';
      if (snapshot) render(); else markInitialUnknown();
    } finally {
      clearTimeout(timeout); fetching = false;
      if (!stopped && !document.hidden) {
        timer = setTimeout(refresh, refreshRequested ? 0 : 10000); refreshRequested = false;
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
