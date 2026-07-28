/* 公共工具：观影端与管理端共用 */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let MEDIAHUB_DEVICE = null;
try {
  if (window.MediaHubNative && typeof MediaHubNative.getDeviceProfile === 'function') {
    MEDIAHUB_DEVICE = JSON.parse(MediaHubNative.getDeviceProfile() || 'null');
  }
} catch (e) {
  MEDIAHUB_DEVICE = null;
}

async function api(path, opts = {}) {
  if (MEDIAHUB_DEVICE && MEDIAHUB_DEVICE.id) {
    opts.headers = { 'X-MediaHub-Device': MEDIAHUB_DEVICE.id, ...(opts.headers || {}) };
  }
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

async function registerDevice() {
  if (!MEDIAHUB_DEVICE || !MEDIAHUB_DEVICE.id) return;
  try {
    await api('/api/devices/register', { method: 'POST', body: MEDIAHUB_DEVICE });
  } catch (e) {
    // 设备档案是旁路能力，NAS 旧版本或临时离线不阻断影片墙。
  }
}

function toast(msg, ms = 2200) {
  const el = document.createElement('div');
  el.className = 'toast'; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function esc(s) {
  // 注意：不能用 ??（nullish 合并），小米电视的 WebView 是 Chrome 66 不认，
  // 整个文件会解析失败导致海报墙空白
  return String(s == null ? '' : s).replace(/[&<>"']/g,
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

// 浏览器（Chrome/Edge/Safari 的 HTML5 <video>）能解的音频编码。
// 注意：Google Chrome 自带商业解码器，AC3 能播（MKV 容器也行），
// 但 EAC3(DD+ 5.1) 实测在 Mac Chrome 上 MKV/MP4 都无声——和 DTS/TrueHD 一样转码。
const BROWSER_AUDIO_OK = new Set([
  'aac', 'ac3', 'mp3', 'opus', 'vorbis', 'flac', 'alac', 'mp2',
  'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'pcm_u8',
]);

// Safari（含 iOS）：对流式 fMP4 转码流支持不可靠，走 IINA/原生切换路线
const IS_SAFARI = /^((?!chrome|chromium|crios|fxios|android).)*safari/i.test(navigator.userAgent);

async function playMedia(id) {
  const d = await api(`/api/media/${id}`);

  // 安卓 TV App 内（原生桥存在时）：不开网页播放器，直接全屏内嵌 ExoPlayer
  // 硬解原片（HEVC/DTS 都走电视/投影自己的解码器），遥控器菜单键切音轨/字幕。
  // 必须在碰任何播放器 DOM 之前返回——否则网页播放器遮罩会留在原生播放器背后
  if (window.MediaHubNative && typeof MediaHubNative.play === 'function') {
    let tvUrl = `${location.origin}/api/stream/${id}`;
    try {
      const pl = await api(`/api/media/${id}/playlink`);
      tvUrl = location.origin + pl.url;
    } catch (e) {}
    const tvSubs = (d.subs || []).map((t, i) => ({
      label: t.label || t.lang || `字幕 ${i + 1}`,
      url: `${location.origin}/api/subtitle/${id}/${i}`,
      ext: 'vtt',
    }));
    // 剧集的选集列表（原生播放器里「选集」按钮用）；电影/单集为空数组
    let eps = [];
    try {
      if (typeof window.episodesOf === 'function') {
        eps = (window.episodesOf(id) || []).map((e) => ({
          id: e.id,
          label: `S${String(e.season).padStart(2, '0')}E${String(e.episode || '?').padStart(2, '0')}`,
          name: e.name,
        }));
      }
    } catch (e) {}
    MediaHubNative.play(tvUrl, d.filename, JSON.stringify(tvSubs), JSON.stringify(eps));
    return;
  }

  const video = $('#playerVideo');
  if (video.dataset.mid && +video.dataset.mid !== id) savePos(video, +video.dataset.mid);
  video.innerHTML = '';
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

  // 签名播放链接：外部播放器（IINA/VLC/电视）没有登录 Cookie，用 24h 签名令牌
  let playUrl = `${location.origin}/api/stream/${id}`;
  try {
    const pl = await api(`/api/media/${id}/playlink`);
    playUrl = location.origin + pl.url;
  } catch (e) {}

  // 轨道信息（音轨数 + 音频编码 + 语言标签）
  let info = { available: false, audio: 0, audio_codecs: [], audio_tracks: [], preferred_audio: 0 };
  try { info = await api(`/api/media/${id}/tracks`); } catch (e) {}

  const badCodecs = (info.audio_codecs || []).filter((c) => !BROWSER_AUDIO_OK.has(c));
  const tracks = info.audio_tracks || [];
  const pref = info.preferred_audio || 0;
  const isMac = /Macintosh|Mac OS X/.test(navigator.userAgent);
  const nativeOk = /\.(mp4|m4v|mov|webm)(\?|$)/i.test(d.filename || '');

  // 只有 Safari 实现了原生 audioTracks 切换；其他浏览器想换音轨靠转码流（手动）
  const nativeSwitch = !!video.audioTracks && tracks.length > 1;
  // 需要转码：以服务端判断为准（按实际会播放的优选轨判断：
  // DTS/TrueHD 解不了；eac3 实测在 Mac Chrome 上无声，一律转 AAC）。
  // 旧服务端没这个字段时用本地兜底。
  // 不再为了"优选音轨"自动转码——能直放就直放，原生进度条体验最好；
  // 想听非默认音轨，音轨菜单手动切（那时才走转码）
  const useTranscode = info.needs_transcode !== undefined
    ? !!info.needs_transcode
    : badCodecs.length > 0;
  setupAudioMenu(video, id, info, useTranscode, nativeSwitch);

  // Mac 上：容器不支持 或 音频编码浏览器解不了 → 提供「用 IINA 打开」（直接拉起）
  const iinaBtn = $('#btnIina');
  if (isMac && (!nativeOk || badCodecs.length)) {
    iinaBtn.classList.remove('hidden');
    iinaBtn.onclick = async () => {
      try { await navigator.clipboard.writeText(playUrl); } catch (e) {}
      toast('正在拉起 IINA…（若没反应：链接已复制，IINA 里 ⌘U 粘贴即可）', 3500);
      window.location.href = 'iina://open?url=' + encodeURIComponent(playUrl);
    };
  } else {
    iinaBtn.classList.add('hidden');
  }
  if (badCodecs.length) {
    toast(`音频编码 ${[...new Set(badCodecs)].join(' / ')} 浏览器无法解码 → ` +
      '已实时转码 AAC 播放（拖动进度条会重新起流）；要原始音轨可用 IINA / 电视播放器', 5000);
  }
  // Safari 对流式转码 fMP4 支持不可靠：直接指路 IINA（Mac）或 Chrome
  if (IS_SAFARI && (badCodecs.length || !nativeOk)) {
    toast('Safari 无法在线播放此格式 → 请点下方「用 IINA 打开」，或改用 Chrome', 6000);
  }

  // 浏览器解不了的音轨 / 优选音轨非第 0 条 → 服务端实时转 AAC（视频流原样拷贝）；
  // 转码流不支持字节 seek：自绘全时长进度条，点哪儿从哪儿重新起流
  video.dataset.a = String(pref);
  video.dataset.transcode = useTranscode ? '1' : '';
  video.dataset.base = '0';
  video.src = `/api/stream/${id}` + (useTranscode ? `?audio=aac&a=${pref}` : '');
  setupSeekBar(video, id, info.duration || 0);

  $('#btnCopyLink').onclick = async () => {
    try { await navigator.clipboard.writeText(playUrl); toast('直链已复制（可粘贴到极米/Infuse 等播放器）'); }
    catch (e) { prompt('复制此直链:', playUrl); }
  };
  setupResume(video, id);
  setupEpisodeList(id);
  video.play().catch(() => {});
}

/* ---------- 转码流自绘进度条（全片时长，任意点选跳转） ---------- */

// 流式转码响应没有字节 range，原生进度条只能拖到已缓冲处；
// 自绘条始终展示全片时长：点在缓冲区内直接跳，点外面按时间点重新起流。
// 进度换算：绝对位置 = base（当前流的起始秒）+ video.currentTime
function setupSeekBar(video, id, duration) {
  const bar = $('#seekBar'), fill = $('#seekFill');
  if (!bar) return;
  let reloading = false;
  // 后端 ffprobe 时长为准：有些 MKV 头部时长是坏的（Slam Dunk 标 1 秒），
  // Chrome 只能按已收到数据估时长（渐进式），绝不能让它覆盖后端的准确值
  if (duration > 0) video.dataset.total = String(duration);

  const vis = () => {
    // Safari 转码流本身播不了，自绘条点了也没用，不展示
    const on = video.dataset.transcode === '1' && !IS_SAFARI;
    bar.classList.toggle('hidden', !on);
    video.classList.toggle('transcode', on);  // 隐藏 Chrome 原生时间轴
    updateSeekFill(video);
  };
  vis();
  video._seekVis = vis;  // 音轨菜单切轨（转码开启）后刷新

  video.ondurationchange = () => {
    if (parseFloat(video.dataset.total || '0') > 0) return;  // 已有后端准确时长
    const d = video.duration;
    if (isFinite(d) && d > 0)
      video.dataset.total = String(parseFloat(video.dataset.base || '0') + d);
  };

  const inBuffer = (abs) => {
    const base = parseFloat(video.dataset.base || '0');
    for (let i = 0; i < video.buffered.length; i++)
      if (abs >= base + video.buffered.start(i) - 0.5 && abs <= base + video.buffered.end(i))
        return true;
    return false;
  };
  const startStreamAt = (abs) => { transcodeAt(video, id, abs); };

  const track = bar.querySelector('.seek-track');
  bar.onclick = (e) => {
    const total = parseFloat(video.dataset.total || '0');
    if (!total || video.dataset.transcode !== '1') return;
    const r = track.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const abs = frac * total;
    if (inBuffer(abs)) {
      video.currentTime = abs - parseFloat(video.dataset.base || '0');
      return;
    }
    startStreamAt(abs);
  };

  // 键盘左右键 / Safari 原生条拖出缓冲区时的兜底
  video.onseeking = () => {
    if (video.dataset.transcode !== '1' || reloading || video._aligning) return;
    const abs = parseFloat(video.dataset.base || '0') + video.currentTime;
    if (inBuffer(abs)) return;
    reloading = true;
    startStreamAt(abs);
    setTimeout(() => { reloading = false; }, 800);
  };
}

function updateSeekFill(video) {
  const fill = $('#seekFill'), time = $('#seekTime');
  if (!fill) return;
  const total = parseFloat(video.dataset.total || '0');
  if (!total) {
    fill.style.width = '0%';
    if (time) time.textContent = '';
    return;
  }
  const abs = parseFloat(video.dataset.base || '0') + (video.currentTime || 0);
  fill.style.width = Math.min(100, abs / total * 100) + '%';
  if (time) time.textContent = `${fmtTime(abs)} / ${fmtTime(total)}`;
}

// 转码流从 t 秒重起后时间轴从 0 开始，字幕却是原片绝对时间：
// base 一变就刷新字幕轨地址，让服务端把 cue 整体前移 base 秒
function syncSubOffset(video, id) {
  const base = parseFloat(video.dataset.base || '0');
  video.querySelectorAll('track').forEach((tr, i) => {
    tr.src = `/api/subtitle/${id}/${i}` + (base > 0 ? `?offset=${base}` : '');
  });
}

// 转码起流统一入口：视频流是整包拷贝，-ss 只能落在关键帧上。
// 先问服务端 `-ss t` 的实际落点 start 作为 base（服务端起流时会对同一个 t
// 再测一次落点并从该点起音频，保证音画同点）；t→start 的前摇在加载后跳掉。
// 注意 URL 里传原始 t 而不是 start：MKV 上 -ss 恰好落在关键帧时刻会多退
// 一个 GOP，传 start 反而会让服务端落点再前移，base 就对不上了。
async function transcodeAt(video, id, abs) {
  const t = Math.max(0, Math.floor(abs));
  let start = t;
  try {
    const r = await api(`/api/stream-prep/${id}?t=${t}`);
    if (r && r.start >= 0) start = r.start;
  } catch (e) {}
  video.dataset.transcode = '1';
  video.dataset.base = String(start);
  const skip = t - start;
  if (skip > 0.25) {
    video._aligning = true;
    video.onloadedmetadata = () => {
      const done = () => { video._aligning = false; };
      video.addEventListener('seeked', done, { once: true });
      setTimeout(done, 3000);
      try { video.currentTime = skip; } catch (e) { done(); }
    };
  } else {
    video.onloadedmetadata = null;
  }
  video.src = `/api/stream/${id}?audio=aac&a=${video.dataset.a}&t=${t}`;
  video.play().catch(() => {});
  syncSubOffset(video, id);
  if (video._seekVis) video._seekVis();
}

/* ---------- 续播（记住上次播放位置） ---------- */

function fmtTime(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? h + ':' : '') + mm + ':' + String(ss).padStart(2, '0');
}

// 进度存浏览器 localStorage（mh_pos_<id>）：每台设备各记各的，30 秒以内不记，快播完自动清
function savePos(video, id) {
  const t = video.currentTime || 0;
  // 时长优先用后端准确值（坏头 MKV 的 video.duration 不可信）；换算成绝对位置
  const d = parseFloat(video.dataset.total || '0') || video.duration || 0;
  const abs = parseFloat(video.dataset.base || '0') + t;
  const key = `mh_pos_${id}`;
  try {
    if (d && d - abs <= 60) localStorage.removeItem(key);
    else if (abs > 30) localStorage.setItem(key, String(Math.floor(abs)));
  } catch (e) {}
}

function setupResume(video, id) {
  video.dataset.mid = String(id);
  // 播放中每 5 秒记一次进度（onXXX 赋值，换片不会叠加监听）
  let lastSave = 0;
  video.ontimeupdate = () => {
    updateSeekFill(video);
    const now = Date.now();
    if (now - lastSave < 5000) return;
    lastSave = now;
    savePos(video, id);
  };
  // 上次看到 30 秒以上 → 弹询问条：继续播放 / 从头开始
  const promptEl = $('#resumePrompt');
  if (!promptEl) return;
  let saved = 0;
  try { saved = parseInt(localStorage.getItem(`mh_pos_${id}`) || '0', 10) || 0; } catch (e) {}
  if (saved <= 30) { promptEl.classList.add('hidden'); return; }
  $('#resumeText').textContent = `上次看到 ${fmtTime(saved)}，是否从上次的位置开始？`;
  promptEl.classList.remove('hidden');
  // 8 秒无操作自动收起（自动播放会立刻触发 play 事件，不能靠 play 收）
  const hideTimer = setTimeout(() => promptEl.classList.add('hidden'), 8000);
  $('#btnResumeYes').onclick = () => {
    clearTimeout(hideTimer);
    promptEl.classList.add('hidden');
    if (video.dataset.transcode === '1') {
      // 转码流不能按字节 seek：关键帧对齐后重新起流
      transcodeAt(video, id, saved);
    } else {
      video.currentTime = saved;
      video.play().catch(() => {});
    }
  };
  $('#btnResumeNo').onclick = () => {
    clearTimeout(hideTimer);
    promptEl.classList.add('hidden');
    try { localStorage.removeItem(`mh_pos_${id}`); } catch (e) {}
    video.currentTime = 0;
    video.play().catch(() => {});
  };
  // ×：只关掉提示条，不动播放进度（记录保留，下次打开还会问）
  $('#btnResumeClose').onclick = () => {
    clearTimeout(hideTimer);
    promptEl.classList.add('hidden');
  };
}

/* ---------- 播放列表（当前剧集选集） ---------- */

// 数据由观影端 viewer.js 的 window.episodesOf(id) 提供；电影/单集时按钮隐藏
function setupEpisodeList(currentId) {
  const btn = $('#btnPlaylist'), panel = $('#playerPlaylist');
  const prev = $('#btnPrevEp'), next = $('#btnNextEp');
  if (!btn || !panel) return;
  const eps = (typeof window.episodesOf === 'function' && window.episodesOf(currentId)) || [];
  if (eps.length < 2) {
    btn.classList.add('hidden'); panel.classList.add('hidden');
    if (prev) prev.classList.add('hidden');
    if (next) next.classList.add('hidden');
    return;
  }
  const idx = eps.findIndex((e) => e.id === currentId);
  // 上一集/下一集：第一集/最后一集时置灰
  if (prev && next) {
    prev.classList.remove('hidden'); next.classList.remove('hidden');
    prev.disabled = idx <= 0;
    next.disabled = idx < 0 || idx >= eps.length - 1;
    prev.onclick = () => { if (idx > 0) playMedia(eps[idx - 1].id); };
    next.onclick = () => { if (idx >= 0 && idx < eps.length - 1) playMedia(eps[idx + 1].id); };
  }
  btn.classList.remove('hidden');
  panel.innerHTML = eps.map((e) => {
    const tag = `S${String(e.season).padStart(2, '0')}E${String(e.episode || '?').padStart(2, '0')}`;
    return `<div class="pl-row${e.id === currentId ? ' on' : ''}" data-ep="${e.id}">` +
      `<span class="pl-ep">${tag}</span><span class="pl-name">${esc(e.name)}</span></div>`;
  }).join('');
  panel.querySelectorAll('[data-ep]').forEach((r) =>
    r.addEventListener('click', () => playMedia(+r.dataset.ep)));
}

function closePlayer() {
  const v = $('#playerVideo');
  if (v.dataset.mid) savePos(v, +v.dataset.mid);
  v.pause(); v.removeAttribute('src'); v.load();
  v.ontimeupdate = null; v.onseeking = null; v.onloadedmetadata = null; v._aligning = false;
  const panel = $('#playerPlaylist');
  if (panel) panel.classList.add('hidden');
  const rp = $('#resumePrompt');
  if (rp) rp.classList.add('hidden');
  $('#playerMask').classList.add('hidden');
}

/* ---------- 音轨菜单 ---------- */

// ISO 639 语言码 → 显示名（ffprobe 标签命名习惯不一，常见码都归一到这里）
const LANG_NAMES = {
  zh: '中文', chi: '中文', zho: '中文', cmn: '中文', yue: '粤语',
  en: 'English', eng: 'English',
  ja: '日语', jp: '日语', jpn: '日语',
  it: '意大利语', ita: '意大利语', es: '西班牙语', spa: '西班牙语',
  fr: '法语', fre: '法语', fra: '法语', de: '德语', ger: '德语', deu: '德语',
  ko: '韩语', kor: '韩语', ru: '俄语', rus: '俄语', pt: '葡萄牙语', por: '葡萄牙语',
  th: '泰语', tha: '泰语', und: '',
};

function trackLabel(t, i) {
  const lang = LANG_NAMES[(t.lang || '').toLowerCase()] || t.lang || '';
  const bits = [lang, t.title, t.codec ? t.codec.toUpperCase() : ''].filter(Boolean);
  return `音轨 ${i + 1}${bits.length ? ' · ' + esc(bits.join(' / ')) : ''}`;
}

// useTranscode：当前播放的是服务端转码流；nativeSwitch：浏览器（Safari）能原生切内嵌音轨
function setupAudioMenu(video, id, info, useTranscode, nativeSwitch) {
  const sel = $('#audioTrackSel');
  sel.classList.remove('hidden');
  const tracks = (info && info.audio_tracks) || [];
  const n = (info && info.audio) || 0;
  if (tracks.length > 1) {
    sel.disabled = false;
    sel.innerHTML = tracks.map((t, i) =>
      `<option value="${i}">${trackLabel(t, i)}</option>`).join('');
    if (nativeSwitch && !useTranscode) {
      // Safari：直接启用/禁用内嵌音轨，初始启用优选轨
      const at = video.audioTracks;
      sel.value = String(info.preferred_audio || 0);
      for (let i = 0; i < at.length; i++) at[i].enabled = (i === +sel.value);
      sel.onchange = () => {
        for (let i = 0; i < at.length; i++) at[i].enabled = (i === +sel.value);
      };
      sel.title = '切换内嵌音轨';
    } else {
      // Chrome/Edge 等：换转码流的 a 参数，从当前进度重起（秒级生效）
      sel.value = String(video.dataset.a || info.preferred_audio || 0);
      sel.onchange = () => {
        video.dataset.a = sel.value;
        // 进度换算成绝对秒，关键帧对齐后重新起流
        const abs = parseFloat(video.dataset.base || '0') + video.currentTime;
        transcodeAt(video, id, abs);
      };
      sel.title = '切换音轨（服务端转码，从当前进度继续）';
    }
  } else {
    sel.disabled = true;
    sel.innerHTML = `<option>${n > 1 ? n + ' 条音轨' : '单音轨'}</option>`;
    sel.title = n > 1
      ? '当前浏览器不支持网页内切换音轨（仅 Safari 支持）；请用「电视播放器」或复制直链到 IINA / Infuse / VLC 切换'
      : '该视频只有一条音轨';
  }
}
