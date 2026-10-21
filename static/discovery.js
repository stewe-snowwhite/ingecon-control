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
        rows.append(row);
      });
      document.getElementById('discovery-empty').hidden = data.results.length !== 0;
      repeat = ['queued','running','cancelling'].includes(data.state);
    } catch (failure) {
      error.hidden = false; error.textContent = failure.message || 'Втрачено зв’язок із вебпанеллю.';
    } finally { clearTimeout(timer); if (repeat && !stopped) setTimeout(refresh, 2500); }
  }
  window.addEventListener('pagehide', () => { stopped = true; });
  refresh();
})();
