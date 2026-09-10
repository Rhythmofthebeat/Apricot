(() => {
  if (window.communityResumeCountStarted) return;
  window.communityResumeCountStarted = true;
  let busy = false;
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const response = await fetch('/api/community-resume-count', {cache: 'no-store'});
      if (!response.ok) throw new Error('Count unavailable');
      const data = await response.json();
      if (!Number.isSafeInteger(data.total) || data.total < 0) throw new Error('Invalid count');
      document.querySelectorAll('[data-community-resume-total]').forEach(el => { el.textContent = data.total.toLocaleString('en-US'); });
      document.querySelectorAll('[data-community-count-status]').forEach(el => { el.textContent = 'Live total · Community Edition'; });
    } catch (_) {
      document.querySelectorAll('[data-community-count-status]').forEach(el => { el.textContent = 'Live total temporarily unavailable'; });
    } finally { busy = false; }
  }
  refresh();
  setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', refresh);
})();
