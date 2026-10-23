'use strict';
(() => {
  const root = document.getElementById('power-budget');
  if (!root) return;
  const el = id => document.getElementById(id);
  const power = value => Number.isFinite(value) ? (value / 1000).toLocaleString('uk-UA') + ' кВт' : '—';
  const refresh = async () => {
    try {
      const response = await fetch(root.dataset.statusUrl, {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error();
      const data = await response.json();
      el('budget-enabled').textContent = data.enabled ? 'Автоматизацію увімкнено' : 'Автоматизацію вимкнено';
      el('budget-target').textContent = power(data.target_w);
      el('budget-actual').textContent = power(data.actual_w);
      el('budget-coverage').textContent = '(' + (data.known || 0) + '/' + (data.total || 0) + ' пристроїв)';
      el('budget-reason').textContent = data.reason;
      el('budget-updated').textContent = data.last_cycle_at ? 'Контролер: ' + new Date(data.last_cycle_at * 1000).toLocaleString('uk-UA') : 'Очікування контролера';
      el('budget-error').hidden = true;
    } catch (_) {
      el('budget-error').textContent = 'Не вдалося оновити стан автоматизації. Показані дані можуть бути застарілими.';
      el('budget-error').hidden = false;
    }
    window.setTimeout(refresh, 30000);
  };
  window.setTimeout(refresh, 30000);
})();
