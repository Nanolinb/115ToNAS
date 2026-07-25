/* 公共工具：观影端与管理端共用 */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

async function api(path, opts = {}) {
  if (opts.body && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  }
  const r = await fetch(path, opts);
  if (r.status === 401) {
    const err = new Error('unauthorized');
    err.unauthorized = true;
    throw err;
  }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 2200) {
  const el = document.createElement('div');
  el.className = 'toast'; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(2) + ' GB';
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB';
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(0) + ' KB';
  return n + ' B';
}
function fmtSpeed(n) { return n > 0 ? fmtSize(n) + '/s' : ''; }

function posterUrl(fname) {
  return fname ? `/api/poster/${encodeURIComponent(fname)}` : '/api/poster/_none';
}

function bindModalClosers(root) {
  (root || document).querySelectorAll('[data-close]').forEach((b) =>
    b.addEventListener('click', () => $('#' + b.dataset.close).classList.add('hidden')));
}

/* ---------- 播放器（观影端用） ---------- */

async function playMedia(id) {
  const d = await api(`/api/media/${id}`);
  const video = $('#playerVideo');
  video.innerHTML = '';
  video.src = `/api/stream/${id}`;
  // 多语言字幕轨道：中文 / English / 中英双语，浏览器原生 CC 菜单切换
  (d.subs || []).forEach((t, i) => {
    const track = document.createElement('track');
    track.kind = 'subtitles';
    track.label = t.label || t.lang || `字幕 ${i + 1}`;
    track.srclang = t.lang === 'en' ? 'en' : 'zh';
    track.src = `/api/subtitle/${id}/${i}`;
    if (i === 0) track.default = true;
    video.appendChild(track);
  });
  $('#playerName').textContent = d.filename;
  $('#playerMask').classList.remove('hidden');
  setupAudioMenu(id, video);
  // 安卓 TV App 内（原生桥存在时）：提供「外部播放器」通道，
  // 由电视/投影仪自己的播放器硬解，支持内嵌多音轨/字幕切换
  const extBtn = $('#btnExternal');
  if (window.MediaHubNative && typeof MediaHubNative.play === 'function') {
    extBtn.classList.remove('hidden');
    extBtn.onclick = () => {
      MediaHubNative.play(`${location.origin}/api/stream/${id}`, d.filename);
    };
  } else {
    extBtn.classList.add('hidden');
  }
  $('#btnCopyLink').onclick = async () => {
    const url = `${location.origin}/api/stream/${id}`;
    try { await navigator.clipboard.writeText(url); toast('直链已复制（可粘贴到极米/Infuse 等播放器）'); }
    catch (e) { prompt('复制此直链:', url); }
  };
  $('#btnDownloadFile').onclick = () => { window.open(`/api/stream/${id}`, '_blank'); };
  video.play().catch(() => {});
}

function closePlayer() {
  const v = $('#playerVideo');
  v.pause(); v.removeAttribute('src'); v.load();
  $('#playerMask').classList.add('hidden');
}

/* ---------- 音轨菜单 ---------- */

async function setupAudioMenu(id, video) {
  const sel = $('#audioTrackSel');
  sel.classList.remove('hidden');
  sel.disabled = true;
  sel.innerHTML = '<option>检测音轨…</option>';
  let info = { available: false, audio: 0 };
  try { info = await api(`/api/media/${id}/tracks`); } catch (e) {}
  const n = info.audio || 0;
  // 浏览器里只有 Safari 实现了 audioTracks；Chrome/WebView 不支持网页内切音轨
  const canSwitch = !!video.audioTracks && n > 1;
  if (canSwitch) {
    const at = video.audioTracks;
    sel.disabled = false;
    sel.innerHTML = Array.from({ length: n }, (_, i) =>
      `<option value="${i}">音轨 ${i + 1}${at[i] && at[i].label ? ' · ' + esc(at[i].label) : ''}</option>`
    ).join('');
    sel.onchange = () => {
      for (let i = 0; i < at.length; i++) at[i].enabled = (i === +sel.value);
    };
    sel.title = '切换内嵌音轨';
  } else {
    sel.innerHTML = `<option>${n > 1 ? n + ' 条音轨' : '单音轨'}</option>`;
    sel.title = n > 1
      ? '当前浏览器不支持网页内切换音轨（仅 Safari 支持）；请用「电视播放器」或复制直链到 IINA / Infuse / VLC 切换'
      : '该视频只有一条音轨';
  }
}
