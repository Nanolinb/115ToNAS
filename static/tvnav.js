/* TV 遥控器方向键导航 + Apple TV 风格界面：仅在安卓 TV App 内（原生桥存在时）启用。
 *
 * 首页三段式（参考 Apple TV / 流媒体电视端布局）：
 * - 左栏「继续观看」：原生播放器 SharedPreferences 里的观看进度；一条都没有时填最新录入
 * - 中部 Hero：当前聚焦影片的大标题/简介/播放钮，背景是该片封面的清晰大图+全屏氛围模糊层，
 *   背景层向下延伸出卡片 50% 并渐隐，当作整个舞台的背景
 * - 底部海报行「最近更新」：最新录入的 8 条（近 7 天看过的隐藏）
 * - 顶部过滤器：类型/题材/年份三个胶囊，确认键弹纵向选项浮层，纯方向键+确认+返回操作
 *   （不依赖 WebView 原生 select；对最近更新与浏览层两排同时生效）
 * - 浏览层：焦点在底部海报行按「下」打开 #tvBrowse（电影/剧集两排全量列表，
 *   滑动渐显进场、浮在 Hero 之上透出背景大图；上排再按「上」或返回键反向收层）
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
  // 顶部过滤器：类型 ''=全部 / movie / show；题材 ''=全部；年份 0=全部
  const flt = { kind: '', genre: '', year: 0 };
  let featuredKey = null;

  function buildHome() {
    if (home || !$('#page-library')) return;
    home = document.createElement('div');
    home.id = 'tvHome';
    home.innerHTML =
      '<aside id="tvRail"><div class="rail-title">继续观看</div><div class="rail-list"></div></aside>' +
      '<section id="tvStage">' +
      '  <nav id="tvTabs">' +
      '    <span class="tv-tab tv-filter" data-flt="kind"></span>' +
      '    <span class="tv-tab tv-filter" data-flt="genre"></span>' +
      '    <span class="tv-tab tv-filter" data-flt="year"></span>' +
      '  </nav>' +
      '  <div id="tvFltPop"></div>' +
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
      '  <div id="tvBrowse"></div>' +
      '  <div class="shelf-title">最近更新</div>' +
      '  <div id="tvShelf"></div>' +
      '</section>';
    const page = $('#page-library');
    page.parentNode.insertBefore(home, page);

    $all('.tv-filter', home).forEach((c) => c.addEventListener('click', () => openFltPop(c.dataset.flt)));
    updateCapsules();
    $('.hc-watch', home).addEventListener('click', () => {
      const e = libItem(featuredKey);
      if (!e) return;
      if (e.kind === 'movie') playMedia(e.id); else openDetail(e.key);
    });
    renderRail();
    renderShelf(() => renderHero(shelfItems()[0] || null));
  }

  /* ---------- 底部海报行：最新录入 8 条，近 7 天看过的隐藏 ---------- */

  // 近 7 天有观看记录（updated_at）的 media id 集合；null=未加载，拉取失败退回 {}（不过滤）
  let recentWatched = null;
  function loadRecentWatched(cb) {
    if (recentWatched) { cb(); return; }
    fetch('/api/progress?full=1').then((r) => (r.ok ? r.json() : null)).then((m) => {
      const set = {};
      const cutoff = Math.floor(Date.now() / 1000) - 7 * 86400;
      if (m) {
        Object.keys(m).forEach((id) => {
          const v = m[id];
          if (v && Number(v.ts) >= cutoff) set[id] = true;
        });
      }
      recentWatched = set;
      cb();
    }).catch(() => { recentWatched = {}; cb(); });
  }

  // 电影看自身 media id，剧集看任意一集的 id
  function isRecentWatched(e) {
    if (!recentWatched) return false;
    if (e.kind === 'movie') return !!recentWatched[e.id];
    const eps = e.episodes || [];
    for (let i = 0; i < eps.length; i++) if (recentWatched[eps[i].id]) return true;
    return false;
  }

  // 题材/年份过滤（类型由调用方各自处理：shelf 套在 8 条规则后，浏览层按排显隐）
  function passGenreYear(e) {
    if (flt.genre && (e.genres || '').split(',').indexOf(flt.genre) < 0) return false;
    if (flt.year && e.year !== flt.year) return false;
    return true;
  }

  // 录入时间倒序取最新 8 条（剔除近一周看过的），再套 类型/题材/年份 过滤
  function shelfItems() {
    const items = lib().filter((e) => !isRecentWatched(e));
    items.sort((a, b) => (b.added || 0) - (a.added || 0));
    return items.slice(0, 8).filter((e) =>
      (!flt.kind || e.kind === flt.kind) && passGenreYear(e));
  }

  /* ---------- 顶部过滤器：类型/题材/年份胶囊 + 纵向选项浮层 ---------- */

  // 选项清单：题材/年份从库条目实时收集（分割符与 web 端 viewer.js 同口径：逗号分割、年份倒序）
  function fltOptions(kind) {
    if (kind === 'kind') return [['', '全部'], ['movie', '电影'], ['show', '剧集']];
    const set = {};
    if (kind === 'genre') {
      lib().forEach((e) => (e.genres || '').split(',').filter(Boolean)
        .forEach((g) => { set[g] = true; }));
      return [['', '全部题材']].concat(Object.keys(set).sort().map((g) => [g, g]));
    }
    lib().forEach((e) => { if (e.year) set[e.year] = true; });
    return [['0', '全部年份']].concat(
      Object.keys(set).map(Number).sort((a, b) => b - a).map((y) => [String(y), String(y)]));
  }

  function fltCapsuleText(kind) {
    if (kind === 'kind') return '类型：' + (flt.kind === 'movie' ? '电影' : flt.kind === 'show' ? '剧集' : '全部');
    if (kind === 'genre') return '题材：' + (flt.genre || '全部');
    return '年份：' + (flt.year || '全部');
  }

  function updateCapsules() {
    $all('.tv-filter', home).forEach((c) => {
      const k = c.dataset.flt;
      // 不下拼 ▾ 字符：电视字体缺这个字形会显示方框 x，下拉指示用 CSS 三角（::after）
      c.textContent = fltCapsuleText(k);
      c.classList.toggle('on',
        k === 'kind' ? !!flt.kind : k === 'genre' ? !!flt.genre : !!flt.year);
    });
  }

  // 选项浮层：胶囊正下方的纵向列表；打开时焦点锁在浮层内（见 currentItems），
  // 方向键上下移动、确认选择、返回关闭
  let fltPopOpen = false;
  let fltPopKind = null;

  function openFltPop(kind) {
    if (fltPopOpen || !home) return;
    fltPopKind = kind;
    const pop = $('#tvFltPop', home);
    const curVal = String(flt[kind]);
    pop.innerHTML = fltOptions(kind).map((o) =>
      '<div class="fp-opt' + (String(o[0]) === curVal ? ' on' : '') +
      '" data-val="' + esc(o[0]) + '">' + esc(o[1]) + '</div>'
    ).join('');
    // 定位到对应胶囊正下方
    const cap = $('.tv-filter[data-flt="' + kind + '"]', home);
    const sr = $('#tvStage', home).getBoundingClientRect();
    const cr = cap.getBoundingClientRect();
    pop.style.left = Math.max(0, cr.left - sr.left) + 'px';
    pop.style.top = (cr.bottom - sr.top + 6) + 'px';
    fltPopOpen = true;
    pop.classList.add('on');
    void pop.offsetWidth;
    pop.classList.add('in');
    $all('.fp-opt', pop).forEach((o) => o.addEventListener('click', () => applyFlt(o.dataset.val)));
    setFocus($('.fp-opt.on', pop) || $('.fp-opt', pop));
  }

  function closeFltPop() {
    if (!fltPopOpen) return;
    fltPopOpen = false;
    const pop = $('#tvFltPop', home);
    pop.classList.remove('in');
    setTimeout(() => { if (!fltPopOpen) pop.classList.remove('on'); }, 240);
    setFocus($('.tv-filter[data-flt="' + fltPopKind + '"]', home));
  }

  function applyFlt(val) {
    if (fltPopKind === 'year') flt.year = +val || 0;
    else flt[fltPopKind] = val;
    updateCapsules();
    closeFltPop();
    renderShelf();
    rebuildBrowse(); // 浏览层开着时重建两排（焦点保持不飞出）
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

  // 跨设备续播：服务端进度（秒→毫秒转 pos_<id>）与本地原生 map 按 key 取 max 合并
  function mergeResume(local, srv) {
    const out = {};
    Object.keys(local).forEach((k) => { out[k] = local[k]; });
    Object.keys(srv || {}).forEach((id) => {
      const k = 'pos_' + id;
      const ms = Math.floor((Number(srv[id]) || 0) * 1000);
      if (ms > 0 && ms > (out[k] || 0)) out[k] = ms;
    });
    return out;
  }

  function renderRail() {
    const list = $('.rail-list', home);
    if (!list) return;
    // 先拉服务端进度合并再渲染；失败退回本地 map（原行为）
    fetch('/api/progress').then((r) => (r.ok ? r.json() : {})).then((m) => {
      renderRailWith(mergeResume(resumeMap(), m));
    }).catch(() => renderRailWith(resumeMap()));
  }

  function renderRailWith(resume) {
    const list = $('.rail-list', home);
    if (!list) return;
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

  /* ---------- 海报卡：底部最新行与浏览层共用 ---------- */

  function tcardHtml(e) {
    return '<div class="tcard" data-key="' + esc(e.key) + '">' +
      '<div class="tp-wrap"><img loading="lazy" src="' +
      (e.poster ? '/api/poster/' + encodeURIComponent(e.poster) : '/api/poster/_none') + '">' +
      (e.kind === 'show' ? '<span class="tbadge">' + e.count + '集</span>' : '') +
      '</div><div class="ttitle">' + esc(e.name_cn || e.title) + '</div></div>';
  }

  function bindTcards(root) {
    $all('.tcard', root).forEach((c) => c.addEventListener('click', () => openDetail(c.dataset.key)));
  }

  /* ---------- 底部海报行 ---------- */

  function renderShelf(after) {
    const shelf = $('#tvShelf', home);
    if (!shelf) return;
    loadRecentWatched(() => {
      shelf.innerHTML = shelfItems().map(tcardHtml).join('');
      bindTcards(shelf);
      if (after) after();
    });
  }

  /* ---------- 浏览层：两排（电影/剧集）全量分类浏览，浮在 Hero 上 ---------- */

  let browseOpen = false;
  let lastShelfKey = null; // 打开浏览层前 shelf 上的焦点卡，关闭后焦点回到它

  // 题材/年份过滤两排都生效；类型过滤在 buildBrowseRows 里按排显隐
  function browseItems(kind) {
    return lib().filter((e) => e.kind === kind && passGenreYear(e))
      .sort((a, b) => (b.added || 0) - (a.added || 0));
  }

  function buildBrowseRows() {
    const b = $('#tvBrowse', home);
    b.innerHTML = [['movie', '电影'], ['show', '剧集']].map((r) =>
      '<div class="br-row"' + (flt.kind && flt.kind !== r[0] ? ' style="display:none"' : '') + '>' +
      '<div class="br-title">' + r[1] + '</div>' +
      '<div class="br-strip">' + browseItems(r[0]).map(tcardHtml).join('') + '</div></div>'
    ).join('');
    bindTcards(b);
  }

  // 第一条可见排的第一张卡（类型过滤可能藏掉电影排）
  function firstBrowseCard() {
    const rows = $all('.br-row', $('#tvBrowse', home)).filter(isVis);
    let target = null;
    for (let i = 0; i < rows.length && !target; i++) target = $('.tcard', rows[i]);
    return target;
  }

  function openBrowse() {
    if (browseOpen || !home) return;
    const cur = focused();
    lastShelfKey = (cur && cur.dataset) ? cur.dataset.key : null;
    browseOpen = true;
    buildBrowseRows();
    const b = $('#tvBrowse', home);
    $('#tvStage', home).classList.add('browse-open');
    // 滑动渐显进场：先 display:block 占位，强制 reflow 后再加终态类触发 transition
    b.classList.add('on');
    void b.offsetWidth;
    b.classList.add('in');
    // 焦点固定落在第一排第一张卡（hero 背景随之切到该片）
    setFocus(firstBrowseCard());
  }

  // 过滤器改变时重建两排：优先焦点留在同 key 卡，卡没了回第一排第一张
  function rebuildBrowse() {
    if (!browseOpen || !home) return;
    const cur = focused();
    const key = cur && cur.dataset ? cur.dataset.key : null;
    buildBrowseRows();
    let target = key ? $('.tcard[data-key="' + key + '"]', $('#tvBrowse', home)) : null;
    if (!target || !isVis(target)) target = firstBrowseCard();
    setFocus(target);
  }

  function closeBrowse() {
    if (!browseOpen || !home) return;
    browseOpen = false;
    $('#tvStage', home).classList.remove('browse-open');
    // 反向播放进场动画（下滑+淡出），动画结束后再 display:none
    const b = $('#tvBrowse', home);
    b.classList.remove('in');
    setTimeout(() => { if (!browseOpen) b.classList.remove('on'); }, 360);
    const shelf = $('#tvShelf', home);
    let target = lastShelfKey && shelf ? $('.tcard[data-key="' + lastShelfKey + '"]', shelf) : null;
    if (!target && shelf) target = $('.tcard', shelf);
    setFocus(target);
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
      // 过滤器浮层打开时只导航浮层选项
      if (fltPopOpen) return $all('.fp-opt', $('#tvFltPop', home)).filter(isVis);
      // 浏览层打开时只导航两排卡片（tabs/播放钮此时不可见，不参与焦点）
      if (browseOpen) return $all('.tcard', $('#tvBrowse', home)).filter(isVis);
      return $all('.rail-row, .tv-tab, .hc-watch, .tcard', home).filter(isVis);
    }
    return $all('#libGrid .card, .toolbar select, .toolbar input').filter(isVis);
  }

  /* ---------- 海报行横向滚动：手动算 scrollLeft + JS 缓动 ----------
     （Chrome 66 对 scrollIntoView 的 {inline:'nearest'} 选项支持不全，
       且它会把所有可滚动祖先一起滚，容易把页面带偏） */

  let shelfAnim = null;
  function scrollShelfTo(el) {
    // 泛化：滚动卡片所在的行容器（底部最新行 #tvShelf 或浏览层的 .br-strip）
    const shelf = el.closest('#tvShelf, .br-strip');
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
    // 焦点在底部最新行的海报上按「下」→ 打开浏览层
    if (dir === 'down' && home && !browseOpen) {
      const cur = focused();
      if (cur && cur.classList.contains('tcard') && cur.closest('#tvShelf')) {
        openBrowse();
        return;
      }
    }
    // 浏览层顶排（第一条可见排；类型过滤可能藏掉电影排）再按「上」→ 反向动画收层
    if (dir === 'up' && home && browseOpen) {
      const cur = focused();
      const b = $('#tvBrowse', home);
      const firstRow = b ? $all('.br-row', b).filter(isVis)[0] : null;
      if (cur && firstRow && cur.closest('.br-row') === firstRow) {
        closeBrowse();
        return;
      }
    }
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
    if (fltPopOpen) { closeFltPop(); return true; }
    const player = $('#playerMask');
    if (isVis(player) && typeof closePlayer === 'function') { closePlayer(); return true; }
    const masks = $all('.modal-mask').filter(isVis);
    if (masks.length) { masks[masks.length - 1].classList.add('hidden'); return true; }
    if (browseOpen) { closeBrowse(); return true; }
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
