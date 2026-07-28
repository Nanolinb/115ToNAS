/* TV 遥控器方向键导航 + Apple TV 风格界面：仅在安卓 TV App 内（原生桥存在时）启用。
 *
 * 首页采用低成本 Apple TV 式舞台：
 * - 顶部品牌与分类导航
 * - 全宽 Hero：清晰横版剧照、标题、简介和播放按钮
 * - 继续观看横向轨道：服务端进度优先，本机存档兜底
 * - 影片海报轨道：只使用 transform/opacity 动画
 * 方向键在当前最上层界面内按几何位置移动焦点，确认键点击，
 * 返回键逐层关闭（由原生 MainActivity 调 window.tvBack）。 */
'use strict';

(function () {
  const forceTvPreview = /(?:^|[?&])tv=1(?:&|$)/.test(location.search);
  if (!window.MediaHubNative && !forceTvPreview) return;
  document.body.classList.add('tv-mode');
  try {
    const c = MEDIAHUB_DEVICE && MEDIAHUB_DEVICE.capabilities;
    if ((c && (+c.androidApi <= 23 || +c.height <= 720)) ||
        (forceTvPreview && window.innerHeight <= 720)) {
      document.body.classList.add('tv-low-power');
    }
  } catch (e) {}

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

  /* ---------- Hero 双层背景（交叉淡入，避免闪黑） ---------- */

  const amb = document.createElement('div');
  amb.id = 'tvAmbient';
  amb.innerHTML = '<div class="amb"></div><div class="amb"></div>';
  document.body.insertBefore(amb, document.body.firstChild);
  function setLayerBg(el, url, fallbackUrl) {
    if (!el) return;
    if (!url) {
      Array.prototype.forEach.call(el.children, function (layer) {
        if (layer.classList && layer.classList.contains('l')) {
          layer.style.backgroundImage = '';
          layer.classList.remove('on');
        }
      });
      el._front = 0;
      return;
    }
    const apply = function (resolved) {
      const f = el._front || 0;
      const next = el.children[1 - f];
      const cur = el.children[f];
      if (!next || !cur) return;
      if (cur.style.backgroundImage.indexOf(resolved) >= 0
          && cur.classList.contains('on')) return;
      next.style.backgroundImage = 'url("' + resolved + '")';
      next.classList.add('on');
      cur.classList.remove('on');
      el._front = 1 - f;
    };
    const probe = new Image();
    probe.onload = function () { apply(url); };
    probe.onerror = function () {
      if (fallbackUrl && fallbackUrl !== url) apply(fallbackUrl);
    };
    probe.src = url;
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
      '<header id="tvChrome">' +
      '  <div class="tv-brand"><span class="tv-brand-mark">A</span><span>Aurora</span></div>' +
      '  <nav id="tvTabs">' +
      '    <span class="tv-tab on" data-tab="">全部</span>' +
      '    <span class="tv-tab" data-tab="movie">电影</span>' +
      '    <span class="tv-tab" data-tab="show">剧集</span>' +
      '  </nav><div class="tv-nas"><i></i> NAS 在线</div>' +
      '</header>' +
      '<section id="tvStage">' +
      '  <div id="tvHeroCard">' +
      '    <div class="hc-bg"><div class="l"></div><div class="l"></div><div class="hc-fade"></div></div>' +
      '    <div class="hc-body">' +
      '      <div class="hc-badge">AURORA 精选</div>' +
      '      <h1 class="hc-title"></h1>' +
      '      <div class="hc-meta"></div>' +
      '      <p class="hc-overview"></p>' +
      '      <div class="hc-actions"><button class="hc-watch"><i class="tri"></i> 播放</button>' +
      '      <button class="hc-detail">更多信息</button></div>' +
      '    </div>' +
      '  </div>' +
      '  <div class="tv-row-head"><h2>全部影片与剧集</h2><span class="tv-count"></span></div>' +
      '  <div id="tvShelf"></div>' +
      '  <div class="tv-row-head"><h2 id="continueTitle">继续观看</h2></div>' +
      '  <div id="tvContinue"></div>' +
      '</section>';
    const page = $('#page-library');
    page.parentNode.insertBefore(home, page);

    $all('.tv-tab', home).forEach((t) => t.addEventListener('click', () => {
      tab = t.dataset.tab;
      $all('.tv-tab', home).forEach((x) => x.classList.toggle('on', x === t));
      renderShelf();
      renderHero(firstHeroItem());
    }));
    $('.hc-watch', home).addEventListener('click', () => {
      const e = libItem(featuredKey);
      if (!e) return;
      if (e.kind === 'movie') playMedia(e.id); else openDetail(e.key);
    });
    $('.hc-detail', home).addEventListener('click', () => {
      const e = libItem(featuredKey);
      if (e) openDetail(e.key);
    });
    renderContinue();
    renderShelf();
    renderHero(firstHeroItem());
  }

  function shelfItems() {
    return lib().filter((e) => !tab || e.kind === tab);
  }

  function firstHeroItem() {
    const items = shelfItems();
    return items.filter((e) => e.backdrop || e.poster)[0] || items[0] || null;
  }

  function mediaIdOf(e) {
    return e.kind === 'movie' ? e.id : ((e.episodes || [])[0] || {}).id;
  }

  /* ---------- 继续观看：服务端进度优先，本机存档兜底 ---------- */

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

  function renderContinue() {
    const list = $('#tvContinue', home);
    if (!list) return;
    const rows = [];
    const appendLocal = function () {
      const resume = resumeMap();
      Object.keys(resume).forEach((k) => {
        const id = +k.replace(/^pos_/, '');
        const hit = itemByMediaId(id);
        if (hit && resume[k] > 0
            && !rows.some((r) => r.id === id)) {
          rows.push({ item: hit.item, ep: hit.ep, id: id,
            pos: resume[k], duration: 0 });
        }
      });
    };
    const draw = function () {
      $('#continueTitle', home).textContent = rows.length ? '继续观看' : '最近添加';
      if (!rows.length) {
        lib().slice(0, 5).forEach((e) => rows.push({
          item: e, ep: null, id: mediaIdOf(e), pos: 0, duration: 0
        }));
      }
      list.innerHTML = rows.slice(0, 6).map((r) => {
        const e = r.item;
        const sub = r.ep
          ? 'S' + String(r.ep.season).padStart(2, '0') + 'E'
            + String(r.ep.episode || '?').padStart(2, '0')
          : (e.year || (e.kind === 'show' ? e.count + ' 集' : '电影'));
        const pct = r.duration > 0 ? Math.min(100, Math.round(r.pos / r.duration * 100)) : 0;
        const pic = e.backdrop || e.poster;
        return '<div class="continue-card" data-key="' + esc(e.key)
          + '" data-mid="' + r.id + '" data-resume="' + (r.pos > 0 ? '1' : '') + '">' +
          '<div class="continue-art"><img src="'
          + (pic ? '/api/poster/' + encodeURIComponent(pic) : '/api/poster/_none') + '">' +
          '<span class="continue-play" aria-hidden="true"></span>' +
          (pct ? '<span class="continue-progress"><i style="width:' + pct + '%"></i></span>' : '') +
          '</div><div class="continue-copy"><strong>' + esc(e.name_cn || e.title)
          + '</strong><span>' + esc(r.pos > 0 ? sub + ' · 看到 ' + fmtPos(r.pos) : sub)
          + '</span></div></div>';
      }).join('');
      $all('.continue-card', list).forEach((r) => r.addEventListener('click', () => {
        if (r.dataset.resume === '1') playMedia(+r.dataset.mid);
        else openDetail(r.dataset.key);
      }));
    };

    api('/api/progress?limit=12').then((data) => {
      (data.items || []).forEach((p) => {
        const hit = itemByMediaId(+p.media_id);
        if (hit) rows.push({ item: hit.item, ep: hit.ep, id: +p.media_id,
          pos: +p.position_ms || 0, duration: +p.duration_ms || 0 });
      });
      appendLocal();
      draw();
    }).catch(() => {
      appendLocal();
      draw();
    });
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
    $('.tv-count', home).textContent = shelfItems().length + ' 个项目';
    $all('.tcard', shelf).forEach((c) => c.addEventListener('click', () => openDetail(c.dataset.key)));
  }

  /* ---------- 中部 Hero ---------- */

  function renderHero(e) {
    if (!home || !e) return;
    featuredKey = e.key;
    $('.hc-badge', home).textContent = 'AURORA 精选 · '
      + (e.kind === 'show' ? '剧集' : '电影');
    $('.hc-title', home).textContent = e.name_cn || e.title;
    $('.hc-meta', home).textContent = [e.year, e.genres, e.rating ? '★ ' + e.rating : '']
      .filter(Boolean).join(' · ');
    $('.hc-overview', home).textContent = e.overview || '';
    // 大背景优先用 TMDB 横版剧照（backdrop），没有再回退竖版海报
    const pic = e.backdrop || e.poster;
    const url = pic ? '/api/poster/' + encodeURIComponent(pic) : '';
    const fallback = e.poster ? '/api/poster/' + encodeURIComponent(e.poster) : '';
    setLayerBg(amb, url, fallback);
    const hcBg = $('.hc-bg', home);
    setLayerBg(hcBg, url, fallback);
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
      return $all('button, input, select, [data-pick], [data-confirm]', top).filter(isVis);
    }
    if (home) {
      return $all('.tv-tab, .hc-watch, .hc-detail, .continue-card, .tcard', home).filter(isVis);
    }
    return $all('#libGrid .card, .toolbar select, .toolbar input').filter(isVis);
  }

  /* ---------- 海报行横向滚动：手动算 scrollLeft + JS 缓动 ----------
     （Chrome 66 对 scrollIntoView 的 {inline:'nearest'} 选项支持不全，
       且它会把所有可滚动祖先一起滚，容易把页面带偏） */

  let shelfAnim = null;
  function scrollRowTo(el, row) {
    if (!row) return;
    const sr = row.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const margin = 40; // 边缘留白：聚焦放大后不贴边，也给下一张露出半截作提示
    let target = row.scrollLeft;
    if (r.left < sr.left + margin) target -= (sr.left + margin) - r.left;
    else if (r.right > sr.right - margin) target += r.right - (sr.right - margin);
    const maxScroll = row.scrollWidth - row.clientWidth;
    if (target < 0) target = 0;
    if (target > maxScroll) target = maxScroll;
    if (target === row.scrollLeft) return;
    if (shelfAnim) cancelAnimationFrame(shelfAnim);
    const from = row.scrollLeft, delta = target - from, t0 = Date.now(), dur = 180;
    const step = function () {
      const t = Math.min(1, (Date.now() - t0) / dur);
      const ease = 1 - Math.pow(1 - t, 3); // easeOutCubic
      row.scrollLeft = from + delta * ease;
      shelfAnim = t < 1 ? requestAnimationFrame(step) : null;
    };
    step();
  }

  function scrollPageTo(el) {
    if (!home || !home.contains(el)) return;
    const r = el.getBoundingClientRect();
    const topLimit = 18;
    const bottomLimit = window.innerHeight - 34;
    if (r.top < topLimit) {
      home.scrollTop += r.top - topLimit;
    } else if (r.bottom > bottomLimit) {
      home.scrollTop += r.bottom - bottomLimit;
    }
  }

  function setFocus(el) {
    $all('.tv-focus').forEach((x) => x.classList.remove('tv-focus'));
    if (!el) return;
    el.classList.add('tv-focus');
    if (el.classList.contains('tcard') || el.classList.contains('continue-card')) {
      scrollRowTo(el, el.classList.contains('tcard') ? $('#tvShelf') : $('#tvContinue'));
    } else {
      try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) { el.scrollIntoView(false); }
    }
    // 横向卡片仍需要带动最外层纵向容器；旧实现遗漏了这一步，
    // 导致焦点进入屏外行但画面停在原处。
    scrollPageTo(el);
    // 聚焦海报/续看行 → Hero 与氛围背景联动
    const key = el.dataset ? el.dataset.key : null;
    if (key && (el.classList.contains('tcard') || el.classList.contains('continue-card'))) {
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
        const preferred = home && !isVis($('#detailMask'))
          ? ($('.tcard', home) || $('.continue-card', home) || $('.hc-watch', home))
          : items.filter((el) => el.classList.contains('primary'))[0];
        setFocus(preferred || items[0]);
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
