/* TV 遥控器方向键导航 + Apple TV 风格界面：仅在安卓 TV App 内（原生桥存在时）启用。
 *
 * 首页三段式（参考 Apple TV / 流媒体电视端布局）：
 * - 左栏「继续观看」：原生播放器 SharedPreferences 里的观看进度；一条都没有时填最新录入
 * - 中部 Hero：当前聚焦影片的大标题/简介/播放钮，背景是该片封面的清晰大图+全屏氛围模糊层
 * - 底部海报行：按 全部/电影/剧集 页签过滤的媒体库
 * 方向键在当前最上层界面内按几何位置移动焦点，确认键点击，
 * 返回键逐层关闭（由原生 MainActivity 调 window.tvBack）。 */
'use strict';

(function () {
  if (!window.MediaHubNative) return;
  document.body.classList.add('tv-mode');

  function $(s, r) { return (r || document).querySelector(s); }
  function $all(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function isVis(el) {
    if (!el || el.classList.contains('hidden')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function lib() { return (typeof state !== 'undefined' && state.library) || []; }
  function libItem(key) {
    const l = lib();
    for (let i = 0; i < l.length; i++) if (l[i].key === key) return l[i];
    return null;
  }
  // 任一媒体 id → 所属库条目（电影是自身，剧集反查包含该集的剧）
  function itemByMediaId(id) {
    const l = lib();
    for (let i = 0; i < l.length; i++) {
      const e = l[i];
      if (e.kind === 'movie') { if (e.id === id) return { item: e, ep: null }; }
      else {
        const eps = e.episodes || [];
        for (let j = 0; j < eps.length; j++) if (eps[j].id === id) return { item: e, ep: eps[j] };
      }
    }
    return null;
  }

  /* ---------- 氛围背景 + Hero 清晰背景（双层交叉淡入，避免闪黑） ---------- */

  const amb = document.createElement('div');
  amb.id = 'tvAmbient';
  amb.innerHTML = '<div class="amb"></div><div class="amb"></div>';
  document.body.insertBefore(amb, document.body.firstChild);
  let ambFront = 0;

  function setLayerBg(el, url) {
    if (!el || !url) return;
    const f = el._front || 0;
    const next = el.children[1 - f];
    const cur = el.children[f];
    if (!next || !cur) return;
    if (cur.style.backgroundImage.indexOf(url) >= 0 && cur.classList.contains('on')) return;
    if (next.style.backgroundImage.indexOf(url) >= 0 && next.classList.contains('on')) return;
    next.style.backgroundImage = 'url("' + url + '")';
    next.classList.add('on');
    cur.classList.remove('on');
    el._front = 1 - f;
  }

  /* ---------- 首页结构 ---------- */

  let home = null;
  let tab = ''; // ''=全部 movie=电影 show=剧集
  let featuredKey = null;

  function buildHome() {
    if (home || !$('#page-library')) return;
    home = document.createElement('div');
    home.id = 'tvHome';
    home.innerHTML =
      '<aside id="tvRail"><div class="rail-title">继续观看</div><div class="rail-list"></div></aside>' +
      '<section id="tvStage">' +
      '  <nav id="tvTabs">' +
      '    <span class="tv-tab on" data-tab="">全部</span>' +
      '    <span class="tv-tab" data-tab="movie">电影</span>' +
      '    <span class="tv-tab" data-tab="show">剧集</span>' +
      '  </nav>' +
      '  <div id="tvHeroCard">' +
      '    <div class="hc-bg"><div class="l"></div><div class="l"></div><div class="hc-fade"></div></div>' +
      '    <div class="hc-body">' +
      '      <div class="hc-badge"></div>' +
      '      <h1 class="hc-title"></h1>' +
      '      <div class="hc-meta"></div>' +
      '      <p class="hc-overview"></p>' +
      '      <button class="hc-watch"><i class="tri"></i>播放</button>' +
      '    </div>' +
      '  </div>' +
      '  <div id="tvShelf"></div>' +
      '</section>';
    const page = $('#page-library');
    page.parentNode.insertBefore(home, page);

    $all('.tv-tab', home).forEach((t) => t.addEventListener('click', () => {
      tab = t.dataset.tab;
      $all('.tv-tab', home).forEach((x) => x.classList.toggle('on', x === t));
      renderShelf();
      renderHero(shelfItems()[0] || null);
    }));
    $('.hc-watch', home).addEventListener('click', () => {
      const e = libItem(featuredKey);
      if (!e) return;
      if (e.kind === 'movie') playMedia(e.id); else openDetail(e.key);
    });
    renderRail();
    renderShelf();
    renderHero(shelfItems()[0] || null);
  }

  function shelfItems() {
    return lib().filter((e) => !tab || e.kind === tab);
  }

  function mediaIdOf(e) {
    return e.kind === 'movie' ? e.id : ((e.episodes || [])[0] || {}).id;
  }

  /* ---------- 左栏：继续观看（原生播放进度），空则填最新录入 ---------- */

  function resumeMap() {
    try {
      if (typeof MediaHubNative.getResume === 'function') {
        return JSON.parse(MediaHubNative.getResume() || '{}');
      }
    } catch (e) {}
    return {};
  }

  function fmtPos(ms) {
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(ss).padStart(2, '0');
  }

  function renderRail() {
    const list = $('.rail-list', home);
    if (!list) return;
    const resume = resumeMap();
    const rows = [];
    Object.keys(resume).forEach((k) => {
      const id = +k.replace(/^pos_/, '');
      const hit = itemByMediaId(id);
      if (hit && resume[k] > 0) rows.push({ item: hit.item, ep: hit.ep, id, pos: resume[k] });
    });
    $('.rail-title', home).textContent = rows.length ? '继续观看' : '最新录入';
    if (!rows.length) {
      lib().slice(0, 6).forEach((e) => rows.push({ item: e, ep: null, id: mediaIdOf(e), pos: 0 }));
    }
    list.innerHTML = rows.slice(0, 6).map((r) => {
      const e = r.item;
      const sub = r.ep
        ? 'S' + String(r.ep.season).padStart(2, '0') + 'E' + String(r.ep.episode || '?').padStart(2, '0')
        : (e.year || '');
      const right = r.pos > 0 ? '看到 ' + fmtPos(r.pos) : (e.kind === 'show' ? e.count + ' 集' : '电影');
      return '<div class="rail-row" data-key="' + esc(e.key) + '" data-mid="' + r.id + '">' +
        '<img src="' + (e.poster ? '/api/poster/' + encodeURIComponent(e.poster) : '/api/poster/_none') + '">' +
        '<div class="rr-info"><div class="rr-title">' + esc(e.name_cn || e.title) + '</div>' +
        '<div class="rr-sub">' + esc(sub) + '</div>' +
        '<div class="rr-pos">' + esc(right) + '</div></div></div>';
    }).join('');
    $all('.rail-row', list).forEach((r) => r.addEventListener('click', () => {
      // 继续观看直接起播（原生播放器自己会问续播/从头）；无进度的打开详情
      if (r.querySelector('.rr-pos').textContent.indexOf('看到') === 0) {
        playMedia(+r.dataset.mid);
      } else {
        openDetail(r.dataset.key);
      }
    }));
  }

  /* ---------- 底部海报行 ---------- */

  function renderShelf() {
    const shelf = $('#tvShelf', home);
    if (!shelf) return;
    shelf.innerHTML = shelfItems().map((e) =>
      '<div class="tcard" data-key="' + esc(e.key) + '">' +
      '<div class="tp-wrap"><img loading="lazy" src="' +
      (e.poster ? '/api/poster/' + encodeURIComponent(e.poster) : '/api/poster/_none') + '">' +
      (e.kind === 'show' ? '<span class="tbadge">' + e.count + '集</span>' : '') +
      '</div><div class="ttitle">' + esc(e.name_cn || e.title) + '</div></div>'
    ).join('');
    $all('.tcard', shelf).forEach((c) => c.addEventListener('click', () => openDetail(c.dataset.key)));
  }

  /* ---------- 中部 Hero ---------- */

  function renderHero(e) {
    if (!home || !e) return;
    featuredKey = e.key;
    $('.hc-badge', home).textContent = e.kind === 'show' ? '剧集 · 共 ' + e.count + ' 集' : '电影';
    $('.hc-title', home).textContent = e.name_cn || e.title;
    $('.hc-meta', home).textContent = [e.year, e.genres, e.rating ? '★ ' + e.rating : '']
      .filter(Boolean).join(' · ');
    $('.hc-overview', home).textContent = e.overview || '';
    // 大背景优先用 TMDB 横版剧照（backdrop），没有再回退竖版海报
    const pic = e.backdrop || e.poster;
    const url = pic ? '/api/poster/' + encodeURIComponent(pic) : '';
    setLayerBg(amb, url);
    const hcBg = $('.hc-bg', home);
    if (url) setLayerBg(hcBg, url);
  }

  /* ---------- 焦点管理 ---------- */

  // 当前最上层的可操作元素集合（层级：播放器 > 弹窗 > 首页）
  function currentItems() {
    const player = $('#playerMask');
    if (isVis(player)) {
      return $all('button, select, .pl-row', player).filter(isVis);
    }
    const masks = $all('.modal-mask').filter(isVis);
    if (masks.length) {
      const top = masks[masks.length - 1];
      // 剧集行整行作为一个焦点项（行内小按钮不再单独聚焦，避免横向噪音）
      return $all('button, input, select, [data-pick], [data-confirm], .ep-row', top)
        .filter((el) => isVis(el) &&
          (el.classList.contains('ep-row') || !el.closest('.ep-row')));
    }
    if (home) {
      return $all('.rail-row, .tv-tab, .hc-watch, .tcard', home).filter(isVis);
    }
    return $all('#libGrid .card, .toolbar select, .toolbar input').filter(isVis);
  }

  /* ---------- 海报行横向滚动：手动算 scrollLeft + JS 缓动 ----------
     （Chrome 66 对 scrollIntoView 的 {inline:'nearest'} 选项支持不全，
       且它会把所有可滚动祖先一起滚，容易把页面带偏） */

  let shelfAnim = null;
  function scrollShelfTo(el) {
    const shelf = $('#tvShelf');
    if (!shelf) return;
    const sr = shelf.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const margin = 40; // 边缘留白：聚焦放大后不贴边，也给下一张露出半截作提示
    let target = shelf.scrollLeft;
    if (r.left < sr.left + margin) target -= (sr.left + margin) - r.left;
    else if (r.right > sr.right - margin) target += r.right - (sr.right - margin);
    const maxScroll = shelf.scrollWidth - shelf.clientWidth;
    if (target < 0) target = 0;
    if (target > maxScroll) target = maxScroll;
    if (target === shelf.scrollLeft) return;
    if (shelfAnim) cancelAnimationFrame(shelfAnim);
    const from = shelf.scrollLeft, delta = target - from, t0 = Date.now(), dur = 180;
    const step = function () {
      const t = Math.min(1, (Date.now() - t0) / dur);
      const ease = 1 - Math.pow(1 - t, 3); // easeOutCubic
      shelf.scrollLeft = from + delta * ease;
      shelfAnim = t < 1 ? requestAnimationFrame(step) : null;
    };
    step();
  }

  function setFocus(el) {
    $all('.tv-focus').forEach((x) => x.classList.remove('tv-focus'));
    if (!el) return;
    el.classList.add('tv-focus');
    if (el.classList.contains('tcard')) {
      scrollShelfTo(el);
    } else {
      try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) { el.scrollIntoView(false); }
    }
    // 聚焦海报/续看行 → Hero 与氛围背景联动
    const key = el.dataset ? el.dataset.key : null;
    if (key && (el.classList.contains('tcard') || el.classList.contains('rail-row'))) {
      const e = libItem(key);
      if (e) renderHero(e);
    }
  }

  function focused() {
    const f = $('.tv-focus');
    return f && isVis(f) ? f : null;
  }

  function move(dir) {
    const items = currentItems();
    if (!items.length) return;
    const cur = focused();
    if (!cur || items.indexOf(cur) < 0) { setFocus(items[0]); return; }
    const r = cur.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let best = null, bestScore = Infinity;
    items.forEach((el) => {
      if (el === cur) return;
      const b = el.getBoundingClientRect();
      const x = b.left + b.width / 2, y = b.top + b.height / 2;
      const dx = x - cx, dy = y - cy;
      if (dir === 'left' && dx >= -4) return;
      if (dir === 'right' && dx <= 4) return;
      if (dir === 'up' && dy >= -4) return;
      if (dir === 'down' && dy <= 4) return;
      const horiz = dir === 'left' || dir === 'right';
      const primary = horiz ? Math.abs(dx) : Math.abs(dy);
      const secondary = horiz ? Math.abs(dy) : Math.abs(dx);
      const score = primary + secondary * 2.2;
      if (score < bestScore) { bestScore = score; best = el; }
    });
    if (best) setFocus(best);
  }

  document.addEventListener('keydown', (ev) => {
    const k = ev.keyCode;
    if (k === 37 || k === 38 || k === 39 || k === 40) {
      const a = document.activeElement;
      if (a && a.tagName === 'SELECT' && a !== focused()) return;
      ev.preventDefault();
      move({ 37: 'left', 38: 'up', 39: 'right', 40: 'down' }[k]);
    } else if (k === 13 || k === 23) { // Enter / DPAD_CENTER
      const f = focused();
      if (f) {
        ev.preventDefault();
        if (f.tagName === 'SELECT' || (f.tagName === 'INPUT' && /text|search|password/.test(f.type))) {
          f.focus();
        } else {
          f.click();
        }
      }
    }
  }, true);

  // 返回键（原生 MainActivity.onBackPressed 调用）：逐层关闭，返回 true 表示已处理
  window.tvBack = function () {
    const player = $('#playerMask');
    if (isVis(player) && typeof closePlayer === 'function') { closePlayer(); return true; }
    const masks = $all('.modal-mask').filter(isVis);
    if (masks.length) { masks[masks.length - 1].classList.add('hidden'); return true; }
    return false;
  };

  // 弹窗开关/列表重绘后校正焦点：弹窗打开时焦点移进弹窗（首选主按钮），
  // 媒体库异步渲染完后建首页并聚焦第一张海报
  let homeBuilt = false;
  let moQueued = false;
  const mo = new MutationObserver(() => {
    if (moQueued) return;
    moQueued = true;
    setTimeout(() => {
      moQueued = false;
      if (!homeBuilt && lib().length) {
        homeBuilt = true;
        buildHome();
      }
      const items = currentItems();
      const cur = focused();
      if (items.length && (!cur || items.indexOf(cur) < 0)) {
        const primary = items.filter((el) => el.classList.contains('primary'))[0];
        const pill = items.filter((el) => el.classList.contains('ep-pill'))[0];
        setFocus(primary || pill || items[0]);
      }
    }, 0);
  });
  mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });

  window.addEventListener('load', () => {
    setTimeout(() => {
      if (!homeBuilt && lib().length) { homeBuilt = true; buildHome(); }
      if (!focused()) {
        const first = $('.tcard', home || document);
        if (first) setFocus(first); else move('down');
      }
    }, 800);
  });
})();
