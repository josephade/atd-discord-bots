/* Apply the browser preference before painting the voting page. */
(() => {
  const system = matchMedia('(prefers-color-scheme: dark)');
  function apply() {
    let saved;
    try { saved = localStorage.getItem('tfm-vote-theme'); } catch (_) {}
    document.documentElement.dataset.theme = saved === 'light' || saved === 'dark'
      ? saved : system.matches ? 'dark' : 'light';
    window.dispatchEvent(new Event('vote-theme-change'));
  }
  system.addEventListener('change', apply);
  apply();
})();
