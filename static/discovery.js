'use strict';
(() => {
  const all = document.getElementById('discovery-select-all');
  if (all) all.addEventListener('click', () => {
    const boxes = [...document.querySelectorAll('input[name="subnet_id"]')];
    const select = boxes.some(box => !box.checked);
    boxes.forEach(box => { box.checked = select; });
  });
  const container = document.getElementById('discovery-results');
  if (!container) return;
  const progress = document.getElementById('discovery-progress');
  const bar = document.getElementById('discovery-progress-bar');
  const rows = document.getElementById('discovery-rows');
  const error = document.getElementById('discovery-error');
  const states = {queued:'У черзі', running:'Пошук', cancelling:'Скасування', cancelled:'Скасовано', completed:'Завершено', failed:'Помилка'};
  let stopped = false;
  const bulk = document.getElementById('discovery-bulk-form');
  const selected = new Set();
  let candidates = [];
  let boxes = [];
  let available = false;
  function selection() {
    if (!bulk) return;
    document.getElementById('discovery-selection-count').textContent = 'Обрано: '+selected.size+' / 100';
    document.getElementById('discovery-bulk-submit').disabled = !available || selected.size === 0;
    document.getElementById('discovery-candidates-all').disabled = !available || candidates.length === 0;
    boxes.forEach(box => { box.checked = selected.has(box.value); });
  }
  if (bulk) {
    rows.addEventListener('change', event => {
      const box = event.target;
      if (box.name !== 'candidate_id') return;
      const warning = document.getElementById('discovery-bulk-warning');
      if (box.checked && selected.size >= 100 && !selected.has(box.value)) {
        box.checked = false;
        warning.hidden = false; warning.textContent = 'За один раз можна додати до 100 інверторів.';
      } else {
        if (box.checked) selected.add(box.value); else selected.delete(box.value);
        warning.hidden = true;
      }
      selection();
    });
    document.getElementById('discovery-candidates-all').addEventListener('click', () => {
      const first = candidates.slice(0,100).map(item => String(item.id));
      const clear = first.every(key => selected.has(key));
      selected.clear();
      if (!clear) first.forEach(key => selected.add(key));
      selection();
    });
  }
  const labels = ['IP / MAC', 'Підмережа', 'Modbus', 'Перевірка', 'Облік'];
  function cell(row, text) { const node = document.createElement('td'); node.dataset.label = labels[row.children.length]; node.textContent = text; row.append(node); return node; }
  async function refresh() {
    if (stopped) return;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 7000);
    let repeat = true;
    try {
      const response = await fetch(container.dataset.url, {credentials:'same-origin', headers:{Accept:'application/json'}, signal:controller.signal});
      if (response.status === 401 || response.status === 403) { stopped = true; throw new Error('Увійдіть знову як адміністратор.'); }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не вдалося прочитати результати.');
      available = true;
      candidates = data.results.filter(item => !item.registered);
      const eligible = new Set(candidates.map(item => String(item.id)));
      for (const key of selected) if (!eligible.has(key)) selected.delete(key);
      boxes = [];
      progress.textContent = (states[data.state] || data.state) + ': перевірено '+data.processed+' / '+data.total+', знайдено '+data.found;
      bar.max = Math.max(1, data.total); bar.value = data.processed;
      error.hidden = !(data.error || data.stale);
      error.textContent = data.error || (data.stale ? 'Worker не відповідає або ще не запущений. Перевірки могли зупинитися.' : '');
      rows.replaceChildren();
      data.results.forEach(item => {
        const row = document.createElement('tr');
        cell(row, item.ip+' / '+(item.mac || 'MAC недоступна'));
        cell(row, item.cidr);
        cell(row, item.modbus_ok ? 'FC04 підтверджено' : (item.tcp_ok ? 'TCP 502 відкритий; FC04 не підтверджено' : 'TCP 502 недоступний'));
        cell(row, (item.confidence === 'profile' ? 'Відповідає очікуваному профілю. ' : 'Можливий інвертор. ')+item.reason);
        const action = cell(row, item.registered ? 'Вже доданий' : 'Новий ');
        if (!item.registered) { const link = document.createElement('a'); link.className = 'button'; link.href = item.add_url; link.textContent = 'Додати'; action.append(link); }
        if (bulk && !item.registered) {
          const label = document.createElement('label');
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox'; checkbox.name = 'candidate_id'; checkbox.value = String(item.id);
          checkbox.checked = selected.has(checkbox.value);
          checkbox.ariaLabel = 'Обрати '+item.ip;
          label.append(checkbox, ' Обрати'); action.append(label); boxes.push(checkbox);
        }
        rows.append(row);
      });
      document.getElementById('discovery-empty').hidden = data.results.length !== 0;
      selection();
      repeat = ['queued','running','cancelling'].includes(data.state);
    } catch (failure) {
      available = false; selection();
      error.hidden = false; error.textContent = failure.message || 'Втрачено зв’язок із вебпанеллю.';
    } finally { clearTimeout(timer); if (repeat && !stopped) setTimeout(refresh, 2500); }
  }
  window.addEventListener('pagehide', () => { stopped = true; });
  refresh();
})();
