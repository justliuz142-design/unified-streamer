/**
 * LunarMediaDL — Downloader Page Controller
 * Three-step flow: URL input → metadata + options → progress (realtime via socket.io).
 * Talks to the MeTube-style Python backend through the API client in api.js.
 */
(function () {
  'use strict';

  if (!document.getElementById('appPanel')) return; // not on the downloader page

  // ── DOM refs ────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);

  const urlInput = $('urlInput');
  const fetchBtn = $('fetchBtn');
  const clearBtn = $('clearBtn');
  const pasteBtn = $('pasteBtn');
  const urlValidation = $('urlValidation');

  const playlistToggle = $('playlistToggle');
  const playlistAdvanced = $('playlistAdvanced');
  const playlistStart = $('playlistStart');
  const playlistEnd = $('playlistEnd');

  const videoThumb = $('videoThumb');
  const videoDuration = $('videoDuration');
  const videoTitle = $('videoTitle');
  const videoChannel = $('videoChannel');
  const videoViews = $('videoViews');
  const videoDate = $('videoDate');
  const videoDesc = $('videoDesc');
  const videoTypeBadge = $('videoTypeBadge');

  const qualitySelect = $('qualitySelect');
  const containerSelect = $('containerSelect');
  const formatList = $('formatList');

  const audioFormatSelect = $('audioFormatSelect');
  const audioQualitySelect = $('audioQualitySelect');

  const embedThumbVideo = $('embedThumbVideo');
  const embedMetaVideo = $('embedMetaVideo');
  const embedChapters = $('embedChapters');
  const embedThumbAudio = $('embedThumbAudio');
  const embedMetaAudio = $('embedMetaAudio');

  const writeSubtitles = $('writeSubtitles');
  const writeAutoSubs = $('writeAutoSubs');
  const embedSubs = $('embedSubs');
  const subLangInput = $('subLangInput');
  const subFormatSelect = $('subFormatSelect');

  const backBtn = $('backBtn');
  const downloadBtn = $('downloadBtn');
  const downloadBtnLabel = $('downloadBtnLabel');
  const downloadIconVideo = document.querySelector('.download-btn__icon--video');
  const downloadIconAudio = document.querySelector('.download-btn__icon--audio');

  const progressTitle = $('progressTitle');
  const progressFilename = $('progressFilename');
  const progressBarFill = $('progressBarFill');
  const progressPct = $('progressPct');
  const progressPctText = $('progressPctText');
  const progressRing = $('progressRing');
  const progressSpeed = $('progressSpeed');
  const progressEta = $('progressEta');
  const progressSize = $('progressSize');
  const cancelBtn = $('cancelBtn');
  const newDownloadBtn = $('newDownloadBtn');
  const downloadFileBtn = $('downloadFileBtn');

  // ── State ───────────────────────────────────────────────────
  const state = {
    info: null,
    mode: 'video', // 'video' | 'audio'
    activeJobId: null,
  };

  // Ring circumference for SVG progress (r=28 → 2πr ≈ 175.93)
  const RING_CIRCUMFERENCE = 2 * Math.PI * 28;
  if (progressRing) {
    progressRing.style.strokeDasharray = String(RING_CIRCUMFERENCE);
    progressRing.style.strokeDashoffset = String(RING_CIRCUMFERENCE);
  }

  // ── URL validation ──────────────────────────────────────────
  function isValidUrl(v) {
    try { new URL(v); return true; } catch { return false; }
  }

  function syncUrlState() {
    const v = urlInput.value.trim();
    const valid = isValidUrl(v);
    fetchBtn.disabled = !valid;
    clearBtn.classList.toggle('hidden', !v);
    urlValidation.textContent = v && !valid ? 'Please enter a valid URL' : '';
  }

  urlInput.addEventListener('input', syncUrlState);
  urlInput.addEventListener('paste', () => setTimeout(syncUrlState, 0));
  clearBtn.addEventListener('click', () => { urlInput.value = ''; syncUrlState(); urlInput.focus(); });
  pasteBtn.addEventListener('click', async () => {
    const text = await UI.pasteFromClipboard();
    if (text) { urlInput.value = text; syncUrlState(); }
    else UI.toast('Clipboard read blocked by browser', 'error');
  });

  // ── Tabs → mode tracking ────────────────────────────────────
  document.querySelectorAll('.options-tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      if (target === 'audio') setMode('audio');
      else if (target === 'video') setMode('video');
      // Advanced tab does not change mode.
    });
  });

  function setMode(mode) {
    state.mode = mode;
    if (downloadBtnLabel) downloadBtnLabel.textContent = mode === 'audio' ? 'Download Audio' : 'Download Video';
    if (downloadIconVideo) downloadIconVideo.style.display = mode === 'audio' ? 'none' : '';
    if (downloadIconAudio) downloadIconAudio.style.display = mode === 'audio' ? '' : 'none';
    if (downloadBtn) downloadBtn.dataset.mode = mode;
  }

  // ── Step 1 → Step 2: fetch metadata ────────────────────────
  fetchBtn.addEventListener('click', onFetchMetadata);
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !fetchBtn.disabled) onFetchMetadata(); });

  async function onFetchMetadata() {
    const url = urlInput.value.trim();
    if (!isValidUrl(url)) return;
    fetchBtn.setAttribute('aria-busy', 'true');
    fetchBtn.disabled = true;
    try {
      const info = await API.metadata(url);
      state.info = info;
      populateInfo(info);
      UI.showStep('stepInfo');
    } catch (err) {
      console.error(err);
      UI.toast(`Could not analyse: ${err.message}`, 'error', 5000);
    } finally {
      fetchBtn.setAttribute('aria-busy', 'false');
      fetchBtn.disabled = false;
    }
  }

  function populateInfo(info) {
    videoTitle.textContent = info.title || 'Untitled';
    videoChannel.textContent = info.uploader || '—';
    videoViews.textContent = info.view_count ? `${UI.formatNumber(info.view_count)} views` : '—';
    videoDate.textContent = info.upload_date ? UI.formatDate(info.upload_date) : '—';
    videoDesc.textContent = info.description || '';
    videoThumb.src = info.thumbnail || '';
    videoThumb.alt = info.title || '';
    videoDuration.textContent = info.duration ? UI.formatDuration(info.duration) : '';
    videoTypeBadge.textContent = info.is_playlist ? `Playlist · ${info.entries.length} items` : 'Video';

    populateQualityOptions(info.formats || []);
    populateFormatList(info.formats || []);
    playlistAdvanced.style.display = info.is_playlist ? '' : 'none';
  }

  function populateQualityOptions(formats) {
    const groups = { uhd: $('qualityGroupUHD'), hd: $('qualityGroupHD'), sd: $('qualityGroupSD') };
    Object.values(groups).forEach((g) => { if (g) g.innerHTML = ''; });
    const seenHeights = new Set();
    formats
      .filter((f) => f.height && f.vcodec && f.vcodec !== 'none')
      .sort((a, b) => (b.height || 0) - (a.height || 0))
      .forEach((f) => {
        const h = f.height;
        if (seenHeights.has(h)) return;
        seenHeights.add(h);
        const opt = document.createElement('option');
        opt.value = String(h);
        opt.textContent = `${h}p${f.fps && f.fps > 30 ? f.fps : ''} · ${f.ext || ''} ${f.filesize ? `· ${UI.formatSize(f.filesize)}` : ''}`.trim();
        const target = h >= 2160 ? groups.uhd : h >= 720 ? groups.hd : groups.sd;
        if (target) target.appendChild(opt);
      });
  }

  function populateFormatList(formats) {
    formatList.innerHTML = '';
    if (!formats.length) {
      formatList.innerHTML = '<div class="format-empty" style="padding:1rem;opacity:.7">No format details exposed.</div>';
      return;
    }
    formats.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'format-row';
      row.setAttribute('role', 'option');
      row.innerHTML = `
        <span class="format-row__id">${f.format_id}</span>
        <span class="format-row__res">${f.resolution || (f.acodec !== 'none' ? 'audio' : '—')}</span>
        <span class="format-row__ext">${f.ext || ''}</span>
        <span class="format-row__note">${f.format_note || ''}</span>
        <span class="format-row__size">${f.filesize ? UI.formatSize(f.filesize) : ''}</span>
      `;
      row.style.cssText = 'display:grid;grid-template-columns:60px 1fr 60px 1fr 80px;gap:.5rem;padding:.4rem .6rem;border-bottom:1px solid var(--clr-border,#222);font-size:.85rem;';
      formatList.appendChild(row);
    });
  }

  // ── Step 2 → Step 3: enqueue download ──────────────────────
  backBtn.addEventListener('click', () => UI.showStep('stepUrl'));

  downloadBtn.addEventListener('click', onStartDownload);

  function buildPayload() {
    const isAudio = state.mode === 'audio';
    const payload = {
      url: state.info.webpage_url || urlInput.value.trim(),
      download_type: isAudio ? 'audio' : 'video',
      quality: isAudio ? null : qualitySelect.value,
      container: isAudio ? null : containerSelect.value,
      audio_format: isAudio ? audioFormatSelect.value : null,
      audio_quality: isAudio ? audioQualitySelect.value : null,
      embed_thumb: isAudio ? embedThumbAudio.checked : embedThumbVideo.checked,
      embed_meta: isAudio ? embedMetaAudio.checked : embedMetaVideo.checked,
      embed_chapters: !isAudio && embedChapters.checked,
      write_subs: writeSubtitles.checked,
      write_auto_subs: writeAutoSubs.checked,
      embed_subs: embedSubs.checked,
      subtitle_langs: subLangInput.value || 'en',
      subtitle_format: subFormatSelect.value,
    };
    if (playlistToggle.checked && state.info?.is_playlist) {
      payload.playlist_start = Number(playlistStart.value) || undefined;
      payload.playlist_end = Number(playlistEnd.value) || undefined;
    } else {
      payload.noplaylist = true;
    }
    return payload;
  }

  async function onStartDownload() {
    if (!state.info) return;
    downloadBtn.disabled = true;
    try {
      const job = await API.download(buildPayload());
      state.activeJobId = job.id;
      resetProgressUI(job);
      UI.showStep('stepProgress');
      UI.toast('Download started', 'success');
    } catch (err) {
      UI.toast(`Failed to start: ${err.message}`, 'error', 5000);
    } finally {
      downloadBtn.disabled = false;
    }
  }

  function resetProgressUI(job) {
    progressTitle.textContent = 'Starting…';
    progressFilename.textContent = job.title || '';
    progressBarFill.style.width = '0%';
    progressPct.textContent = '0%';
    progressPctText.textContent = '0%';
    progressRing.style.strokeDashoffset = String(RING_CIRCUMFERENCE);
    progressSpeed.textContent = '—';
    progressEta.textContent = '—';
    progressSize.textContent = '—';
    downloadFileBtn.classList.add('hidden');
    cancelBtn.classList.remove('hidden');
  }

  function applyProgress(job) {
    if (job.id !== state.activeJobId) return;
    const pct = Math.max(0, Math.min(100, Number(job.progress) || 0));
    progressBarFill.style.width = `${pct.toFixed(1)}%`;
    progressPct.textContent = `${Math.round(pct)}%`;
    progressPctText.textContent = `${pct.toFixed(1)}%`;
    progressRing.style.strokeDashoffset = String(RING_CIRCUMFERENCE * (1 - pct / 100));
    progressTitle.textContent = job.status === 'downloading' ? 'Downloading…'
      : job.status === 'finished' ? 'Download complete'
      : job.status === 'error' ? 'Download failed'
      : job.status === 'cancelled' ? 'Cancelled'
      : 'Working…';
    progressSpeed.textContent = job.speed ? `${UI.formatSize(job.speed)}/s` : '—';
    progressEta.textContent = job.eta != null ? `ETA ${UI.formatDuration(job.eta)}` : '—';
    progressSize.textContent = job.total_bytes
      ? `${UI.formatSize(job.downloaded_bytes)} / ${UI.formatSize(job.total_bytes)}`
      : (job.downloaded_bytes ? UI.formatSize(job.downloaded_bytes) : '—');
    if (job.filename) progressFilename.textContent = job.filename;

    if (job.status === 'finished') {
      cancelBtn.classList.add('hidden');
      if (job.download_url) {
        downloadFileBtn.href = job.download_url;
        downloadFileBtn.classList.remove('hidden');
      }
      UI.toast('Download complete', 'success');
      Lunar.history.refresh();
    } else if (job.status === 'error') {
      cancelBtn.classList.add('hidden');
      UI.toast(`Download error: ${job.error || 'unknown'}`, 'error', 6000);
    } else if (job.status === 'cancelled') {
      cancelBtn.classList.add('hidden');
    }
  }

  cancelBtn.addEventListener('click', async () => {
    if (!state.activeJobId) return;
    try { await API.cancel(state.activeJobId); }
    catch (err) { UI.toast(`Cancel failed: ${err.message}`, 'error'); }
  });

  newDownloadBtn.addEventListener('click', () => {
    state.activeJobId = null;
    state.info = null;
    urlInput.value = '';
    syncUrlState();
    UI.showStep('stepUrl');
  });

  // ── Socket.IO realtime ─────────────────────────────────────
  const socket = API.socket();
  if (socket) {
    const handle = (job) => applyProgress(job);
    socket.on('added', handle);
    socket.on('progress', handle);
    socket.on('completed', handle);
    socket.on('cancelled', handle);
    socket.on('error', handle);
  }

  // Init
  syncUrlState();
  setMode('video');
})();
