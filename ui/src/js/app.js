/**
 * LunarMediaDL — App bootstrap
 * Shared init: UI helpers, history panel, global toasts, starfield.
 */
const Lunar = (function () {
  'use strict';

  const historyToggleBtn = document.getElementById('historyToggleBtn');
  const historyPanel = document.getElementById('historyPanel');
  const historyBackdrop = document.getElementById('historyBackdrop');
  const historyCloseBtn = document.getElementById('historyCloseBtn');
  const historyList = document.getElementById('historyList');
  const historyEmpty = document.getElementById('historyEmpty');
  const clearHistoryBtn = document.getElementById('clearHistoryBtn');

  function openHistory() {
    if (!historyPanel) return;
    historyPanel.classList.add('open');
    historyPanel.setAttribute('aria-hidden', 'false');
    refreshHistory();
  }

  function closeHistory() {
    if (!historyPanel) return;
    historyPanel.classList.remove('open');
    historyPanel.setAttribute('aria-hidden', 'true');
  }

  async function refreshHistory() {
    if (!historyList) return;
    try {
      const items = await API.history();
      render(items);
    } catch (err) {
      console.warn('history fetch failed', err);
    }
  }

  function render(items) {
    if (!historyList) return;
    const existing = historyList.querySelectorAll('.history-item');
    existing.forEach((el) => el.remove());
    if (!items.length) {
      if (historyEmpty) historyEmpty.style.display = '';
      return;
    }
    if (historyEmpty) historyEmpty.style.display = 'none';
    items.forEach((j) => {
      const row = document.createElement('div');
      row.className = 'history-item';
      row.style.cssText = 'padding:.75rem;border-bottom:1px solid var(--clr-border,#222);display:flex;flex-direction:column;gap:.25rem';
      const statusColor = j.status === 'finished' ? '#34d399' : j.status === 'error' ? '#f87171' : '#fbbf24';
      const link = j.download_url
        ? `<a href="${j.download_url}" download style="color:var(--clr-accent,#60a5fa);font-size:.85rem">Save file</a>`
        : '';
      const date = j.finished_at ? new Date(j.finished_at * 1000).toLocaleString() : '';
      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;gap:.5rem;align-items:start">
          <strong style="font-size:.9rem;line-height:1.3;flex:1">${escapeHtml(j.title || j.url)}</strong>
          <span style="color:${statusColor};font-size:.75rem;text-transform:uppercase">${j.status}</span>
        </div>
        <div style="display:flex;justify-content:space-between;gap:.5rem;font-size:.78rem;opacity:.7">
          <span>${date}</span>
          ${link}
        </div>
      `;
      historyList.appendChild(row);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  if (historyToggleBtn) historyToggleBtn.addEventListener('click', openHistory);
  if (historyCloseBtn) historyCloseBtn.addEventListener('click', closeHistory);
  if (historyBackdrop) historyBackdrop.addEventListener('click', closeHistory);
  if (clearHistoryBtn) {
    clearHistoryBtn.addEventListener('click', async () => {
      try { await API.clearHistory(); refreshHistory(); UI.toast('History cleared', 'success'); }
      catch (err) { UI.toast(`Clear failed: ${err.message}`, 'error'); }
    });
  }

  document.addEventListener('DOMContentLoaded', () => UI.init());

  // Live history refresh on completion is wired by downloader.js via Lunar.history.
  return { history: { refresh: refreshHistory, open: openHistory, close: closeHistory } };
})();
