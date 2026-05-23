/**
 * LunarMediaDL — Backend Client
 * Centralised REST + Socket.IO wrapper. Single source of truth for every
 * call into the Python backend so endpoints stay consistent across pages.
 */
const API = (function () {
  'use strict';

  const BASE = ''; // same origin
  let socket = null;

  function getSocket() {
    if (socket) return socket;
    if (typeof io === 'undefined') {
      console.warn('[API] socket.io client not loaded');
      return null;
    }
    socket = io(BASE, {
      path: '/socket.io',
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
    });
    socket.on('connect', () => console.info('[API] socket connected', socket.id));
    socket.on('disconnect', (reason) => console.warn('[API] socket disconnected', reason));
    socket.on('connect_error', (err) => console.warn('[API] socket error', err.message));
    return socket;
  }

  async function _request(path, options = {}) {
    const res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    if (!res.ok) {
      const msg = (data && data.error) || `HTTP ${res.status}`;
      const err = new Error(msg);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  return {
    metadata: (url) => _request(`/metadata?url=${encodeURIComponent(url)}`),
    download: (payload) => _request('/download', { method: 'POST', body: JSON.stringify(payload) }),
    progress: () => _request('/progress'),
    cancel: (id) => _request('/cancel', { method: 'POST', body: JSON.stringify({ id }) }),
    history: () => _request('/history'),
    clearHistory: () => _request('/history', { method: 'DELETE' }),
    cleanup: () => _request('/cleanup', { method: 'POST' }),
    socket: getSocket,
  };
})();
