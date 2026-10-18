'use strict';
(() => {
  const root = document.getElementById('monitor-demo');
  if (!root) return;
  const alert = document.getElementById('page-alert');
  const stamp = value => value == null ? '—' : new Date(value * 1000).toISOString().replace('T', ' ').slice(0, 19);
  const set = (key, value) => { const node = root.querySelector(`[data-demo="${key}"]`); if (node) node.textContent = value; };
  const flag = value => value === true ? 'доступний' : value === false ? 'недоступний' : 'невідомо';
  function rows(id, entries, labels) {
    const body = document.getElementById(id);
    body.replaceChildren();
    for (const entry of entries) {
      const row = document.createElement('tr');
      entry.forEach((value, index) => {
        const cell = document.createElement('td'); cell.dataset.label = labels[index];
        cell.textContent = value ?? '—'; row.appendChild(cell);
      });
      body.appendChild(row);
    }
    if (!entries.length) {
      const row = document.createElement('tr'), cell = document.createElement('td');
      cell.colSpan = labels.length; cell.textContent = 'Записів немає.'; row.appendChild(cell); body.appendChild(row);
    }
  }
  let stopped = false;
  async function poll() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(root.dataset.api, {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
      if (response.status === 401 || response.status === 403) { stopped = true; throw new Error('Доступ до демо втрачено. Увійдіть знову.'); }
      if (!response.ok) throw new Error('Не вдалося оновити демо. Перевірте запуск монітора.');
      const data = await response.json(), inv = data.inverters[0];
      set('state', inv.state); set('equipment', data.equipment);
      set('heartbeat', data.monitor.alive ? 'активний' : 'дані застарілі / монітор зупинено');
      set('tcp', flag(inv.checks.tcp.ok)); set('modbus', flag(inv.checks.modbus.ok)); set('checked', stamp(inv.checked_at));
      set('notifications', data.notifications ? 'увімкнено' : 'вимкнено');
      for (const key of ['power_w','voltage_v','current_a','frequency_hz','temperature_c']) set(key, data.measurements?.[key] ?? '—');
      rows('demo-events', data.events.map(e => [stamp(e.time),e.kind,e.state,e.detail]), ['Час','Тип','Стан','Подія']);
      rows('demo-deliveries', data.deliveries.map(d => [d.event_id,d.state,d.error]), ['Подія','Результат','Причина']);
      alert.hidden = true;
    } catch (error) {
      // Never leave cached live measurements looking current after a web outage.
      set('state','Unknown'); set('equipment','Unknown'); set('heartbeat','оновлення недоступне');
      set('tcp','невідомо'); set('modbus','невідомо');
      for (const key of ['power_w','voltage_v','current_a','frequency_hz','temperature_c']) set(key,'—');
      alert.textContent = stopped ? 'Доступ до демо втрачено. Увійдіть знову.' : 'Оновлення демо недоступне. Дані можуть бути застарілими.';
      alert.hidden = false;
    } finally { clearTimeout(timeout); if (!stopped) setTimeout(poll, 5000); }
  }
  poll();
})();
