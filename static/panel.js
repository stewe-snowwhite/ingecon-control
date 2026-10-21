'use strict';
(() => {
  const themeButton = document.getElementById('theme-toggle');
  const updateThemeLabel = () => {
    const dark = document.documentElement.dataset.theme === 'dark';
    themeButton.textContent = dark ? 'Світла тема' : 'Темна тема';
    themeButton.setAttribute('aria-pressed', String(dark));
  };
  if (themeButton) {
    updateThemeLabel();
    themeButton.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('ingecon-theme', next); } catch (_) { /* Theme still works. */ }
      updateThemeLabel();
    });
  }

  const dialog = document.getElementById('command-dialog');
  const pageAlert = document.getElementById('page-alert');
  const state = window.IngeconPanel = {busy: false, connected: true};
  let pending = null;
  let originatingForm = null;
  const alert = message => { pageAlert.textContent = message; pageAlert.hidden = false; };
  const closePreview = () => { pending = null; state.busy = false; if (dialog.open) dialog.close(); };
  if (dialog) {
    document.getElementById('cancel-command').addEventListener('click', closePreview);
    dialog.addEventListener('cancel', closePreview);
  }

  async function send(url, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, {method: 'POST', body: data, credentials: 'same-origin',
        headers: {'Accept': 'application/json'}, signal: controller.signal});
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 401) state.connected = false;
        throw new Error(result.error || 'Не вдалося виконати запит.');
      }
      return result;
    } finally { clearTimeout(timer); }
  }

  function showResults(results) {
    if (!Array.isArray(results)) throw new Error('Некоректна відповідь сервера.');
    const list = document.getElementById('result-list');
    list.replaceChildren();
    results.forEach(result => {
      const item = document.createElement('li');
      item.className = result.outcome === 'unconfirmed' ? 'unconfirmed' : '';
      item.textContent = result.name + ': ' + result.message;
      list.append(item);
    });
    document.getElementById('command-results').hidden = false;
    document.dispatchEvent(new Event('commandcomplete'));
    if (!document.getElementById('dashboard') && results.some(row => row.outcome === 'deleted')) {
      document.querySelectorAll('form').forEach(form => {
        if (form.action.includes('/edit/') || form.action.includes('/delete/')) form.hidden = true;
      });
    }
  }

  document.querySelectorAll('form[data-command]').forEach(form => {
    // Older browsers fall back to the server-rendered confirmation page.
    if (!dialog || typeof dialog.showModal !== 'function' || typeof fetch !== 'function') return;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (state.busy) return;
      if (!state.connected) { alert('Немає актуального зв’язку із сервером. Дочекайтеся відновлення перед командою.'); return; }
      const data = new FormData(form);
      if (event.submitter && event.submitter.name) data.append(event.submitter.name, event.submitter.value);
      if (form.id === 'control-form' && !data.getAll('selected_ids').length) {
        alert('Оберіть хоча б один інвертор.'); return;
      }
      state.busy = true;
      originatingForm = form;
      pageAlert.hidden = true;
      try {
        const result = await send(form.action, data);
        if (result.confirmation_required) {
          pending = {url: form.action, data};
          data.set('confirmation_token', result.confirmation_token);
          document.getElementById('confirmation-title').textContent = result.label;
          document.getElementById('confirmation-description').textContent =
            result.action === 'delete' ? 'Інвертори буде видалено зі списку; історія залишиться.' :
            result.label + (result.action === 'limit' ? ': ' + result.value + '%' : '') +
            '. Після підтвердження буде виконано реальний Modbus-запис. Сумісність прошивки не підтверджена. Запис регістра не підтверджує фізичний стан.';
          const targets = document.getElementById('confirmation-targets');
          targets.replaceChildren();
          result.inverters.forEach(row => {
            const item = document.createElement('li');
            item.textContent = row.name + ' · ' + row.ip_address;
            if (row.command) item.textContent += ' · TCP ' + row.command.port + ', Unit ID ' + row.command.device_id +
              ', FC' + row.command.function + ', HR' + row.command.address + ', слова [' + row.command.values.join(', ') + ']';
            targets.append(item);
          });
          dialog.showModal();
        } else { showResults(result.results); state.busy = false; }
      } catch (error) {
        state.busy = false; pending = null;
        alert(error.name === 'AbortError' || error instanceof TypeError || error instanceof SyntaxError ?
          'Зв’язок втрачено або перевищено час відповіді. Результат невідомий; не повторюйте команду автоматично.' : error.message);
      }
    });
  });

  if (dialog) document.getElementById('confirm-command').addEventListener('click', async () => {
    if (!pending) return;
    if (!state.connected) { closePreview(); alert('Зв’язок втрачено. Після відновлення перевірте й підтвердьте дію заново.'); return; }
    const job = pending;
    pending = null; // A double click cannot submit a second command.
    const button = document.getElementById('confirm-command');
    button.disabled = true;
    try {
      const result = await send(job.url, job.data);
      dialog.close();
      showResults(result.results);
    } catch (error) {
      dialog.close();
      alert(error.name === 'AbortError' || error instanceof TypeError || error instanceof SyntaxError ?
        'Зв’язок втрачено. Результат команди невідомий; не повторюйте її без перевірки.' : error.message);
    } finally {
      button.disabled = false; state.busy = false;
      if (originatingForm) originatingForm.dispatchEvent(new Event('commandfinished'));
    }
  });
})();
