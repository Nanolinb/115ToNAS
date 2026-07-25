/* 观影端：海报墙 + 播放。默认免密；管理端设了观影密码时才需登录。 */
'use strict';

const state = { library: [], adminAuthed: false };

/* ---------- 观影登录 ---------- */

async function checkViewerAuth() {
  const st = await api('/api/auth/viewer_status');
  if (st.need_password && !st.authed) {
    $('#loginMask').classList.remove('hidden');
    $('#loginPw').focus();
    return false;
  }
  return true;
}

async function doViewerLogin() {
  try {
    await api('/api/auth/viewer_login', { method: 'POST', body: { password: $('#loginPw').value } });
    $('#loginMask').classList.add('hidden');
    $('#loginPw').value = ''; $('#loginErr').textContent = '';
    boot();
  } catch (e) {
    $('#loginErr').textContent = e.message;
  }
}

/* ---------- 媒体库 ---------- */

async function loadLibrary() {
  const params = new URLSearchParams({
    q: $('#libSearch').value, type: $('#libType').value,
    year: $('#libYear').value, genre: $('#libGenre').value,
  });
  const data = await api(`/api/library?${params}`);
  state.library = data.items;
  renderLibrary();
  fillFilters(data.items);
}

function fillFilters(items) {
  const genres = new Set(), years = new Set();
  items.forEach((e) => {
    (e.genres || '').split(',').filter(Boolean).forEach((g) => genres.add(g));
    if (e.year) years.add(e.year);
  });
  const gsel = $('#libGenre'), gval = gsel.value;
  gsel.innerHTML = '<option value="">全部题材</option>' +
    [...genres].sort().map((g) => `<option ${g === gval ? 'selected' : ''}>${esc(g)}</option>`).join('');
  const ysel = $('#libYear'), yval = ysel.value;
  ysel.innerHTML = '<option value="0">全部年份</option>' +
    [...years].sort((a, b) => b - a).map((y) =>
      `<option value="${y}" ${String(y) === yval ? 'selected' : ''}>${y}</option>`).join('');
}

function renderLibrary() {
  const grid = $('#libGrid');
  $('#libEmpty').classList.toggle('hidden', state.library.length > 0);
  grid.innerHTML = state.library.map((e) => {
    const subBadge = e.kind === 'movie' && e.has_sub
      ? '<span class="badge sub">字幕</span>' : '';
    const countBadge = e.kind === 'show' ? `<span class="badge">${e.count}集</span>` : '';
    return `<div class="card" data-key="${esc(e.key)}" tabindex="0" role="button"
      aria-label="${esc(e.name_cn || e.title)}">
      <div class="poster-wrap">
        <img loading="lazy" src="${posterUrl(e.poster)}" alt="">
        ${countBadge}${subBadge}
      </div>
      <div class="title">${esc(e.name_cn || e.title)}</div>
      <div class="sub-title">${e.year || ''} ${e.rating ? '· ★' + e.rating : ''}</div>
    </div>`;
  }).join('');
  grid.querySelectorAll('.card').forEach((c) => {
    c.addEventListener('click', () => openDetail(c.dataset.key));
    // 遥控器确认键（Enter / 空格）打开详情
    c.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openDetail(c.dataset.key); }
    });
  });
}

async function openDetail(key) {
  const e = state.library.find((x) => x.key === key);
  if (!e) return;
  const modal = $('#detailModal');
  const heroImg = e.poster ? `<img src="${posterUrl(e.poster)}">` : '';
  // 管理端已登录时才显示维护按钮（电视上不会出现）
  const adminBtns = state.adminAuthed ? `
    ${e.kind === 'movie' ? `<button class="btn" data-subs="${e.id}">搜索字幕</button>` : ''}
    <button class="btn" data-rematch="${e.kind === 'movie' ? e.id : (e.episodes[0] && e.episodes[0].id)}">重新匹配信息</button>` : '';
  let html = `
    <div class="detail-hero">${heroImg}<div class="fade"></div></div>
    <div class="detail-flex">
      <img class="dposter" src="${posterUrl(e.poster)}">
      <div class="detail-meta">
        <h2>${esc(e.name_cn || e.title)}</h2>
        <div class="tags">${esc([e.year, e.genres, e.rating ? '★ ' + e.rating : '']
          .filter(Boolean).join(' · '))}</div>
        <div class="overview">${esc(e.overview || '暂无简介')}</div>
        <div class="detail-actions">
          ${e.kind === 'movie' ? `<button class="btn primary" data-play="${e.id}">▶ 播放</button>` : ''}
          ${adminBtns}
        </div>
      </div>
    </div>`;
  if (e.kind === 'show') {
    html += `<div style="margin-top:18px">` + e.episodes.map((ep) => `
      <div class="ep-row">
        <span class="ep-label">S${String(ep.season).padStart(2, '0')}E${String(ep.episode || '?').padStart(2, '0')}</span>
        <span class="ep-name">${esc(ep.name)}</span>
        <span class="dot ${ep.has_sub ? 'g' : (ep.sub_status === 'failed' ? 'r' : 'y')}"
              title="${ep.has_sub ? '有字幕' : (ep.sub_status === 'failed' ? '字幕未找到' : '无字幕')}"></span>
        <span class="fsize">${fmtSize(ep.size)}</span>
        <button class="btn small primary" data-play="${ep.id}">▶</button>
        ${state.adminAuthed ? `<button class="btn small" data-subs="${ep.id}">字幕</button>` : ''}
      </div>`).join('') + `</div>`;
  }
  modal.innerHTML = `<div class="mhead"><h3></h3><button class="mclose" data-close="detailMask">✕</button></div>` + html;
  $('#detailMask').classList.remove('hidden');

  modal.querySelectorAll('[data-play]').forEach((b) =>
    b.addEventListener('click', () => playMedia(+b.dataset.play)));
  modal.querySelectorAll('[data-subs]').forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = '搜索中…';
      try {
        const r = await api(`/api/media/${b.dataset.subs}/subtitle`, { method: 'POST' });
        toast(r.found ? '字幕下载成功' : '没有找到匹配字幕');
      } catch (err) { toast(err.message); }
      b.disabled = false; b.textContent = '字幕';
    }));
  modal.querySelectorAll('[data-rematch]').forEach((b) =>
    b.addEventListener('click', () => openRematch(+b.dataset.rematch)));
  bindModalClosers(modal);
}

/* ---------- 手动匹配（仅管理端会话可见） ---------- */

async function openRematch(mediaId) {
  const kw = prompt('输入用于 TMDB 搜索的片名（外文原名更准）:');
  if (!kw) return;
  let cands;
  try {
    cands = (await api(`/api/media/${mediaId}/tmdb_candidates?q=${encodeURIComponent(kw)}`)).items;
  } catch (e) { toast(e.message); return; }
  if (!cands.length) { toast('没有搜索结果（确认已配置 TMDB Key）'); return; }
  const modal = $('#detailModal');
  modal.innerHTML = `<div class="mhead"><h3>选择正确的影片</h3><button class="mclose" data-close="detailMask">✕</button></div>
    <div class="mbody">` + cands.map((c) => `
    <div class="ep-row" style="border:1px solid var(--border);border-radius:8px;margin-bottom:8px">
      ${c.poster_url ? `<img src="${c.poster_url}" style="width:40px;border-radius:4px">` : '<span style="width:40px"></span>'}
      <div style="flex:1">
        <div>${esc(c.title)} <span style="color:var(--muted)">${esc(c.original_title || '')}</span></div>
        <div style="color:var(--muted);font-size:12px">${c.year || ''} · ★${c.rating}</div>
      </div>
      <button class="btn small primary" data-pick="${c.tmdb_id}">匹配</button>
    </div>`).join('') + `</div>`;
  modal.querySelectorAll('[data-pick]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        await api(`/api/media/${mediaId}/rematch`, { method: 'POST', body: { tmdb_id: +b.dataset.pick } });
        toast('匹配成功');
        $('#detailMask').classList.add('hidden');
        loadLibrary();
      } catch (e) { toast(e.message); }
    }));
  bindModalClosers(modal);
}

/* ---------- 启动 ---------- */

async function boot() {
  try {
    const st = await api('/api/auth/status');
    state.adminAuthed = st.authed;
  } catch (e) { state.adminAuthed = false; }
  loadLibrary().catch((e) => {
    if (e.unauthorized) { $('#loginMask').classList.remove('hidden'); }
  });
}

document.addEventListener('DOMContentLoaded', async () => {
  let searchTimer;
  $('#libSearch').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadLibrary, 350);
  });
  ['libType', 'libGenre', 'libYear'].forEach((id) =>
    $('#' + id).addEventListener('change', loadLibrary));
  $('#btnLogin').addEventListener('click', doViewerLogin);
  $('#loginPw').addEventListener('keydown', (e) => { if (e.key === 'Enter') doViewerLogin(); });
  $('#btnClosePlayer').addEventListener('click', closePlayer);
  $('#playerMask').addEventListener('click', (e) => { if (e.target.id === 'playerMask') closePlayer(); });
  $('#detailMask').addEventListener('click', (e) => { if (e.target.id === 'detailMask') e.target.classList.add('hidden'); });

  if (await checkViewerAuth()) boot();
});
