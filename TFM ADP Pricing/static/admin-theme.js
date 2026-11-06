/* Runs before CSS so a saved dark theme never flashes a light background. */
(() => {
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let saved;
  try { saved = localStorage.getItem('tfm-admin-theme'); } catch (_) { /* Storage may be disabled. */ }
  document.documentElement.dataset.theme = saved === 'light' || saved === 'dark'
    ? saved : system.matches ? 'dark' : 'light';
  system.addEventListener('change', (event) => {
    let preference;
    try { preference = localStorage.getItem('tfm-admin-theme'); } catch (_) {}
    if (preference !== 'light' && preference !== 'dark') {
      document.documentElement.dataset.theme = event.matches ? 'dark' : 'light';
      window.dispatchEvent(new Event('admin-theme-change'));
    }
  });
})();
