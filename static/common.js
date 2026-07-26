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

// 浏览器（Chrome/Edge/Safari 的 HTML5 <video>）能解的音频编码；
// ac3/eac3/dts/truehd 等只有 Safari 部分支持、Chrome 一律无声
const BROWSER_AUDIO_OK = new Set([
  'aac', 'mp3', 'opus', 'vorbis', 'flac', 'alac', 'mp2',
  'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'pcm_u8',
]);

async function playMedia(id) {
  const d = await api(`/api/media/${id}`);
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

  // 只有 Safari 实现了原生 audioTracks 切换；其他浏览器靠服务端转码流换音轨
  const nativeSwitch = !!video.audioTracks && tracks.length > 1;
  // 需要转码：有解不了的音轨；或优选音轨不是第 0 条且不能原生切换（直放只会播第 0 条）
  const useTranscode = badCodecs.length > 0 || (tracks.length > 1 && pref !== 0 && !nativeSwitch);
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

  // 浏览器解不了的音轨 / 优选音轨非第 0 条 → 服务端实时转 AAC（视频流原样拷贝）；
  // 转码流不支持字节 seek：自绘全时长进度条，点哪儿从哪儿重新起流
  video.dataset.a = String(pref);
  video.dataset.transcode = useTranscode ? '1' : '';
  video.dataset.base = '0';
  video.src = `/api/stream/${id}` + (useTranscode ? `?audio=aac&a=${pref}` : '');
  setupSeekBar(video, id, info.duration || 0);

  // 安卓 TV App 内（原生桥存在时）：提供「外部播放器」通道，
  // 由电视/投影仪自己的播放器硬解，支持内嵌多音轨/字幕切换
  const extBtn = $('#btnExternal');
  if (window.MediaHubNative && typeof MediaHubNative.play === 'function') {
    extBtn.classList.remove('hidden');
    extBtn.onclick = () => {
      MediaHubNative.play(playUrl, d.filename);
    };
  } else {
    extBtn.classList.add('hidden');
  }
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
  // 后端探测的全片时长：进度条立即有总长，不等 metadata
  if (duration > 0) video.dataset.total = String(duration);

  const vis = () => {
    const on = video.dataset.transcode === '1';
    bar.classList.toggle('hidden', !on);
    video.classList.toggle('transcode', on);  // 隐藏 Chrome 原生时间轴
    updateSeekFill(video);
  };
  vis();
  video._seekVis = vis;  // 音轨菜单切轨（转码开启）后刷新

  video.ondurationchange = () => {
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
  const startStreamAt = (abs) => {
    const t = Math.max(0, Math.floor(abs));
    video.dataset.base = String(t);
    video.src = `/api/stream/${id}?audio=aac&a=${video.dataset.a}&t=${t}`;
    video.play().catch(() => {});
  };

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
    if (video.dataset.transcode !== '1' || reloading) return;
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

/* ---------- 续播（记住上次播放位置） ---------- */

function fmtTime(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? h + ':' : '') + mm + ':' + String(ss).padStart(2, '0');
}

// 进度存浏览器 localStorage（mh_pos_<id>）：每台设备各记各的，30 秒以内不记，快播完自动清
function savePos(video, id) {
  const t = video.currentTime || 0, d = video.duration || 0;
  const key = `mh_pos_${id}`;
  try {
    if (d && d - t <= 60) localStorage.removeItem(key);
    else if (t > 30) localStorage.setItem(key, String(Math.floor(t)));
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
  $('#btnResumeYes').onclick = () => {
    promptEl.classList.add('hidden');
    if (video.dataset.transcode === '1') {
      // 转码流不能按字节 seek：按时间点重新起流
      video.dataset.base = String(saved);
      video.src = `/api/stream/${id}?audio=aac&a=${video.dataset.a}&t=${saved}`;
      video.play().catch(() => {});
    } else {
      video.currentTime = saved;
      video.play().catch(() => {});
    }
  };
  $('#btnResumeNo').onclick = () => {
    promptEl.classList.add('hidden');
    try { localStorage.removeItem(`mh_pos_${id}`); } catch (e) {}
    video.currentTime = 0;
    video.play().catch(() => {});
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
  v.ontimeupdate = null; v.onseeking = null;
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
        video.dataset.transcode = '1';
        // 进度换算成绝对秒（重起流后 currentTime 从 0 起算）
        const t = Math.floor(parseFloat(video.dataset.base || '0') + video.currentTime);
        video.dataset.base = String(t);
        video.src = `/api/stream/${id}?audio=aac&a=${sel.value}` +
          `&t=${t}`;
        video.play().catch(() => {});
        if (video._seekVis) video._seekVis();
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
