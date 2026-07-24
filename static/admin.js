/* 管理端：115 登录/浏览/下载队列/设置。独立密码通道。 */
'use strict';

const state = {
  tab: 'cloud',
  settings: {},
  cloud: { cid: '0', path: [], items: [], selected: new Map() },
  taskTimer: null,
  qrUid: null,
  qrTimer: null,
  dirPickTarget: null,
  dirPath: '',
};

/* ---------- 管理端登录 ---------- */

function showLogin() {
  $('#loginMask').classList.remove('hidden');
  $('#loginPw').focus();
}

async function checkAuth() {
  const st = await api('/api/auth/status');
  if (!st.configured) $('#loginHint').textContent = '首次使用，请设置管理密码（至少 4 位）';
  $('#viewerPwState').textContent = st.viewer_password ? '当前：已设置观影密码' : '当前：观影页免密';
  if (!st.authed) { showLogin(); return false; }
  return true;
}

async function doLogin() {
  const pw = $('#loginPw').value;
  try {
    await api('/api/auth/login', { method: 'POST', body: { password: pw } });
    $('#loginMask').classList.add('hidden');
    $('#loginPw').value = ''; $('#loginErr').textContent = '';
    boot();
  } catch (e) {
    if (!e.unauthorized) $('#loginErr').textContent = e.message;
  }
}

/* ---------- 页面切换 ---------- */

function switchTab(tab) {
  state.tab = tab;
  $$('.tab[data-tab]').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
  ['cloud', 'tasks', 'settings'].forEach((p) =>
    $(`#page-${p}`).classList.toggle('hidden', p !== tab));
  clearInterval(state.taskTimer); state.taskTimer = null;
  if (tab === 'cloud') initCloud();
  if (tab === 'tasks') { loadTasks(); state.taskTimer = setInterval(loadTasks, 2000); }
  if (tab === 'settings') loadSettings();
}

/* ---------- 115 网盘 ---------- */

async function initCloud() {
  const st = await api('/api/cloud/status');
  $('#cloudLogin').classList.toggle('hidden', st.logged_in);
  $('#cloudBrowser').classList.toggle('hidden', !st.logged_in);
  if (st.logged_in) {
    fillCloudTargets();
    if (!state.cloud.items.length) loadCloudList('0');
  }
}

function fillCloudTargets() {
  const s = state.settings;
  const sel = $('#cloudTarget');
  const opts = [
    ['下载目录', s.download_dir], ['电影目录', s.movie_dir], ['剧集目录', s.tv_dir],
  ].filter(([, v]) => v);
  sel.innerHTML = opts.map(([label, v]) =>
    `<option value="${esc(v)}">下载到：${label}</option>`).join('');
}

async function startQr() {
  $('#btnQr').disabled = true;
  $('#qrStatus').textContent = '正在获取二维码…';
  try {
    const { uid } = await api('/api/cloud/qrcode/new', { method: 'POST' });
    state.qrUid = uid;
    const img = $('#qrImg');
    img.src = `/api/cloud/qrcode/${uid}.png?t=${Date.now()}`;
    img.classList.remove('hidden');
    $('#qrStatus').textContent = '请用手机 115 App 扫码';
    clearInterval(state.qrTimer);
    state.qrTimer = setInterval(pollQr, 2000);
  } catch (e) { $('#qrStatus').textContent = e.message; }
  $('#btnQr').disabled = false;
}

async function pollQr() {
  if (!state.qrUid) return;
  try {
    const st = await api(`/api/cloud/qrcode/${state.qrUid}/status`);
    if (st.status === 'scanned') $('#qrStatus').textContent = '已扫码，请在手机上确认登录';
    if (st.status === 'done') {
      clearInterval(state.qrTimer);
      $('#qrStatus').textContent = '登录成功！';
      toast('115 登录成功');
      state.cloud = { cid: '0', path: [], items: [], selected: new Map() };
      setTimeout(initCloud, 600);
    }
    if (st.status === 'expired') {
      clearInterval(state.qrTimer);
      $('#qrStatus').textContent = '二维码已过期，请重新获取';
      $('#qrImg').classList.add('hidden');
    }
  } catch (e) { /* 网络抖动忽略 */ }
}

async function loadCloudList(cid) {
  const data = await api(`/api/cloud/list?cid=${encodeURIComponent(cid)}`);
  state.cloud.cid = cid;
  state.cloud.items = data.items;
  if (data.path && data.path.length) {
    state.cloud.path = data.path;
  } else if (cid === '0') {
    state.cloud.path = [{ cid: '0', name: '根目录' }];
  }
  renderCloud();
}

async function searchCloud() {
  const q = $('#cloudSearch').value.trim();
  if (!q) { loadCloudList(state.cloud.cid); return; }
  const data = await api(`/api/cloud/search?q=${encodeURIComponent(q)}`);
  state.cloud.items = data.items;
  renderCloud(true);
}

const VIDEO_EXT_RE = /\.(mp4|mkv|avi|mov|wmv|flv|ts|m2ts|mpg|mpeg|rmvb|rm|vob|webm|f4v|3gp)$/i;

function renderCloud(isSearch = false) {
  const crumbs = $('#cloudCrumbs');
  if (isSearch) {
    crumbs.innerHTML = `<a id="crumbBack">← 返回目录浏览</a><span>搜索结果</span>`;
    crumbs.querySelector('#crumbBack').addEventListener('click', () => loadCloudList(state.cloud.cid));
  } else {
    crumbs.innerHTML = state.cloud.path.map((p, i) =>
      `<a data-cid="${esc(p.cid)}">${esc(p.name || '根目录')}</a>` +
      (i < state.cloud.path.length - 1 ? '<span>/</span>' : '')).join('');
    crumbs.querySelectorAll('a').forEach((a) =>
      a.addEventListener('click', () => loadCloudList(a.dataset.cid)));
  }

  const list = $('#cloudList');
  if (!state.cloud.items.length) {
    list.innerHTML = '<div class="file-row dim"><span class="fname">（空）</span></div>';
    return;
  }
  list.innerHTML = state.cloud.items.map((it, i) => {
    const playable = VIDEO_EXT_RE.test(it.name);
    const checkable = it.is_dir || playable;
    const checked = state.cloud.selected.has(it.id + it.name) ? 'checked' : '';
    return `<div class="file-row ${checkable ? '' : 'dim'}">
      <input type="checkbox" data-i="${i}" ${checkable ? '' : 'disabled'} ${checked}>
      <span class="icon">${it.is_dir ? '📁' : (playable ? '🎞️' : '📄')}</span>
      <span class="fname ${it.is_dir ? 'clickable' : ''}" data-i="${i}">${esc(it.name)}</span>
      <span class="fsize">${it.is_dir ? '' : fmtSize(it.size)}</span>
    </div>`;
  }).join('');
  list.querySelectorAll('.fname.clickable').forEach((el) =>
    el.addEventListener('click', () => {
      const it = state.cloud.items[+el.dataset.i];
      state.cloud.path = [...state.cloud.path, { cid: it.id, name: it.name }];
      loadCloudList(it.id);
    }));
  list.querySelectorAll('input[type=checkbox]').forEach((el) =>
    el.addEventListener('change', () => {
      const it = state.cloud.items[+el.dataset.i];
      const key = it.id + it.name;
      if (el.checked) state.cloud.selected.set(key, it);
      else state.cloud.selected.delete(key);
      $('#selCount').textContent = state.cloud.selected.size;
    }));
}

async function enqueueSelected() {
  const items = [...state.cloud.selected.values()];
  if (!items.length) { toast('请先勾选文件或文件夹'); return; }
  const target = $('#cloudTarget').value;
  try {
    const r = await api('/api/cloud/download', {
      method: 'POST', body: { items, target_dir: target },
    });
    toast(`已加入 ${r.queued} 个下载任务`);
    state.cloud.selected.clear();
    $('#selCount').textContent = '0';
    renderCloud();
  } catch (e) { toast(e.message); }
}

/* ---------- 下载任务 ---------- */

const TASK_STATUS = {
  queued: ['排队中', 'var(--muted)'], downloading: ['下载中', 'var(--accent2)'],
  paused: ['已暂停', 'var(--muted)'], done: ['已完成', 'var(--green)'],
  failed: ['失败', 'var(--red)'], canceled: ['已取消', 'var(--muted)'],
};

async function loadTasks() {
  let data;
  try { data = await api('/api/tasks'); } catch (e) { return; }
  const items = data.items;
  const active = items.filter((t) => ['queued', 'downloading'].includes(t.status)).length;
  $('#taskBadge').textContent = active ? `(${active})` : '';
  $('#taskEmpty').classList.toggle('hidden', items.length > 0);
  $('#taskList').innerHTML = items.map((t) => {
    const pct = t.size ? Math.min(100, (t.downloaded / t.size * 100)) : 0;
    const [label, color] = TASK_STATUS[t.status] || [t.status, 'var(--muted)'];
    const btns = [];
    if (['queued', 'downloading'].includes(t.status)) {
      btns.push(`<button class="btn small" data-act="pause" data-id="${t.id}">暂停</button>`);
      btns.push(`<button class="btn small danger" data-act="cancel" data-id="${t.id}">取消</button>`);
    }
    if (['paused', 'failed', 'canceled'].includes(t.status))
      btns.push(`<button class="btn small" data-act="resume" data-id="${t.id}">继续</button>`);
    if (['done', 'failed', 'canceled'].includes(t.status))
      btns.push(`<button class="btn small danger" data-act="delete" data-id="${t.id}">删除记录</button>`);
    const pcls = t.status === 'done' ? 'done' : (t.status === 'failed' ? 'failed' : '');
    return `<div class="task-card">
      <div class="tline1">
        <span class="tname">${esc(t.name)}</span>
        <span class="tstatus" style="color:${color}">${label}${t.error ? ' · ' + esc(t.error) : ''}</span>
      </div>
      <div class="progress ${pcls}"><div style="width:${pct.toFixed(1)}%"></div></div>
      <div class="tline2">
        <span>${fmtSize(t.downloaded)} / ${fmtSize(t.size)}</span>
        <span>${fmtSpeed(t.speed)}</span>
        <span>→ ${esc(t.target_dir)}</span>
        <span class="tactions">${btns.join('')}</span>
      </div>
    </div>`;
  }).join('');
  $('#taskList').querySelectorAll('[data-act]').forEach((b) =>
    b.addEventListener('click', async () => {
      try { await api(`/api/tasks/${b.dataset.id}/${b.dataset.act}`, { method: 'POST' }); }
      catch (e) { toast(e.message); }
      loadTasks();
    }));
}

/* ---------- 设置 ---------- */

async function loadSettings() {
  state.settings = await api('/api/settings');
  $('#setMovieDir').value = state.settings.movie_dir;
  $('#setTvDir').value = state.settings.tv_dir;
  $('#setDownloadDir').value = state.settings.download_dir;
  $('#setTmdb').value = state.settings.tmdb_key;
  $('#setAssrt').value = state.settings.assrt_token;
  $('#setSpeed').value = state.settings.speed_limit;
  $('#setAutoScan').value = state.settings.auto_scan;
}

async function saveSettings() {
  await api('/api/settings', {
    method: 'POST',
    body: {
      movie_dir: $('#setMovieDir').value, tv_dir: $('#setTvDir').value,
      download_dir: $('#setDownloadDir').value, tmdb_key: $('#setTmdb').value,
      assrt_token: $('#setAssrt').value, speed_limit: $('#setSpeed').value,
      auto_scan: $('#setAutoScan').value,
    },
  });
  state.settings = await api('/api/settings');
  $('#settingsSaved').textContent = '已保存 ✓';
  setTimeout(() => { $('#settingsSaved').textContent = ''; }, 2500);
}

/* ---------- 目录选择器 ---------- */

async function openDirPicker(targetInputId) {
  state.dirPickTarget = targetInputId;
  $('#dirMask').classList.remove('hidden');
  await browseDir($('#' + targetInputId).value || '');
}

async function browseDir(path) {
  const data = await api(`/api/fs/list?path=${encodeURIComponent(path || '')}`);
  state.dirPath = data.path;
  $('#dirCurrent').textContent = '📂 ' + data.path;
  const rows = [];
  if (data.parent !== null) rows.push(`<div class="dir-row" data-path="${esc(data.parent)}">⬅ 上一级</div>`);
  data.dirs.forEach((d) => rows.push(`<div class="dir-row" data-path="${esc(d.path)}">📁 ${esc(d.name)}</div>`));
  $('#dirList').innerHTML = rows.join('') || '<div class="dir-row">（无子目录）</div>';
  $('#dirList').querySelectorAll('[data-path]').forEach((el) =>
    el.addEventListener('click', () => browseDir(el.dataset.path)));
}

/* ---------- 启动 ---------- */

async function boot() {
  try { state.settings = await api('/api/settings'); } catch (e) { return; }
  switchTab('cloud');
}

function bindEvents() {
  $$('.tab[data-tab]').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.tab)));
  $('#btnLogin').addEventListener('click', doLogin);
  $('#loginPw').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });

  // 115
  $('#btnQr').addEventListener('click', startQr);
  $('#btnCloudSearch').addEventListener('click', searchCloud);
  $('#cloudSearch').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchCloud(); });
  $('#btnCloudRoot').addEventListener('click', () => {
    state.cloud.path = [{ cid: '0', name: '根目录' }];
    loadCloudList('0');
  });
  $('#btnEnqueue').addEventListener('click', enqueueSelected);

  // 设置
  $('#btnSaveSettings').addEventListener('click', () => saveSettings().catch((e) => toast(e.message)));
  $('#btnScan').addEventListener('click', async () => {
    await api('/api/scan', { method: 'POST' });
    $('#scanStatus').textContent = '已开始扫描…';
    setTimeout(() => { $('#scanStatus').textContent = ''; }, 8000);
  });
  $('#btnSetViewerPw').addEventListener('click', async () => {
    const pw = $('#viewerPw').value;
    if (pw.length < 4) { toast('观影密码至少 4 位'); return; }
    await api('/api/auth/viewer_password', { method: 'POST', body: { password: pw } });
    $('#viewerPw').value = '';
    $('#viewerPwState').textContent = '当前：已设置观影密码';
    toast('观影密码已设置');
  });
  $('#btnClearViewerPw').addEventListener('click', async () => {
    await api('/api/auth/viewer_password', { method: 'POST', body: { password: '' } });
    $('#viewerPwState').textContent = '当前：观影页免密';
    toast('已清除观影密码，观影页恢复免密');
  });
  $('#btnChangePw').addEventListener('click', async () => {
    try {
      await api('/api/auth/password', { method: 'POST', body: { old: $('#oldPw').value, new: $('#newPw').value } });
      toast('密码已修改'); $('#oldPw').value = ''; $('#newPw').value = '';
    } catch (e) { toast(e.message); }
  });
  $('#btnLogout').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' }); location.reload();
  });
  $('#btnCloudLogout').addEventListener('click', async () => {
    await api('/api/cloud/logout', { method: 'POST' }); toast('已退出 115 登录');
    state.cloud = { cid: '0', path: [], items: [], selected: new Map() };
  });

  // 目录选择器
  $$('[data-browse]').forEach((b) =>
    b.addEventListener('click', () => openDirPicker(b.dataset.browse)));
  $('#btnPickDir').addEventListener('click', () => {
    if (state.dirPickTarget) $('#' + state.dirPickTarget).value = state.dirPath;
    $('#dirMask').classList.add('hidden');
  });
  $('#btnMkdir').addEventListener('click', async () => {
    const name = $('#dirNewName').value.trim();
    if (!name) return;
    try {
      const r = await api('/api/fs/mkdir', { method: 'POST', body: { path: state.dirPath + '/' + name } });
      $('#dirNewName').value = '';
      await browseDir(r.path);
      toast('文件夹已创建');
    } catch (e) { toast(e.message); }
  });
  bindModalClosers(document);
}

document.addEventListener('DOMContentLoaded', async () => {
  bindEvents();
  if (await checkAuth()) boot();
});
