/* Apply saved or system preference before the page is painted. */
(() => {
  let saved;
  try { saved = localStorage.getItem('ingecon-theme'); } catch (_) { /* Private/disabled storage. */ }
  const systemDark = typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = ['light', 'dark'].includes(saved) ? saved : systemDark ? 'dark' : 'light';
})();
