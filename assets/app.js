/* 二十四时小路电台
 * 频道位置 = ((now - epoch) + drift) mod cycleLength
 * 节目单是「种子 + 时间」的纯函数，任何时刻打开都会落到同一个位置。
 */
(function () {
  'use strict';

  var CFG = {
    epoch: Date.parse('2026-09-25T00:00:00+08:00') / 1000,
    seed: 20260925,
    pageSize: 20,
    tickMs: 1000
  };

  var CAT_ORDER = ['唱歌', '杂谈', '游戏', '联动', '电台', '特别回', '其他'];
  // 歌曲类场次：画面没读到文字时，按段长估算「约 N 首」才有意义；
  // 游戏/杂谈/联动 没有歌，只能按「第 N 段」定位。
  var MUSIC_CATS = { '唱歌': 1, '电台': 1 };
  /* ---------- 多主播：当前正在看谁 ----------
     所有「按主播区分」的接口都靠 stq() 自动带上 station 参数，切换器改一次 ST，
     清单 / 分段 / 直播 / 弹幕就整体跟着切。这里包装 window.fetch 一次覆盖全部调用，
     比逐个去改 URL 更不容易漏（漏一个就会出现「界面上是小路、数据是别人」的串台）。
     ST 为空 = 不带参数 = 走主站，正好是「主网站保持原样」的默认行为。 */
  /* ---------- 在看谁：可以是多位 ----------
     顶栏那排色灯点亮谁就读谁的内容；点亮多位 = 把他们的回放合并成一条时间轴
     （媒体接口只认 bvid/cid，与板块无关，所以合并只发生在「清单 + 分段」两处）。
     存 xl_stations（JSON 数组）；旧版的单选 xl_station 读一次做迁移。
     ST 仍是「主位」= 数组第一个 —— 直播间、弹幕、状态浮标、发弹幕这些
     有明确指向的功能都按主位工作，避免「看着 A 却把弹幕发给了 B」。 */
  var ST_KEY = 'xl_station';
  var ST_SET_KEY = 'xl_stations';
  var MAIN_KEY = 'xl_main_id';
  var ST_SET = (function () {
    try {
      var raw = localStorage.getItem(ST_SET_KEY);
      if (raw) {
        var arr = JSON.parse(raw);
        if (Array.isArray(arr)) {
          return arr.filter(function (x) { return typeof x === 'string' && x; });
        }
      }
      var one = localStorage.getItem(ST_KEY);      // 旧版单选 → 数组
      if (one) return [one];
    } catch (e) { /* 隐私模式 */ }
    return [];
  })();
  var ST = ST_SET.length ? ST_SET[0] : '';
  /* 主站 id：启动早期（板块列表还没拉回来）就要判断「是不是只有主站」，
     所以缓存一份。拿不到时保守当成「不是纯主站」—— 最坏也只是多走一次接口。 */
  var MAIN_ID = (function () {
    try { return localStorage.getItem(MAIN_KEY) || ''; } catch (e) { return ''; }
  })();

  function findStation(id) {
    var hit = null;
    (STATIONS || []).forEach(function (s) { if (s.id === id) hit = s; });
    return hit;
  }

  // 是否在混合播放（多位）。列表里的来源色点只在混合时出现 —— 单选时界面保持原样。
  function isMixed() { return ST_SET.length > 1; }

  /* 是否「只有主站」——决定能不能用页面内置的离线快照秒开。
     多选时那份快照（只有主站内容）绝不能用，否则会把别人的内容替换掉。 */
  function isMainOnly() {
    if (!ST_SET.length) return true;
    if (ST_SET.length > 1) return false;
    return MAIN_ID ? ST_SET[0] === MAIN_ID : false;
  }

  function saveStations() {
    try {
      if (ST_SET.length) localStorage.setItem(ST_SET_KEY, JSON.stringify(ST_SET));
      else localStorage.removeItem(ST_SET_KEY);
      if (ST_SET.length === 1) localStorage.setItem(ST_KEY, ST_SET[0]);
      else localStorage.removeItem(ST_KEY);
    } catch (e) { /* 隐私模式 */ }
  }
  /* 哪些请求要带上 ?station= —— 也就是服务端**按板块**取数据的那些接口。
     判断依据是服务端实现里用没用 cur_station()，不是「看起来像不像」。
     ⚠ 新增按板块的接口时必须往这里加：漏了的后果不是报错，而是**静默地返回主站的数据**
     （微博就漏过一次 —— 切到别的板块，微博页还是四时小路的内容，界面看起来完全正常）。
     当前对应关系：
       programs(清单) / segments/*(分段) / live/*(直播) / status-board(小路状态)
       / weibo*(微博) / series(回放来源) —— 后两个是补上的。
     不带 station 的：/api/status(B站登录态)、/api/stations(主播注册表)、
       /api/img(图片代理)、/api/protocol、/api/ping —— 都是全局的。 */
  var STATION_RE = new RegExp('^/api/(' + [
    'programs',
    'segments/',
    'playurl', 'dashinfo', 'dash', 'stream',
    'live/',
    'status-board',
    'weibo',
    'series'
  ].join('|') + ')');
  /* ---------- 主题色：随「板块」（当前在看的主播）切换 ----------
     每位主播在 data/stations.json 里有一个 accent。切换时把它写进 --red / --red-soft，
     CSS 里注册过 @property，所以颜色是渐变过去的；再叠一层扫过动画。
     accent 同时缓存到 localStorage —— 下次打开时首屏就能用对颜色，
     否则会先闪一下默认红再变成目标色。 */
  var ACCENT_KEY = 'xl_accent';

  function themeHex(accent) {
    var a = String(accent || '').trim();
    if (/^#[0-9a-f]{6}$/i.test(a)) return a;
    if (/^#[0-9a-f]{3}$/i.test(a)) return '#' + a[1] + a[1] + a[2] + a[2] + a[3] + a[3];
    return '';
  }

  /* 底色/卡片底色 = 把主题色混进一点点到基础暗色里（线性插值，与 CSS 的
     color-mix(in srgb, ...) 算法一致）。比例要和 style.css 的 :root 对上：
     两边不一致的话，首屏（CSS 生效）到 JS 落地那一下会看见跳色。
     注意不能只靠 CSS 的 color-mix 跟随 --red —— 那样属性自身的指定值没变，
     transition 不一定会触发；这里显式给新值，过渡才稳。 */
  var THEME_TINTS = [
    ['--bg', '#08080a', 0.14],
    ['--panel', '#101013', 0.10],
    ['--panel-2', '#16161a', 0.09]
  ];

  function mixHex(base, accent, t) {
    var b = themeHex(base), a = themeHex(accent);
    if (!b || !a) return '';
    var out = '#';
    for (var i = 1; i <= 5; i += 2) {
      var x = parseInt(b.substr(i, 2), 16);
      var y = parseInt(a.substr(i, 2), 16);
      var v = Math.round(x + (y - x) * t);
      out += (v < 16 ? '0' : '') + v.toString(16);
    }
    return out;
  }

  /* 点亮多个板块时，把它们的主题色混成一个（红 + 黄 → 橙）。
     逐级线性插值：第 i 个色按 1/(i+1) 的权重并进去，等价于等权平均；
     和 mixHex 一样走 srgb 线性插值，颜色是可预期的，不会出现意外跳色。 */
  function mixAccents(ids) {
    var hexes = [];
    (ids || []).forEach(function (id) {
      var s = findStation(id);
      var h = s ? themeHex(s.accent) : '';
      if (h) hexes.push(h);
    });
    if (!hexes.length) return '';
    var acc = hexes[0];
    for (var i = 1; i < hexes.length; i++) {
      acc = mixHex(acc, hexes[i], 1 / (i + 1)) || acc;   // mixHex 的 t 是「新色的占比」
    }
    return acc;
  }

  /* 底色/卡片底色是否跟着主题走。
     单板块沿用老规矩：主站不上色（它那套黑红配色是特意保留的），副站按自己的色调。
     但**点亮两个及以上**时一律上色 —— 不然「混出来的颜色」在画面上根本看不见。 */
  function stationTint(ids) {
    var list = ids || [];
    if (!list.length) return false;
    if (list.length > 1) return true;
    var s = findStation(list[0]);
    return !(s && s.main);
  }

  function themeSweep(hex, x, y) {
    if (!hex) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    x = (typeof x === 'number' && isFinite(x)) ? x : vw / 2;
    y = (typeof y === 'number' && isFinite(y)) ? y : 0;
    // 半径要能盖住离它最远的那个角
    var r = Math.ceil(Math.hypot(Math.max(x, vw - x), Math.max(y, vh - y))) + 40;
    var d = document.createElement('div');
    d.className = 'theme-sweep';
    d.style.setProperty('--sweep', hex);
    d.style.setProperty('--sx', x + 'px');
    d.style.setProperty('--sy', y + 'px');
    d.style.setProperty('--sweep-r', r + 'px');
    d.innerHTML = '<i></i>';
    document.body.appendChild(d);
    requestAnimationFrame(function () { d.classList.add('on'); });
    setTimeout(function () {
      if (d.parentNode) d.parentNode.removeChild(d);
    }, 1000);
  }

  function applyTheme(accent, from) {
    var hex = themeHex(accent);
    if (!hex) return false;
    var rs = document.documentElement.style;
    rs.setProperty('--red', hex);
    rs.setProperty('--red-soft', hex + '24');      // 14% 左右，和原来的观感一致
    // 底色与卡片底色：**只有副站上色**。主站移除内联值，回到 style.css 里的原色
    // （也就是它本来那套黑红配色 —— 用户要求主站配色不要变）。
    for (var t = 0; t < THEME_TINTS.length; t++) {
      var tint = THEME_TINTS[t];
      if (!ST_TINT) {
        rs.removeProperty(tint[0]);
        continue;
      }
      var mixed = mixHex(tint[1], hex, tint[2]);
      if (mixed) rs.setProperty(tint[0], mixed);
    }
    try { localStorage.setItem(ACCENT_KEY, hex); } catch (e) { /* 隐私模式 */ }
    // 顶栏那排灯的颜色来自各板块自己的 accent（inline --lc），不跟着当前主题走，
    // 否则「点亮谁」就看不出来了 —— 所以这里不再需要刷新圆点颜色。
    if (from) themeSweep(hex, from.x, from.y);
    return true;
  }

  // 首屏立刻套用上次记住的颜色（不等 /api/stations 回来）
  try { applyTheme(localStorage.getItem(ACCENT_KEY) || ''); } catch (e) { /* 忽略 */ }

  function stq(u) {
    if (!u || !STATION_RE.test(u) || u.indexOf('station=') >= 0) return u;
    return u + (u.indexOf('?') >= 0 ? '&' : '?') + 'station=' + encodeURIComponent(ST);
  }

  (function () {
    var native = window.fetch.bind(window);
    window.fetch = function (u, o) {
      return native(typeof u === "string" ? stq(u) : u, o);
    };
  })();

  var VIEWS = ['live', 'multi', 'broadcast', 'schedule', 'categories', 'weibo', 'about', 'settings'];
  var KEY_MUTED = 'xl_muted';

  var store = {
    get: function (k, d) {
      try {
        var v = localStorage.getItem(k);
        return v === null ? d : JSON.parse(v);
      } catch (e) { return d; }
    },
    set: function (k, v) {
      try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* 隐私模式等，忽略 */ }
    }
  };

  var state = {
    all: [],
    meta: null,
    cats: {},              // 选中分类；空对象 = 全部
    q: '',
    sort: 'date',
    page: 1,
    cycle: null,
    drift: 0,              // 相对「直播中」的偏移（秒）
    mutedDefault: store.get(KEY_MUTED, true),
    muted: true,
    segIndex: -1,
    playingKey: null,
    view: 'live',
    horizon: 86400,        // 节目单视界（秒）；0 = 整个循环
    catFocus: null,
    skip: store.get('xl_skip', false),      // 跳过空白片段
    marking: false,
    chapKey: '',                            // 当前片段标识，用于避免每秒重渲染
    qn: store.get('xl_qn', 80),             // 请求的清晰度；B 站会按登录态降级到最高可用
    media: '',                              // 当前媒体地址
    mediaBase: null,                        // 媒体时间轴上的锚点
    cycleBase: 0,                           // 锚点对应的频道位置
    loadingKey: '',                         // 防止过期的取址结果覆盖新播放
    loading: false,                         // 取流/定位中
    wantPos: null,                          // 本次加载期望的频道位置（视频就绪前的回退）
    offline: false,                         // 本机服务不可用
    retried: {},                            // 已重试过的单元，避免出错循环
    marks: store.get('xl_marks', [])        // 人工标注的点位
  };

  var el = {};

  /* ---------------------------------------------------------- 工具 */

  function now() { return Date.now() / 1000; }

  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    var mm = (h ? (m < 10 ? '0' : '') + m : m) + ':' + (s < 10 ? '0' : '') + s;
    return h ? h + ':' + mm : mm;
  }

  function fmtDur(sec) {
    return sec >= 3600 ? (sec / 3600).toFixed(1) + 'h' : Math.round(sec / 60) + 'm';
  }

  function fmtNum(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function pad2(n) { return n < 10 ? '0' + n : '' + n; }

  function hhmm(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }

  var WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function dayLabel(d) {
    var today = new Date();
    var d0 = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    var dd = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var diff = Math.round((dd - d0) / 86400000);
    var tail = pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + WEEK[d.getDay()];
    if (diff === 0) return '今天 · ' + tail;
    if (diff === 1) return '明天 · ' + tail;
    if (diff === -1) return '昨天 · ' + tail;
    return tail;
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  function emptyRow(cols, text) {
    return '<tr><td colspan="' + cols + '" class="empty">' + text + '</td></tr>';
  }

  /* ---------------------------------------------------------- 片段 */

  // 人工标注的片段，按 cid 索引（data/segments.js）
  function segmentsOf(cid) {
    var all = window.SEGMENTS || {};
    var list = all[String(cid)];
    return (list && list.length) ? list : null;
  }

  // 开启「跳过空白」且该分P 有标注时，只播标注片段；否则整段照常播
  // segments.js 里写的是 start/end，这里换算成 start/duration 并丢弃非法项
  function effectiveUnits(part) {
    var segs = state.skip ? segmentsOf(part.cid) : null;
    if (!segs) return [{ start: 0, duration: part.duration, label: '' }];
    var units = segs
      .filter(function (s) { return s && typeof s.start === 'number' && s.end > s.start; })
      .sort(function (a, b) { return a.start - b.start; })
      .map(function (s) {
        return {
          start: Math.max(0, Math.min(s.start, part.duration)),
          duration: Math.min(s.end, part.duration) - Math.max(0, s.start),
          label: s.label || ''
        };
      });
    return units.length ? units : [{ start: 0, duration: part.duration, label: '' }];
  }

  /* ---------------------------------------------------------- 时间轴 */

  // 确定性洗牌 + 修复（避免相邻节目同类），保证同一份输入永远得到同一条时间轴
  function buildCycle(programs) {
    var rng = mulberry32(CFG.seed);
    var arr = programs.slice();
    var i, j, tmp;

    for (i = arr.length - 1; i > 0; i--) {
      j = Math.floor(rng() * (i + 1));
      tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    }
    for (i = 1; i < arr.length; i++) {
      if (arr[i].category === arr[i - 1].category) {
        for (j = i + 1; j < arr.length; j++) {
          if (arr[j].category !== arr[i - 1].category) {
            tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
            break;
          }
        }
      }
    }

    var segments = [];
    var acc = 0;
    arr.forEach(function (p) {
      p.parts.forEach(function (part) {
        effectiveUnits(part).forEach(function (u) {
          segments.push({
            bvid: p.bvid,
            page: part.page,
            cid: part.cid,
            t0: u.start,                 // 该分P 内的起始秒数
            duration: u.duration,
            label: u.label || '',
            start: acc,                  // 时间轴上的起点
            program: p
          });
          acc += u.duration;
        });
      });
    });
    return { order: arr, segments: segments, total: acc };
  }

  // 频道位置。媒体已加载时以 <video> 的播放位置为准——暂停、缓冲、拖动都会如实反映；
  // 否则回退到挂钟时间。这样进度条与画面永远一致，也不会因为暂停而漂移。
  function cyclePos() {
    var total = state.cycle.total;
    var p;
    if (state.mediaBase !== null && el.player && el.player.readyState > 0) {
      p = state.cycleBase + (el.player.currentTime - state.mediaBase);
    } else if (state.wantPos !== null) {
      p = state.wantPos;          // 视频还没就绪，先用期望位置，避免跳回挂钟造成错位
    } else {
      p = (now() - CFG.epoch) + state.drift;
    }
    p = p % total;
    return p < 0 ? p + total : p;
  }

  function findSeg(pos) {
    var segs = state.cycle.segments;
    for (var i = 0; i < segs.length; i++) {
      if (pos < segs[i].start + segs[i].duration) return i;
    }
    return segs.length - 1;
  }

  function rebuildCycle(soft) {
    var pool = state.all.filter(function (p) {
      return !Object.keys(state.cats).length || state.cats[p.category];
    });
    state.cycle = buildCycle(pool);
    // soft：只是后台抓到新清单后换一份，播放中的那一段不能被打断
    // （drift / segIndex / playingKey 保持原样，tick 会自己判断要不要换段）
    if (soft) return;
    state.drift = 0;
    state.segIndex = -1;
    state.playingKey = null;
    // 直播间页正占着播放器放直播流，回放不能来抢（boot 完成时的这次调用尤其关键，
    // 否则刚接上去的直播流会被回放覆盖）。离开时 resumeReplay() 会重新拉起回放。
    if (state.view === 'broadcast') return;
    if (state.cycle.total > 0) applyPlayer(true);
    else renderIdle();
  }

  /* ---------------------------------------------------------- 播放器 */

  function b64url(s) {
    return btoa(unescape(encodeURIComponent(s)))
      .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function setQualityList(list, cur) {
    if (!list || !list.length) return;
    el.quality.disabled = false;      // 直播间里会被占位禁用，回放取到档位时要恢复
    el.quality.innerHTML = list.map(function (q) {
      return '<option value="' + q.qn + '"' + (q.qn === cur ? ' selected' : '') + '>'
        + q.desc + '</option>';
    }).join('');
  }

  // MP4（durl）通道：实现简单，但**封顶 720P**，只作为 DASH 失败时的兜底。
  function loadMediaMp4(seg, seekTo, anchor) {
    if (state.offline) return;
    var key = seg.bvid + '#' + seg.page + '#' + state.qn;
    state.loadingKey = key;
    state.loading = true;
    fetch('/api/playurl?bvid=' + encodeURIComponent(seg.bvid)
          + '&cid=' + seg.cid + '&qn=' + state.qn)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (state.loadingKey !== key) return;          // 期间又切走了，丢弃这次结果
        if (d.error) {
          state.loading = false;
          el.npMeta.textContent = '播放地址获取失败：' + d.error;
          netFail();
          return;
        }
        state.qn = d.quality;                          // B 站实际下发的清晰度
        setQualityList(d.accept, d.quality);
        state.media = d.media;
        var total = state.cycle.total;
        var base = (anchor === undefined || anchor === null)
          ? (((now() - CFG.epoch) + state.drift) % total + total) % total
          : ((anchor % total) + total) % total;
        state.cycleBase = base;
        state.mediaBase = seekTo;
        state.wantPos = base;
        el.player.muted = state.muted;
        // 用 media fragment 让浏览器「加载时就定位」，比事后 seek 可靠得多
        el.player.src = '/api/stream?u=' + b64url(d.media)
          + '#t=' + Math.max(0, Math.floor(seekTo));
        // 先定位、等 seek 真正完成再播：否则 play() 会被随后的 seek 打断，
        // 表现为「切完清晰度停在 0 秒不动」。
        var done = false;
        var finish = function () {
          if (done) return;
          done = true;
          state.loading = false;
          el.player.play().catch(function () { /* 浏览器可能要求手势 */ });
        };
        var onReady = function () {
          el.player.removeEventListener('loadedmetadata', onReady);
          try { el.player.currentTime = seekTo; } catch (e) { /* 忽略 */ }
          el.player.addEventListener('seeked', function onSeeked() {
            el.player.removeEventListener('seeked', onSeeked);
            finish();
          });
          setTimeout(finish, 4000);        // 兜底：seeked 没来也要能播
        };
        el.player.addEventListener('loadedmetadata', onReady);
        setTimeout(function () { state.loading = false; }, 10000);   // 兜底，别卡死
      })
      .catch(function (e) {
        state.loading = false;
        el.npMeta.textContent = '取播放地址出错：' + e.message;
        netFail();
      });
  }

  var dashPlayer = null;

  function destroyDash() {
    if (dashPlayer) {
      try { dashPlayer.reset(); } catch (e) { /* 忽略 */ }
      dashPlayer = null;
    }
  }

  // 播放：优先 DASH（1080P 只存在于 DASH 通道），失败再退回 MP4。
  var prefetched = {};

  // 提前把下一段的 MPD 拉热（服务端会缓存）：切段时省掉一次 B站往返（实测约 140ms）。
  // 只预热「下一段」——当前段正在被 dash.js 取，重复请求反而多走一趟 B站。
  function prefetchSegment(seg) {
    if (!seg || typeof dashjs === 'undefined' || state.offline) return;
    var key = seg.bvid + '#' + seg.page + '#' + state.qn;
    if (prefetched[key]) return;
    prefetched[key] = 1;
    fetch('/api/dash?bvid=' + encodeURIComponent(seg.bvid) + '&cid=' + seg.cid
          + '&qn=' + state.qn).catch(function () { /* 预取出错无所谓 */ });
    fetch('/api/dashinfo?bvid=' + encodeURIComponent(seg.bvid) + '&cid=' + seg.cid)
      .catch(function () { /* 同上 */ });
  }

  function loadMedia(seg, seekTo, anchor) {
    if (state.offline) return;
    var key = seg.bvid + '#' + seg.page + '#' + state.qn;
    state.loadingKey = key;
    state.loading = true;

    var total = state.cycle.total;
    var base = (anchor === undefined || anchor === null)
      ? (((now() - CFG.epoch) + state.drift) % total + total) % total
      : ((anchor % total) + total) % total;

    // 清晰度阶梯：只有 DASH 接口才知道有没有 1080P
    fetch('/api/dashinfo?bvid=' + encodeURIComponent(seg.bvid) + '&cid=' + seg.cid)
      .then(function (r) { return r.json(); })
      .then(function (info) {
        if (!info || !info.accept || !info.accept.length) return;
        var cur = 0;
        info.accept.forEach(function (x) { if (x.qn <= state.qn && x.qn > cur) cur = x.qn; });
        setQualityList(info.accept, cur || info.accept[0].qn);
      }).catch(function () { /* 忽略，不影响播放 */ });

    if (typeof dashjs === 'undefined') { loadMediaMp4(seg, seekTo, base); return; }

    destroyDash();
    var mpd = '/api/dash?bvid=' + encodeURIComponent(seg.bvid)
            + '&cid=' + seg.cid + '&qn=' + state.qn;

    function ok() {
      state.cycleBase = base;
      state.mediaBase = seekTo;
      state.wantPos = base;
      el.player.muted = state.muted;
      try { el.player.currentTime = seekTo; } catch (e) { /* 忽略 */ }
      el.player.play().catch(function () { /* 浏览器可能要求手势 */ });
      state.loading = false;
    }
    function fallback() {
      state.loading = false;
      el.npMeta.textContent = 'DASH 不可用，已退回 MP4 通道（最高 720P）';
      loadMediaMp4(seg, seekTo, base);
    }

    dashPlayer = dashjs.MediaPlayer().create();
    // 只留这个版本真正认得的项：stableBufferTime（v3 的名字）在本包的 dash.js 里
    // 已不存在，dash.js 会每次创建播放器都打一条 console.error 并忽略它。
    // 缓冲区目标不在这里改 —— 当前 v4 默认（长片 60 秒）比原先写的 12 秒更抗网络抖动。
    dashPlayer.updateSettings({
      debug: { logLevel: dashjs.Debug.LOG_LEVEL_NONE },
      streaming: { buffer: { fastSwitchEnabled: false } }
      // 试过把 abr.initialBitrate 设成最低档来「快速起播」：档位确实降下来了
      // （视频尺寸先 640x360 再升到 1920x1080），但 playing 反而从 389ms 拖到 722ms ——
      // dash.js 起播后立刻升档，把出画时间推后了。实测有害，故不设。
      // 服务端 mpd 里仍然写整条阶梯：它的价值在**网络变差时 ABR 能降档**，而不是起播。
    });
    dashPlayer.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, function () {
      if (state.loadingKey !== key) return;
      ok();
    });
    dashPlayer.on(dashjs.MediaPlayer.events.ERROR, function () {
      if (state.loadingKey !== key) return;
      fallback();
    });
    try {
      dashPlayer.initialize(el.player, mpd, false);
    } catch (e) {
      fallback();
      return;
    }
    setTimeout(function () { if (state.loadingKey === key) state.loading = false; }, 15000);
  }

  function applyPlayer(force) {
    // 直播间页只放实时直播：回放引擎在此时一律不许碰播放器。
    // 这里是唯一的咽喉 —— 扫码登录成功、切回标签页（visibilitychange）、
    // 播放出错后的重试定时器、退出登录、点列表换歌，都会绕到这儿来，
    // 不在这里拦，直播间就会被换成回放画面。离开直播间时 resumeReplay()
    // 是在 state.view 已经切走之后调用的，所以正常恢复不受影响。
    if (state.view === 'broadcast') return;
    if (!state.cycle || !state.cycle.segments.length) return;

    var pos = cyclePos();
    var i = findSeg(pos);
    var seg = state.cycle.segments[i];
    var offset = pos - seg.start;
    var key = seg.bvid + '#' + seg.page;

    if (force || key !== state.playingKey) {
      state.playingKey = key;
      loadMedia(seg, seg.t0 + offset);
    }
    state.segIndex = i;
    paintNowPlaying(seg, offset);
    markPlayingRow(seg.bvid);
  }

  function paintNowPlaying(seg, offset) {
    var p = seg.program;
    el.npCat.textContent = p.category + ' · ' + p.date;
    el.npTitle.textContent = p.title + (p.parts.length > 1
      ? '（分P ' + seg.page + ' / ' + p.parts.length + '）' : '');
    el.pvTitle.textContent = p.title + (p.parts.length > 1 ? '（分P ' + seg.page + '）' : '');
    el.npMeta.textContent = (seg.label ? '片段「' + seg.label + '」 · ' : '')
      + '共 ' + p.parts.length + ' 个分P · '
      + fmtDur(p.duration) + ' · 弹幕 ' + fmtNum(p.dm_total)
      + ' · 播放 ' + fmtNum(p.view);
    el.npBar.style.width = Math.min(100, offset / seg.duration * 100) + '%';
    el.npPos.textContent = fmtClock(offset);
    el.npDur.textContent = fmtClock(seg.duration);
    el.liveBadge.textContent = state.drift === 0 ? '直播中' : '单集播放';
    el.liveBadge.className = 'badge' + (state.drift === 0 ? '' : ' off');
    renderNext(seg);
    renderChapters();
    renderUpNext();
    state.chapKey = seg.bvid + '#' + seg.page + '#' + chapterIndexNow();
  }

  function renderNext(current) {
    if (!state.cycle || !state.cycle.segments.length) return;
    var segs = state.cycle.segments;
    var seen = {};
    var out = [];
    var i = current ? state.segIndex : 0;

    if (current) seen[current.bvid] = 1;
    for (var k = i + 1; k < segs.length + i && out.length < 3; k++) {
      var s = segs[k % segs.length];
      if (seen[s.bvid]) continue;
      seen[s.bvid] = 1;
      out.push(s.program);
    }

    el.upnext.innerHTML = out.map(function (p) {
      return '<li>' + esc(p.title) + ' <span>· ' + fmtDur(p.duration)
        + ' · ' + p.category + '</span></li>';
    }).join('') || '<li>—</li>';
  }

  function markPlayingRow(bvid) {
    var rows = el.rows.querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
      rows[i].className = rows[i].getAttribute('data-bvid') === bvid ? 'playing' : '';
    }
  }

  function renderIdle() {
    el.npCat.textContent = '—';
    el.npTitle.textContent = '当前筛选下没有可播放内容';
    el.npMeta.textContent = '请至少选择一个分类';
    el.npBar.style.width = '0%';
    el.npPos.textContent = '--:--';
    el.npDur.textContent = '--:--';
    el.player.removeAttribute('src');
  }

  /* ---------------------------------------------------------- 列表 */

  // 缩略图一律经本机代理取：部分网络下浏览器直连 i2.hdslb.com 会失败。
  // 尺寸用更小的变体，配合 srcset 让浏览器按设备像素比自选。
  // 取图失败要看得见：出网不通时缩略图和视频会同时失效，光靠「空白图 + 点不动」无从判断。
  // 个别地址失效是常事，累计到 3 次才提示，避免误报。
  var netFails = 0;

  function netFail() {
    netFails++;
    if (!el.netAlert || netFails < 3 || !el.netAlert.hidden) return;
    el.netAlert.hidden = false;
    el.netAlert.innerHTML = '<div><b>连不上 B 站</b>：缩略图与视频都取不到。'
      + '最常见的原因是本机开了系统代理、但代理程序没运行 —— 关掉代理后重试。</div>'
      + '<button class="btn ghost retry" id="net-retry">重新载入</button>';
    document.getElementById('net-retry').onclick = function () { location.reload(); };
  }

  function thumbSrc(p, size) {
    // size 是完整后缀（如 '240w_150h_1c.webp'），与主列表调用约定一致
    return '/api/img?u=' + b64url(p.thumb.replace('@320w_200h_1c.webp', '@' + size));
  }

  /* 列表里的「这条是谁的」小色点。只在混合播放时出现 ——
     单选/主站时界面保持原样，一个字都不多。 */
  function stMark(p) {
    if (!isMixed() || !p._short) return '';
    return '<span class="st-mark" style="--lc:' + esc(p._accent || '#8a8a95')
      + '" title="来自 ' + esc(p._short) + '"></span>';
  }

  function programRow(p) {
    return '<tr data-bvid="' + p.bvid + '">'
      + '<td class="col-title"><div class="cell-title">'
      + '<img loading="lazy" decoding="async" alt="" '
             + 'src="' + esc(thumbSrc(p, '160w_100h_1c.webp')) + '" '
             + 'srcset="' + esc(thumbSrc(p, '160w_100h_1c.webp')) + ' 1x, '
             + esc(thumbSrc(p, '240w_150h_1c.webp')) + ' 1.5x, '
             + esc(thumbSrc(p, '320w_200h_1c.webp')) + ' 2x">'
      + '<div class="tt"><span class="n">' + stMark(p) + esc(p.title) + '</span>'
      + '<span class="d">' + p.date + ' · ' + p.parts.length + ' 个分P</span></div>'
      + '</div></td>'
      + '<td class="col-cat"><span class="tag t-' + p.category + '">' + p.category + '</span></td>'
      + '<td class="col-dur mono">' + fmtDur(p.duration) + '</td>'
      + '<td class="col-dm mono">' + fmtNum(p.dm_total) + '</td>'
      + '<td class="col-act"><button class="play-btn" data-play="' + p.bvid + '">播放</button></td>'
      + '</tr>';
  }

  function filtered() {
    var q = state.q.toLowerCase();
    var list = state.all.filter(function (p) {
      // 混合播放时可以从提示条里挑一位单独看（列表按时间倒序，某位可能排得靠后）
      if (state.onlySrc && p._sid !== state.onlySrc) return false;
      if (Object.keys(state.cats).length && !state.cats[p.category]) return false;
      if (q && (p.title + ' ' + p.date + ' ' + p.category).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
    list.sort(state.sort === 'score'
      ? function (a, b) { return b.score - a.score || b.pubdate - a.pubdate; }
      : function (a, b) { return b.pubdate - a.pubdate; });
    return list;
  }

  function renderList() {
    renderMixedNote();          // 提示条里的「共 N 个节目」跟着清单走
    var list = filtered();
    var pages = Math.max(1, Math.ceil(list.length / CFG.pageSize));
    if (state.page > pages) state.page = pages;

    var start = (state.page - 1) * CFG.pageSize;
    var slice = list.slice(start, start + CFG.pageSize);

    el.rows.innerHTML = slice.length
      ? slice.map(programRow).join('')
      : emptyRow(5, '没有匹配的节目');

    var parts = list.reduce(function (n, p) { return n + p.parts.length; }, 0);
    var hours = list.reduce(function (n, p) { return n + p.duration; }, 0) / 3600;
    el.stat.innerHTML = '共 <b>' + list.length + '</b> 个节目 · <b>' + parts
      + '</b> 个片段 · <b>' + hours.toFixed(1) + '</b> 小时';
    el.pageInfo.textContent = state.page + ' / ' + pages;
    el.prev.disabled = state.page <= 1;
    el.next.disabled = state.page >= pages;

    if (state.segIndex >= 0 && state.cycle.segments.length) {
      markPlayingRow(state.cycle.segments[state.segIndex].bvid);
    }
  }

  /* ---------------------------------------------------------- 节目单视图 */

  function renderSchedule() {
    if (!state.cycle || !state.cycle.segments.length) {
      el.schRows.innerHTML = emptyRow(5, '当前筛选下没有可播放内容');
      el.schStat.textContent = '—';
      return;
    }

    var segs = state.cycle.segments;
    var n = segs.length;
    var pos = cyclePos();
    var i = findSeg(pos);
    var t0 = now();
    var cur = segs[i];
    var elapsed = pos - cur.start;

    var rows = [{ seg: cur, start: t0 - elapsed, live: true }];
    var horizon = state.horizon || state.cycle.total;
    var covered = 0;
    var k = i;

    while (rows.length < n && covered < horizon) {
      k = (k + 1) % n;
      var s = segs[k];
      rows.push({ seg: s, start: t0 - elapsed + cur.duration + covered, live: false });
      covered += s.duration;
    }

    var html = '';
    var lastDay = '';
    rows.forEach(function (r) {
      var d = new Date(r.start * 1000);
      var day = d.getFullYear() + '/' + d.getMonth() + '/' + d.getDate();
      if (day !== lastDay) {
        lastDay = day;
        html += '<tr class="day-row"><td colspan="5">' + dayLabel(d) + '</td></tr>';
      }
      var p = r.seg.program;
      var partNote = p.parts.length > 1 ? ' · 分P ' + r.seg.page + '/' + p.parts.length : '';
      var timeCell = r.live
        ? '<span class="time-cell">' + hhmm(d) + '<small>直播中 · 剩 '
            + fmtClock(r.seg.duration - (t0 - r.start)) + '</small></span>'
        : '<span class="time-cell">' + hhmm(d) + '</span>';
      html += '<tr class="' + (r.live ? 'live-row' : '') + '" data-bvid="' + p.bvid + '">'
        + '<td class="col-time">' + timeCell + '</td>'
        + '<td class="col-title"><div class="cell-title">'
        + '<div class="tt"><span class="n">' + stMark(p) + esc(p.title) + '</span>'
        + '<span class="d">' + p.date + partNote + '</span></div>'
        + '</div></td>'
        + '<td class="col-cat"><span class="tag t-' + p.category + '">' + p.category + '</span></td>'
        + '<td class="col-dur mono">' + fmtDur(r.seg.duration) + '</td>'
        + '<td class="col-act"><button class="play-btn" data-play="' + p.bvid + '">播放</button></td>'
        + '</tr>';
    });
    el.schRows.innerHTML = html;

    var span = rows.reduce(function (n2, r) { return n2 + r.seg.duration; }, 0);
    el.schStat.innerHTML = '共 <b>' + rows.length + '</b> 场 · 覆盖 <b>'
      + (span / 3600).toFixed(1) + '</b> 小时 · 时间轴循环长度 <b>'
      + (state.cycle.total / 3600).toFixed(1) + '</b> 小时';
  }

  /* ---------------------------------------------------------- 分类视图 */

  function renderCategories() {
    var stats = {};
    CAT_ORDER.forEach(function (c) { stats[c] = { n: 0, dur: 0, dm: 0, top: null }; });

    state.all.forEach(function (p) {
      var s = stats[p.category];
      if (!s) return;
      s.n++;
      s.dur += p.duration;
      s.dm += p.dm_total;
      if (!s.top || p.score > s.top.score) s.top = p;
    });

    var cards = [{
      name: '全部',
      focus: null,
      s: {
        n: state.all.length,
        dur: state.all.reduce(function (a, p) { return a + p.duration; }, 0),
        dm: state.all.reduce(function (a, p) { return a + p.dm_total; }, 0),
        top: null
      }
    }];
    CAT_ORDER.forEach(function (c) {
      if (stats[c].n) cards.push({ name: c, focus: c, s: stats[c] });
    });

    el.catCards.innerHTML = cards.map(function (c) {
      var s = c.s;
      var dens = s.dur ? Math.round(s.dm / (s.dur / 3600)) : 0;
      var lines = '<b>' + s.n + '</b> 个节目 · <b>' + (s.dur / 3600).toFixed(1) + '</b> 小时'
        + '<br>均弹幕 <b>' + fmtNum(dens) + '</b> /小时';
      var top = s.top ? '<div class="cc-t">最高：' + esc(s.top.title) + '</div>' : '';
      return '<button class="cat-card' + (state.catFocus === c.focus ? ' active' : '')
        + '" data-cat-focus="' + (c.focus || '') + '">'
        + '<div class="cc-n">' + c.name + '</div>'
        + '<div class="cc-s">' + lines + '</div>' + top + '</button>';
    }).join('');

    var list = state.all.filter(function (p) {
      return !state.catFocus || p.category === state.catFocus;
    }).sort(function (a, b) { return b.score - a.score || b.pubdate - a.pubdate; });

    el.catRows.innerHTML = list.length
      ? list.map(programRow).join('')
      : emptyRow(5, '该分类下暂无节目');

    var dur = list.reduce(function (a, p) { return a + p.duration; }, 0);
    var dm = list.reduce(function (a, p) { return a + p.dm_total; }, 0);
    el.catStat.innerHTML = (state.catFocus ? '「' + state.catFocus + '」' : '全部') + '：共 <b>'
      + list.length + '</b> 个节目 · <b>' + (dur / 3600).toFixed(1) + '</b> 小时 · 弹幕 <b>'
      + fmtNum(dm) + '</b> 条（按高能排序）';
  }

  /* ---------------------------------------------------------- 关于视图 */

  function block(title, html) {
    return '<div class="about-block"><h3>' + title + '</h3>' + html + '</div>';
  }

  function renderAbout() {
    var m = state.meta || {};
    var hours = ((m.total_duration || 0) / 3600).toFixed(1);
    var dist = {};
    state.all.forEach(function (p) { dist[p.category] = (dist[p.category] || 0) + 1; });
    var distText = CAT_ORDER.filter(function (c) { return dist[c]; })
      .map(function (c) { return c + ' ' + dist[c]; }).join(' · ');

    el.aboutBody.innerHTML = [
      block('这是什么', [
        '<p>把 <a href="' + (m.up_space || '#') + '" target="_blank" rel="noopener">'
          + esc(m.up_name || '四时小路Komichi') + '</a> 的直播回放整理成一条 24 小时不间断的频道。'
          + '打开页面时你会直接落进「正在直播中」的进度，而不是从某一场的开头开始。</p>',
        '<p>本站是<b>非官方粉丝向站点</b>，与 UP 主及 B 站官方无隶属关系。'
          + '所有内容版权归 UP 主所有，播放与弹幕均由 B 站官方嵌入播放器提供。</p>'
      ].join('')),

      block('频道怎么工作', [
        '<p>节目单不是一份固定列表，而是时间的纯函数：</p>',
        '<p><code>position = ((now - epoch) + drift) mod cycleLength</code></p>',
        '<p>其中 <code>epoch</code> 固定为 2026-09-25 00:00（UTC+8），'
          + '<code>cycleLength</code> 是全部素材时长之和（' + hours + ' 小时）。'
          + '因此刷新页面、换设备打开、甚至服务重启，都会落到同一个位置——这就是「随时打开都在直播中」。</p>',
        '<p>不重复的保证：一个循环内每个片段只出现一次，而循环长度 ' + hours
          + ' 小时远大于 24 小时，所以<b>任意 24 小时内不会有任何一场重复</b>。'
          + '洗牌后还会修复相邻同类，避免连续几场都是闲聊。</p>',
        '<p>连播由页面自己的计时器驱动：节目时长是已知的，到点就重载播放器到下一段。'
          + '这也是为什么不需要服务端。</p>'
      ].join('')),

      block('播放与画质', [
        '<p>回放台用的是<b>自己的播放器</b>（原生 video），清晰度在播放器右上角的下拉框里直接切换，'
          + '不会跳去 B 站页面。视频流由本机服务 <code>tools/serve.py</code> 代取并转发——'
          + '因为浏览器无法直接播放 B 站的流（CDN 校验 Referer，网页伪造不了）。</p>',
        '<p><b>未登录最高 480P</b>（接口虽列出 1080P 档位，但未登录时只实际下发 360P/480P）；'
          + '要 1080P 需要登录。点播放器右上角「登录」，'
          + '用 B 站 App 扫码即可，<b>全程在本页完成</b>，不会离开回放台。</p>',
        '<p>登录信息只保存在本机 <code>tools/sessdata.txt</code>，只发给 B 站自己的接口，'
          + '不经任何第三方。<b>本站不收集、不上传你的账号信息。</b></p>',
        '<p><b>一个已知取舍</b>：自研播放器没有官方弹幕层，所以本页不再显示弹幕。'
          + '官方嵌入播放器有弹幕，但它没有清晰度参数、也无法在页面内控制——两者不可兼得。</p>'
      ].join('')),

      block('内容识别与排序', [
        '<ul>',
        '<li><b>L0 元数据</b>（已完成）：标题关键词分类、时长、发布时间、播放量。</li>',
        '<li><b>L1 弹幕密度</b>（已完成）：以弹幕密度为主要依据计算「精彩度」，'
          + '用于「高能」排序、随机点歌权重与排期。</li>',
        '<li><b>L2 转写与文本相似度</b>（计划）：识别跨场次重复的话题与段落。</li>',
        '<li><b>L3 音频指纹</b>（计划）：识别同一首歌、同一段子在不同场次中的重复。</li>',
        '</ul>',
        '<p>分类为标题自动打标，个别条目可能不准，可用 <code>tools/overrides.json</code> 人工校正。</p>'
      ].join('')),

      block('数据', [
        '<div class="about-kv">',
        '<div><b>' + (m.count || 0) + '</b>个节目</div>',
        '<div><b>' + (m.part_count || 0) + '</b>个片段</div>',
        '<div><b>' + hours + '</b>小时素材</div>',
        '<div><b>' + fmtNum(m.total_danmaku || 0) + '</b>条弹幕</div>',
        '</div>',
        '<p style="margin-top:14px">分类分布：' + esc(distText) + '</p>',
        '<p>数据来源：<a href="' + (m.source || '#') + '" target="_blank" rel="noopener">'
          + '直播回放系列</a> · 采集于 ' + esc(m.generated_at || '—') + '</p>'
      ].join('')),

      block('已知限制', [
        '<ul>',
        '<li><b>暂停会让位置脱离「直播中」</b>：频道位置跟随播放器推进，暂停后就不再对应墙钟时间，点「回到直播」重新对齐。</li>',
        '<li><b>页内没有弹幕轨道</b>：自托管播放器只播视频与音频，弹幕仅作为列表里的密度统计。</li>',
        '<li><b>跳转会重新缓冲</b>：跳到远处的片段需重新取流，实测约 1.4~2.1 秒。</li>',
        '<li><b>需要联网</b>：视频流与封面图来自 B 站。</li>',
        '</ul>'
      ].join('')),

      block('合规声明', [
        '<p>本站不转发视频流、不下载、不转存、不二次上传，不投放广告，不做任何商业化。'
          + '每个节目均提供指向 B 站原视频的链接。</p>',
        '<p>如权利人提出异议，可立即下线。</p>'
      ].join('')),

      block('更新数据', [
        '<p>新回放进入频道只需重跑采集脚本：</p>',
        '<p><code>python tools/collect.py</code></p>',
        '<p>加 <code>--refresh</code> 可忽略缓存全部重新采集。建议每日一次。</p>'
      ].join(''))
    ].join('');
  }

  /* ---------------------------------------------------------- 直播间 */

  var LIVE_ROOM = '1700301235';   // 与服务端 ROOM_ID 一致
  var liveInit = false;
  var wheelTimer = null;
  var liveBusy = false;
  // 常用弹幕：自定义的常用语，点一下直接发。存本机浏览器（与 xl_marks / xl_keys 同类），
  // 服务端只收最终那条文本，所以这一侧不需要任何后端改动。
  var QUICK_STORE = 'xl_quick';
  var QUICK_MAX = 12;                     // 一行放得下的上限，再多会挤成一片
  var QUICK_DEFAULT = ['丨吧宝宝'];        // 首次进入时给一条示例
  var quickEdit = false;                  // 编辑模式：点标签即删除

  function postJSON(url, obj) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(obj || {})
    }).then(function (r) { return r.json(); });
  }

  var livePlayer = null;
  var liveRetry = null;

  function destroyLive() {
    if (liveRetry) { clearTimeout(liveRetry); liveRetry = null; }
    if (livePlayer) {
      try { livePlayer.pause(); livePlayer.unload(); livePlayer.detachMediaElement(); }
      catch (e) { /* 播放器可能已经处于异常态，忽略 */ }
      livePlayer = null;
    }
    try { el.player.removeAttribute('src'); el.player.load(); } catch (e) { /* 同上 */ }
  }

  // 直播流是 HTTP-FLV，浏览器原生播不了，用 mpegts.js 接到本站同一个 <video> 上
  function startLive(qn) {
    if (typeof mpegts === 'undefined') {
      el.liveInfo.textContent = '播放库没加载（assets/mpegts.js 缺失），无法播放直播流';
      return;
    }
    destroyDash();
    destroyLive();
    livePlayer = mpegts.createPlayer({
      type: 'flv', isLive: true, url: '/api/live/stream?qn=' + (qn || 250),
      // 关掉 stash 缓冲：默认值会先攒一段再喂给 <video>，实测进直播间要多等约 1 秒。
      // 直播不需要「起播顺滑」，要的是尽快看到画面（网络抖动导致的卡顿由下面的重连兜底）。
      enableStashBuffer: false,
      liveBufferLatencyChasing: true      // 落后直播边缘时自动追帧
    });
    livePlayer.attachMediaElement(el.player);
    livePlayer.load();
    // 直播断了就清掉画面并说明，宁可显示一句话，也不要留一帧冻住的画面
    // （更不能让回放趁机接管 —— 上面 applyPlayer 的直播守卫已拦住那条路）
    livePlayer.on(mpegts.Events.ERROR, function () {
      if (state.view !== 'broadcast') return;
      liveOffline('直播连接中断，正在尝试重连…');
      liveRetry = setTimeout(function () {
        liveRetry = null;
        if (state.view === 'broadcast') loadLiveInfo();
      }, 8000);
    });
    el.player.muted = state.muted;
    // play() 返回的是 promise：浏览器要求用户手势、或在解析完成前又被 pause 打断时，
    // 拒绝是异步发生的，try/catch 抓不到，会变成控制台里的未处理拒绝。
    try {
      var p = livePlayer.play();
      if (p && typeof p.catch === 'function') p.catch(function () { /* 等用户手势 */ });
    } catch (e) { /* 浏览器可能要求用户手势 */ }
  }

  function renderBroadcast() {
    if (!liveInit) {
      liveInit = true;
      bindLive();
    }
    // 直播间里播放器控制条那条下拉是**直播画质**（原画 / 蓝光 / 超清），
    // 不是回放的分辨率 —— 先占位禁用，等 playinfo 回来再按实际档位填。
    // 不占位的话，从频道页切过来会先闪一下回放的「1080P 高清」。
    el.quality.innerHTML = '<option value="">直播画质载入中…</option>';
    el.quality.disabled = true;
    // 先把回放彻底摘掉：留着 src 会停在回放的最后一帧上，看起来就像「直播间在放回放」
    destroyDash();
    destroyLive();
    el.banner.hidden = true;
    loadLiveInfo();
    refreshLiveCred();
    pollWheel();
    if (!wheelTimer) wheelTimer = setInterval(pollWheel, 1000);
  }

  // 直播没开 / 断了：把画面清空并说明原因，不要留一帧静止画面让人误会
  function liveOffline(text) {
    destroyLive();
    el.banner.innerHTML = '<div>' + esc(text) + '</div>';
    el.banner.hidden = false;
  }

  function loadLiveInfo() {
    fetch('/api/live/playinfo').then(function (r) { return r.json(); }).then(function (d) {
      var qnList = d.qualities || [];
      // 直播画质就填在播放器控制条那条下拉里（位置与回放一致，切换直接重开直播流）。
      // 直播间不再另放一个「清晰度」—— 同一个功能摆两处只会互相打架。
      if (qnList.length) setQualityList(qnList, d.current_qn);
      if (!d.living) {
        el.liveInfo.innerHTML = '<b>未开播</b>';
        el.quality.innerHTML = '<option value="">未开播</option>';
        el.quality.disabled = true;
        chatStop();
        liveOffline(ST_SHORT + '现在没开播，这里只会显示直播画面。');
        return;
      }
      el.liveInfo.innerHTML = '<b>直播中</b> · ' + esc(d.title || '（无标题）')
        + (d.online ? ' · 人气 ' + fmtNum(d.online) : '');
      el.banner.hidden = true;
      startLive(d.current_qn);
      chatStart();
    }).catch(function () {
      el.liveInfo.textContent = '直播状态获取失败（本机服务没起？）';
    });
  }

  // 离开直播间页：把播放器还给回放频道，走的是「回到直播」按钮同一条路径
  function resumeReplay() {
    destroyLive();
    el.banner.hidden = true;
    state.drift = 0;
    state.mediaBase = null;
    applyPlayer(true);
  }

  function bindLive() {
    // 独轮车平时收着，点按钮才展开（浮层）；收起时按钮上会显示运行进度
    function showWheel(on) { el.wheelPop.hidden = !on; }
    el.wheelToggle.addEventListener('click', function () { showWheel(el.wheelPop.hidden); });
    el.wheelClose.addEventListener('click', function () { showWheel(false); });

    el.liveJctSave.addEventListener('click', function () {
      if (liveBusy) return;
      var v = el.liveJct.value.trim();
      if (!v) { el.liveCredState.textContent = 'bili_jct 不能为空'; return; }
      liveBusy = true;
      postJSON('/api/live/credential', { jct: v })
        .then(function (d) {
          if (d.error) { el.liveCredState.textContent = d.error; return; }
          el.liveJct.value = '';
          refreshLiveCred();
        })
        .catch(function () { el.liveCredState.textContent = '保存失败，请重试'; })
        .then(function () { liveBusy = false; });
    });

    function sendOnce(msg) {
      if (liveBusy) return;
      // 传参时用参数（常用弹幕一键发送走这条路，不经过输入框），不传时取输入框
      if (msg === undefined) msg = el.liveMsg.value;
      msg = String(msg).trim();
      if (!msg) { el.liveResult.textContent = '先输入弹幕内容'; return; }
      liveBusy = true;
      el.liveSend.disabled = true;
      postJSON('/api/live/send', { msg: msg })
        .then(function (d) {
          el.liveResult.textContent = d.error ? d.error
            : (d.code === 0 ? '已发送 ✓' : 'B 站返回：' + (d.message || d.code));
        })
        .catch(function () { el.liveResult.textContent = '发送失败，请重试'; })
        .then(function () { liveBusy = false; el.liveSend.disabled = false; });
    }
    el.liveSend.addEventListener('click', function () { sendOnce(); });
    el.liveMsg.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') sendOnce();
    });

    /* ---- 常用弹幕：一键发送。列表只存本机浏览器，服务端只收最终那条文本 ---- */

    function quickList() {
      var v = store.get(QUICK_STORE, null);
      return Array.isArray(v) ? v : [];
    }

    function renderQuick() {
      var list = quickList();
      el.quickRow.hidden = !list.length && !quickEdit;
      el.quickLabel.textContent = quickEdit ? '点标签删除' : '常用';
      el.quickEdit.hidden = !list.length;
      el.quickEdit.textContent = quickEdit ? '完成' : '编辑';
      // 用下标而不是文本做标识：文本要进 HTML，下标不用，省掉一层转义风险
      el.quickChips.innerHTML = list.map(function (t, i) {
        return '<button type="button" class="quick-chip' + (quickEdit ? ' editing' : '')
          + '" data-qi="' + i + '" title="'
          + (quickEdit ? '点击删除' : '点击发送') + '">' + esc(t)
          + (quickEdit ? '<i class="qc-x">×</i>' : '') + '</button>';
      }).join('');
    }

    function addQuick() {
      var t = el.liveMsg.value.trim();
      if (!t) { el.liveResult.textContent = '先在上面输入要用作常用的弹幕'; return; }
      var list = quickList();
      if (list.indexOf(t) >= 0) {
        el.liveResult.textContent = '「' + t + '」已经在常用里了';
        return;
      }
      if (list.length >= QUICK_MAX) {
        el.liveResult.textContent = '常用最多 ' + QUICK_MAX + ' 条，先删掉几条再存';
        return;
      }
      list.push(t);
      store.set(QUICK_STORE, list);
      renderQuick();
      el.liveResult.textContent = '已存为常用：「' + t + '」';
    }

    // 只在「从没存过」时写入示例，用户清空后不会再冒出来
    if (store.get(QUICK_STORE, null) === null) store.set(QUICK_STORE, QUICK_DEFAULT);
    renderQuick();

    el.liveMsgSave.addEventListener('click', addQuick);
    el.quickEdit.addEventListener('click', function () {
      quickEdit = !quickEdit;
      renderQuick();
    });
    el.quickChips.addEventListener('click', function (e) {
      var b = e.target.closest('.quick-chip');
      if (!b) return;
      var list = quickList();
      var t = list[parseInt(b.getAttribute('data-qi'), 10)];
      if (!t) return;
      if (quickEdit) {
        list.splice(list.indexOf(t), 1);
        store.set(QUICK_STORE, list);
        if (!list.length) quickEdit = false;    // 删空了就退出编辑模式，别留一个空壳
        renderQuick();
      } else {
        sendOnce(t);
      }
    });

    el.wheelStart.addEventListener('click', function () {      if (liveBusy) return;
      var msg = el.wheelMsg.value.trim();
      if (!msg) { el.wheelState.textContent = '先输入要循环的弹幕'; return; }
      liveBusy = true;
      postJSON('/api/live/wheel/start', {
        msg: msg,
        interval: parseFloat(el.wheelInterval.value) || 3,
        count: parseInt(el.wheelCount.value, 10) || 20
      }).then(function (d) {
        if (d.error) el.wheelState.textContent = d.error;
        else el.wheelState.textContent = '已启动：每 ' + d.interval + ' 秒发 1 条，共 ' + d.count + ' 条';
        pollWheel();
      }).catch(function () { el.wheelState.textContent = '启动失败，请重试'; })
        .then(function () { liveBusy = false; });
    });
    el.wheelStop.addEventListener('click', function () {
      postJSON('/api/live/wheel/stop').then(pollWheel);
    });
  }

  function refreshLiveCred() {
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (d) {
      var name = d.uname || '已登录';
      if (d.logged && d.jct) {
        el.liveCredState.innerHTML = '已登录（' + esc(name) + '），弹幕可以直接发。';
        el.liveCredBox.hidden = true;
      } else {
        el.liveCredBox.hidden = false;
        el.liveCredState.innerHTML = d.logged
          ? '发弹幕还需要 <b>bili_jct</b>。<b>重新扫码登录一次就会自动带上</b>'
            + '（B 站登录时返回的 Cookie 里就有它）；也可以手工粘贴：'
            + 'F12 → Application → Cookies → bilibili.com。只存在本机，不会外传。'
          : '还没有登录：去播放器右上角扫码登录，成功后会自动同时取得 SESSDATA 与 bili_jct。';
      }
    }).catch(function () {
      el.liveCredState.textContent = '登录状态获取失败';
    });
  }

  function pollWheel() {
    fetch('/api/live/wheel').then(function (r) { return r.json(); }).then(function (d) {
      el.wheelStart.hidden = d.running;
      el.wheelStop.hidden = !d.running;
      el.wheelToggle.textContent = d.running
        ? '独轮车 · ' + d.sent + '/' + d.total : '独轮车';
      el.wheelToggle.classList.toggle('primary', !!d.running);
      if (d.motto) el.wheelMotto.textContent = d.motto;
      if (!d.running) {
        if (d.reason) {
          el.wheelState.textContent = d.reason;
          // 浮层收着的时候，自动停止这类结果必须留在按钮上，否则用户看不到
          if (el.wheelPop.hidden)
            el.wheelToggle.textContent = '独轮车 · '
              + (d.reason.indexOf('已发完') === 0 ? '已完成' : '已停止');
        }
        if (d.last_code != null && d.last_code !== 0)
          el.wheelState.textContent = '上次发送失败：' + (d.last_message || d.last_code);
        return;
      }
      var last = d.last_code == null ? '还没发第一条'
        : (d.last_code === 0 ? '最近一条成功 ✓' : '最近一条失败：' + (d.last_message || d.last_code));
      el.wheelState.textContent = '运行中：已发 ' + d.sent + ' / ' + d.total
        + ' 条（每 ' + d.interval + ' 秒 1 条）· ' + last;
    }).catch(function () { /* 服务没起时静默，不打扰 */ });
  }

  /* ---------------------------------------------------------- 实时弹幕（SSE） */
  // 浏览器不能直连 B 站弹幕网关：握手带不上 bilibili 域的 Cookie，实测认证后立刻被
  // 断开（close 1006）。所以由本机服务端维持那条 WebSocket，页面用 SSE 订阅结果。

  var chatEs = null;

  function chatSys(text) {
    chatLine(null, null, text, true);
  }

  function chatLine(uid, uname, text, sys) {
    var box = el.liveChat;
    if (!box) return;
    var div = document.createElement('div');
    div.className = 'chat-line' + (sys ? ' chat-sys' : '');
    if (!sys && uname) {
      var u = document.createElement('span');
      u.className = 'chat-uname';
      u.style.color = 'hsl(' + ((uid || 0) % 360) + ',70%,72%)';
      u.textContent = uname + '：';
      div.appendChild(u);
    }
    div.appendChild(document.createTextNode(text));
    var stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    box.appendChild(div);
    while (box.children.length > 80) box.removeChild(box.firstChild);
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function chatStart() {
    if (chatEs) return;
    el.liveChatState.textContent = '连接中…';
    // 必须自己带上 station：stq() 包装的是 window.fetch，管不到 EventSource。
    // 不带的话副站的直播间会订阅到**主站**的弹幕（主站没播就是空面板）。
    chatEs = new EventSource(stq('/api/live/chat/stream'));
    chatEs.onopen = function () { el.liveChatState.textContent = '已连接 · 实时弹幕中'; };
    chatEs.onmessage = function (ev) {
      var j;
      try { j = JSON.parse(ev.data); } catch (e) { return; }
      if (j.type === 'danmaku') chatLine(j.uid, j.uname, j.text);
      else if (j.type === 'popularity')
        el.liveChatState.textContent = '已连接 · 人气 ' + fmtNum(j.value);
      else if (j.type === 'state') chatSys(j.text);
    };
    // EventSource 自己会重连，这里只更新提示
    chatEs.onerror = function () { el.liveChatState.textContent = '已断开，重连中…'; };
  }

  function chatStop() {
    if (chatEs) { chatEs.close(); chatEs = null; }
    if (el.liveChat) {
      el.liveChat.innerHTML = '<div class="chat-line chat-sys">未连接</div>';
      el.liveChatState.textContent = '';
    }
  }

  /* ---------------------------------------------------------- 设置：快捷键 */
  // 按需求「不设置基础默认快捷键」：初始全为空，必须用户自己按一遍来绑定。
  // 绑定存 localStorage，**全局生效** —— 任意视图、焦点在页面任何位置都能用。

  var KEY_STORE = 'xl_keys';
  var keyMap = store.get(KEY_STORE) || {};
  var recording = null;                 // 正在录制的动作 id
  var keysBound = false;

  var KEY_ACTIONS = [
    { id: 'playPause', name: '播放 / 暂停', run: function () { clickSel('#ctrl-play'); } },
    { id: 'muteToggle', name: '静音 / 取消静音', run: function () { clickSel('#ctrl-mute'); } },
    { id: 'volUp', name: '音量增大', run: function () { bumpVolume(0.1); } },
    { id: 'volDown', name: '音量减小', run: function () { bumpVolume(-0.1); } },
    { id: 'seekBack', name: '快退 10 秒', scope: '仅回放', run: function () { seekBy(-10); } },
    { id: 'seekFwd', name: '快进 10 秒', scope: '仅回放', run: function () { seekBy(10); } },
    { id: 'theater', name: '宽屏模式切换', run: function () { clickSel('#btn-theater'); } },
    { id: 'fullscreen', name: '全屏切换', run: function () { clickSel('#ctrl-fs'); } },
    { id: 'random', name: '随机换一场', run: function () { clickSel('#btn-random'); } },
    { id: 'gotoLive', name: '切到 24 小时频道', run: function () { location.hash = '#/live'; } },
    { id: 'gotoBroadcast', name: '切到直播间', run: function () { location.hash = '#/broadcast'; } }
  ];

  function clickSel(sel) {
    var e = document.querySelector(sel);
    if (e) e.click();
  }

  function bumpVolume(d) {
    var v = Math.min(1, Math.max(0, (parseFloat(el.ctrlVol.value) || 0) + d));
    el.ctrlVol.value = v;
    el.ctrlVol.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function seekBy(d) {
    if (state.view === 'broadcast') return;       // 直播没有进度可拖
    if (!el.player || !isFinite(el.player.duration)) return;
    el.player.currentTime = Math.min(el.player.duration,
                                     Math.max(0, el.player.currentTime + d));
  }

  // 用 e.code（物理键位）而不是 e.key：切换输入法 / 大小写也不会变
  function comboOf(ev) {
    var k = ev.code || '';
    if (!k || /^(Control|Alt|Shift|Meta)(Left|Right)?$/.test(k)) return null;   // 只按了修饰键
    var s = '';
    if (ev.ctrlKey) s += 'Ctrl+';
    if (ev.altKey) s += 'Alt+';
    if (ev.shiftKey) s += 'Shift+';
    if (ev.metaKey) s += 'Meta+';
    return s + k;
  }

  var KEY_NAMES = { Space: '空格', ArrowLeft: '←', ArrowRight: '→', ArrowUp: '↑',
    ArrowDown: '↓', Enter: '回车', Escape: 'Esc', Backspace: '退格', Delete: 'Del',
    Tab: 'Tab', Home: 'Home', End: 'End', PageUp: 'PgUp', PageDown: 'PgDn',
    Minus: '-', Equal: '=', Comma: ',', Period: '.', Slash: '/', Semicolon: ';',
    Quote: "'", Backquote: '`', BracketLeft: '[', BracketRight: ']', Backslash: '\\' };

  function comboLabel(c) {
    if (!c) return '未设置';
    return c.split('+').map(function (p) {
      if (/^Key[A-Z]$/.test(p)) return p.slice(3);
      if (/^Digit\d$/.test(p)) return p.slice(5);
      if (/^Numpad/.test(p)) return '小键盘' + p.slice(6);
      return KEY_NAMES[p] || p;
    }).join(' + ');
  }

  function actionName(id) {
    for (var i = 0; i < KEY_ACTIONS.length; i++)
      if (KEY_ACTIONS[i].id === id) return KEY_ACTIONS[i].name;
    return id;
  }

  function saveKeys() { store.set(KEY_STORE, keyMap); }

  function bindSettings() {
    if (keysBound) return;
    keysBound = true;

    // 主播管理：填 UID → 检测 → 选来源 → 保存 / 删除
    var stProbeBtn = document.getElementById('st-probe');
    if (stProbeBtn) stProbeBtn.addEventListener('click', probeStation);
    var stMid = document.getElementById('st-mid');
    if (stMid) {
      stMid.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') probeStation();      // 输入框里回车即检测
      });
    }
    var stSaveBtn = document.getElementById('st-save');
    if (stSaveBtn) stSaveBtn.addEventListener('click', saveStation);
    var stList = document.getElementById('st-list');
    if (stList) {
      stList.addEventListener('click', function (e) {
        var b = e.target && e.target.closest ? e.target.closest('[data-st-del]') : null;
        if (b) deleteStation(b.getAttribute('data-st-del'));
      });
    }

    // 自动分段：开关 + 手动给最新一期排队，状态轮询在 renderSettings 里起
    el.segAuto.addEventListener('change', function () {
      postJSON('/api/segments/auto', { on: el.segAuto.checked }).then(segPoll);
    });

    // 回放清单：强制重新发现（改了 UID / 换了系列后用）。all=true 刷全部 ——
    // 服务端会在每位之间留间隔，避免被 B 站风控挡下
    if (el.seriesRefresh) {
      el.seriesRefresh.addEventListener('click', function () {
        var tip = document.getElementById('series-state');
        el.seriesRefresh.disabled = true;
        if (tip) tip.textContent = '正在重新获取（逐个主播、每位之间留间隔，约十几秒）…';
        postJSON('/api/series/refresh', { all: true })
          .then(function (d) {
            var fails = ((d && d.results) || []).filter(function (x) { return x.error; });
            if (tip) {
              tip.textContent = fails.length
                ? '部分失败：' + fails.map(function (x) {
                    return x.id + '（' + x.error + '）';
                  }).join('；')
                : '已重新获取 ✓';
            }
            seriesPoll();
          })
          .catch(function () { if (tip) tip.textContent = '请求失败，请重试'; })
          .then(function () { el.seriesRefresh.disabled = false; });
      });
    }

    el.segRun.addEventListener('click', function () {
      var p = state.all && state.all[0];
      if (!p) { el.segState.textContent = '节目单还没载入'; return; }
      postJSON('/api/segments/run', { bvid: p.bvid }).then(function (d) {
        el.segState.textContent = d.error ? d.error : ('已排队：' + p.title);
        segPoll();
      });
    });

    // 书签模式：直连（零提示但不能启动程序）/ 协议（能启动，首次要过浏览器授权）
    el.protoDirect.addEventListener('change', function () {
      protoDirect = el.protoDirect.checked;
      store.set(KEY_PROTO_DIRECT, protoDirect);
      renderProtoLink();
      protoPoll();
    });

    // 桌面快捷方式：比书签更彻底 —— 直接指向 EXE、不经过浏览器，零授权零提示
    el.shortcutCreate.addEventListener('click', function () {
      el.shortcutState.textContent = '正在创建…';
      el.shortcutCreate.disabled = true;
      postJSON('/api/shortcut/create', {}).then(function (d) {
        el.shortcutState.textContent = d.error
          ? d.error
          : '已创建：' + d.path + '\n双击它就会启动程序并打开网页（已在运行时直接打开页面）。';
      }).catch(function () {
        el.shortcutState.textContent = '创建失败，请重试。';
      }).then(function () { el.shortcutCreate.disabled = false; });
    });

    // 自定义协议：注册后书签 komichi://open 就能拉起本程序
    el.protoReg.addEventListener('click', function () {
      postJSON('/api/protocol/register', {}).then(function (d) {
        el.protoState.textContent = d.error ? d.error : '已注册，现在可以把书签拖进书签栏了。';
        protoPoll();
      });
    });
    el.protoUnreg.addEventListener('click', function () {
      postJSON('/api/protocol/unregister', {}).then(function (d) {
        el.protoState.textContent = d.error ? d.error : '已取消注册（书签不再能启动程序）。';
        protoPoll();
      });
    });
    // 拿不到 /api/protocol（服务刚起或离线）时也要先把书签与说明按当前模式渲染出来，
    // 否则链接会停在 href="#" 的空壳上
    el.protoDirect.checked = protoDirect;
    renderProtoLink();

    el.keysList.addEventListener('click', function (e) {
      var del = e.target.closest('[data-key-del]');
      if (del) {
        delete keyMap[del.getAttribute('data-key-del')];
        saveKeys(); renderSettings();
        el.keysTip.textContent = '已清除该快捷键。';
        return;
      }
      var btn = e.target.closest('[data-key-btn]');
      if (btn) startRecord(btn.getAttribute('data-key-btn'), btn);
    });
    el.keysReset.addEventListener('click', function () {
      if (!Object.keys(keyMap).length) {
        el.keysTip.textContent = '现在还没有绑定任何快捷键。'; return;
      }
      keyMap = {}; saveKeys(); renderSettings();
      el.keysTip.textContent = '已全部清除。';
    });

    // 全局捕获：任意视图、任意焦点都能触发；录制态优先处理
    document.addEventListener('keydown', function (ev) {
      if (recording) {
        ev.preventDefault();
        ev.stopPropagation();
        if (ev.key === 'Escape') {
          recording = null; renderSettings();
          el.keysTip.textContent = '已取消。';
          return;
        }
        var combo = comboOf(ev);
        if (!combo) return;                        // 还在按修饰键，继续等
        var name = actionName(recording);
        Object.keys(keyMap).forEach(function (k) {  // 同一个组合只能属于一个动作
          if (keyMap[k] === combo && k !== recording) delete keyMap[k];
        });
        keyMap[recording] = combo;
        saveKeys();
        recording = null;
        renderSettings();
        el.keysTip.textContent = '已绑定：' + name + ' → ' + comboLabel(combo) + '。';
        return;
      }

      var hit = comboOf(ev);
      if (!hit) return;
      // 在输入框里打字时不劫持裸按键；带 Ctrl/Alt/Meta 的组合仍然全局生效
      var t = ev.target;
      var typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
                         || t.tagName === 'SELECT' || t.isContentEditable);
      if (typing && !(ev.ctrlKey || ev.altKey || ev.metaKey)) return;
      for (var i = 0; i < KEY_ACTIONS.length; i++) {
        if (keyMap[KEY_ACTIONS[i].id] === hit) {
          ev.preventDefault();
          ev.stopPropagation();
          KEY_ACTIONS[i].run();
          return;
        }
      }
    }, true);
  }

  function startRecord(id, btn) {
    recording = id;
    Array.prototype.forEach.call(el.keysList.querySelectorAll('.key-combo'), function (b) {
      b.classList.remove('recording');
      b.textContent = comboLabel(keyMap[b.getAttribute('data-key-btn')]);
    });
    btn.classList.add('recording');
    btn.textContent = '按下按键…（Esc 取消）';
    el.keysTip.textContent = '正在录制「' + actionName(id) + '」，请按下要用的组合键。';
  }

  /* 回放来源状态：每位主播各自「自动发现到了哪个系列 / 为什么没拿到」 */
  function seriesPoll() {
    var tip = document.getElementById('series-state');
    if (!tip) return;
    fetch('/api/stations').then(function (r) { return r.json(); }).then(function (d) {
      var rows = ((d && d.stations) || []).map(function (s) {
        var sr = s.series || {};
        var tail;
        if (sr.id) {
          tail = '系列 ' + sr.id + (sr.total ? ' · ' + sr.total + ' 场' : '')
            + '（' + (sr.source === 'config' ? 'stations.json 手填' : '自动发现') + '）';
        } else if (sr.error) {
          tail = '没拿到：' + sr.error;
        } else {
          tail = '还没拿到，后台重试中';
        }
        return (s.short || s.name) + '：' + tail;
      });
      tip.textContent = rows.join('\n');
    }).catch(function () { tip.textContent = '读不到回放来源状态（服务未启动？）'; });
  }

  /* ---------------------------------------------------------- 主播管理（设置页）
     换主播原本只能手改 data/stations.json。这里把「探测 → 选来源 → 保存 / 删除」
     搬进页面：名字与房间号由探测填好，来源默认自动发现（留空），
     只有用户**显式**选了某个系列才写死 —— 写死后新系列不会再自动跟上。 */
  var stProbe = null;                 // 最近一次探测结果，保存时用来补字段

  function stTip(html) {
    var t = document.getElementById('st-state');
    if (t) t.innerHTML = html || '';
  }

  function renderStationsCard() {
    var box = document.getElementById('st-list');
    if (!box) return;
    var list = STATIONS || [];
    if (!list.length) {
      box.innerHTML = '<p class="live-note">还没读到主播列表。</p>';
      return;
    }
    box.innerHTML = list.map(function (s) {
      var meta = [];
      if (s.mid) meta.push('UID ' + s.mid);
      if (s.room) meta.push('房间 ' + s.room);
      meta.push(s.series_id ? ('系列 ' + s.series_id + '（手填）') : '来源自动发现');
      return '<div class="key-row">'
        + '<span class="key-name" style="border-left:3px solid '
        + esc(s.accent || '#8a8a95') + ';padding-left:8px">'
        + esc(s.short || s.name || s.id) + (s.main ? ' <b>（主站）</b>' : '') + '</span>'
        + '<span class="key-scope">' + esc(meta.join(' · ')) + '</span>'
        + (s.main ? '' : '<button class="key-del" type="button" title="删除"'
           + ' style="margin-left:auto" data-st-del="' + esc(s.id) + '">×</button>')
        + '</div>';
    }).join('');
  }

  function stRefreshUI() {
    // fetchStations 有 30 秒缓存；服务端改完文件已经主动失效过，这里拿到的是新的
    fetchStations().then(function () {
      renderLamps();
      renderMixedNote();
      renderStationsCard();
    });
  }

  function probeStation() {
    var input = document.getElementById('st-mid');
    var sel = document.getElementById('st-source');
    var box = document.getElementById('st-source-box');
    var note = document.getElementById('st-source-note');
    var save = document.getElementById('st-save');
    var mid = (input.value || '').trim();
    if (!mid) { stTip('先填主播 UID'); return; }
    stTip('正在检测…');
    stProbe = null;
    if (box) box.hidden = true;
    if (note) note.hidden = true;
    if (save) save.hidden = true;
    postJSON('/api/stations/probe', { mid: mid })
      .then(function (d) {
        if (d.error) { stTip('<b>' + esc(d.error) + '</b>'); return; }
        stProbe = d.probe || null;
        var srcs = (stProbe && stProbe.sources) || [];
        sel.innerHTML = '<option value="">自动（推荐：以后新系列会自己跟上）</option>'
          + srcs.map(function (s) {
              return '<option value="' + esc(s.id) + '">'
                + esc((s.name || s.id) + '（' + s.total + ' 个）') + '</option>';
            }).join('');
        if (stProbe && stProbe.suggested) sel.value = stProbe.suggested;
        box.hidden = false;
        save.hidden = false;
        if (note) {
          note.hidden = !srcs.length;
          if (!srcs.length) {
            note.innerHTML = '这位的「合集和系列」里还没有可用的<b>系列</b>，'
              + '加进来也能用，但回放清单会是空的（只有合集不行，归档接口只认系列）。';
          }
        }
        stTip('查到 <b>' + esc((stProbe && stProbe.name) || ('UID ' + mid))
          + '</b>' + ((stProbe && stProbe.room) ? '，房间号 ' + esc(stProbe.room) : '')
          + '，确认无误就点保存。');
      })
      .catch(function () { stTip('检测失败，请重试'); });
  }

  function saveStation() {
    var sel = document.getElementById('st-source');
    var mid = (document.getElementById('st-mid').value || '').trim();
    if (!mid) { stTip('先填主播 UID'); return; }
    var body = { mid: mid };
    if (stProbe) {
      if (stProbe.name) body.name = stProbe.name;
      if (stProbe.room) body.room = stProbe.room;
    }
    var sid = (sel.value || '').trim();
    if (sid) body.series_id = sid;      // 留空 = 自动发现（v2 语义）
    stTip('正在保存…');
    postJSON('/api/stations/save', body)
      .then(function (d) {
        if (d.error) { stTip('<b>' + esc(d.error) + '</b>'); return; }
        stTip(sid ? '已保存 ✓（来源已写死为这个系列，以后不再自动发现）' : '已保存 ✓');
        stRefreshUI();
      })
      .catch(function () { stTip('保存失败，请重试'); });
  }

  function deleteStation(id) {
    var tip = document.getElementById('st-state');
    if (!window.confirm('删除这位主播？\n\n只是从列表里移除；已经抓下来的回放清单仍留在本机。')) return;
    postJSON('/api/stations/delete', { id: id })
      .then(function (d) {
        if (d.error) { stTip('<b>' + esc(d.error) + '</b>'); return; }
        // 「在看谁」里可能还留着它的 id：不清掉的话下次加载会去找一个不存在的板块
        ST_SET = ST_SET.filter(function (x) { return x !== id; });
        if (ST === id) ST = ST_SET.length ? ST_SET[0] : '';
        saveStations();
        if (ST === id || !ST) {
          // 删掉的正是在看的这一位：整页重载，和「点灯切换」走同一条路
          location.reload();
          return;
        }
        stTip('已删除 ✓');
        stRefreshUI();
      })
      .catch(function () { stTip('删除失败，请重试'); });
  }

  function renderSettings() {
    bindSettings();
    renderStationsCard();
    el.keysList.innerHTML = KEY_ACTIONS.map(function (a) {
      var c = keyMap[a.id];
      return '<div class="key-row">'
        + '<span class="key-name">' + esc(a.name)
        + (a.scope ? '<span class="key-scope">' + esc(a.scope) + '</span>' : '')
        + '</span>'
        + '<button class="key-combo' + (c ? ' set' : '') + '" type="button"'
        + ' data-key-btn="' + a.id + '">' + esc(comboLabel(c)) + '</button>'
        + '<button class="key-del" type="button" title="清除"'
        + ' data-key-del="' + a.id + '"' + (c ? '' : ' hidden') + '>×</button>'
        + '</div>';
    }).join('');
    protoPoll();
    seriesPoll();
  }

  // 书签两种模式：
  //   直连 —— 书签指向 http://127.0.0.1:8765/，点一下直接进网页、零提示；
  //           但**只能打开已经在运行的程序，不能启动它**。http:// 是浏览器保留协议，
  //           技术上无法关联到本地 EXE（否则恶意网页就能拉起任意本地程序），
  //           所以「直连 + 能启动」在浏览器里不可能同时成立 —— 这是安全边界，不是实现问题。
  //   协议 —— 书签指向 komichi://open，程序没在跑也能一点拉起；代价是浏览器会先问一次
  //           「要打开二十四时小路电台吗」。那是浏览器给出的确认框，网页关不掉。
  // 想要「一点就进 + 完全没有任何提示」，正解是**桌面快捷方式**（见设置页）：
  // 它直接指向 EXE、不经过浏览器，因此不存在授权那一层。
  /* 键名带 _v2：旧的 `xl_proto_direct`（默认直连那版）在用户浏览器里可能已经存了 true
     （只要点过一次开关就会落盘），沿用旧键名会让新的默认值失效、仍停在直连模式。
     换键名让旧值自然作废 —— 反正旧默认值本身就是要纠正的设计。 */
  var KEY_PROTO_DIRECT = 'xl_proto_direct_v2';
  var PROTO_URL = 'komichi://open';
  var protoAutoTried = false;
  /* 默认「协议模式」而不是「直连」：直连书签虽然零提示，但它**打不开没在运行的电台**
     —— 刚开机点书签只会得到「无法连接」，用户被卡住且找不到入口
     （旧版直连时还把「注册 / 修复」按钮藏了）。书签的第一职责是「能把程序启动起来」，
     所以默认走协议书签；想零提示的用户可以切直连，代价在界面上写清楚。 */
  var protoDirect = store.get(KEY_PROTO_DIRECT, false) === true;

  function renderProtoLink(url) {
    if (protoDirect) {
      el.protoLink.setAttribute('href', location.origin + '/');
      el.protoLink.textContent = '▶ 打开小路电台（直连）';
      el.protoHint.innerHTML = '直连：点书签直接进网页、没有任何提示。'
        + '但它<b>只能打开已经在运行的电台，不能把程序启动起来</b> —— '
        + '程序没在跑（比如刚开机）时点它会提示打不开。'
        + '要让书签也能启动程序，请关掉上面这个开关。';
    } else {
      el.protoLink.setAttribute('href', url || PROTO_URL);
      el.protoLink.textContent = '▶ 启动小路电台';
      el.protoHint.innerHTML = '协议模式：程序没在跑时，点书签也能把它<b>启动起来</b>。'
        + '代价是浏览器会先问一次「要打开二十四时小路电台吗」——'
        + '弹窗里若有「<b>始终允许</b>」就勾上，多数情况下以后不再问。'
        + '（这个确认框是浏览器的安全边界，网页关不掉；若你的浏览器每次都问，'
        + '就用下面的<b>桌面快捷方式</b> —— 那条路完全没有任何提示。）';
    }
  }

  function protoPoll() {
    fetch('/api/protocol').then(function (r) { return r.json(); }).then(function (d) {
      el.protoDirect.checked = protoDirect;
      renderProtoLink(d.url);
      /* 协议模式下若还没注册就自动注册一次：不注册的话那块注册表项不存在，
         点协议书签浏览器会「找不到关联程序」，书签等于废的。
         只写 HKEY_CURRENT_USER、不需要管理员权限，随时可用「取消注册」撤销。
         只自动试一次，失败就交回给用户手点，避免反复重试。 */
      if (!protoDirect && d.supported && !d.registered && !protoAutoTried) {
        protoAutoTried = true;
        el.protoState.textContent = '正在自动注册协议…';
        postJSON('/api/protocol/register', {}).then(function (r2) {
          if (r2.error) { el.protoState.textContent = '自动注册失败：' + r2.error; return; }
          protoPoll();
        }).catch(function () {
          el.protoState.textContent = '自动注册失败，请点上面的「注册 / 修复」。';
        });
        return;
      }
      var s;
      if (protoDirect) {
        s = '当前：直连书签（点击零提示，但程序必须已经在运行、不能启动程序）。'
          + (d.supported
             ? '协议目前' + (d.registered ? '已注册' : '未注册') + '，切回协议模式才会用到它。'
             : '源码运行模式：协议不需要注册（注册的目标得是本程序的 EXE）。');
      } else if (!d.supported) {
        s = '源码运行模式：不需要注册（注册的目标得是本程序的 EXE）。';
      } else if (d.registered) {
        s = '已注册 ✓ 这条书签能把程序启动起来。\n'
          + '还没加书签的话：把上面那条链接拖到书签栏即可；'
          + '首次点击时浏览器会问一次，弹窗里有「始终允许」就勾上。\n'
          + '想完全不要提示：点下面的「创建桌面快捷方式」。';
      } else {
        s = '未注册 —— 点上面的「注册 / 修复」，书签才能启动程序。\n'
          + '（只写 HKEY_CURRENT_USER，不需要管理员权限）';
      }
      el.protoState.textContent = s;
    }).catch(function () { /* 服务未起时静默 */ });
  }

  var segTimer = null;
  var segBusy = false;

  function segPoll() {
    fetch('/api/segments/status').then(function (r) { return r.json(); }).then(function (d) {
      el.segAuto.checked = !!d.auto;
      var cov = d.coverage || {};
      var line = 'ffmpeg：' + (d.ffmpeg ? '已找到' : '未找到（分段需要 ffmpeg）')
        + ' · 边界精修：' + (d.refine ? '可用' : '不可用（缺 numpy/pillow，只用音频分析）')
        + ' · 已有分段 ' + (cov.have || 0) + ' / ' + (cov.total || 0) + ' 个分P';
      if (d.running) line += ' · 正在处理 ' + (d.current || '');
      else if ((d.queue || []).length) line += ' · 排队 ' + d.queue.length + ' 个';
      el.segNote.textContent = line;
      segBar(d, cov);

      var out = [];
      if (d.error) out.push('错误：' + d.error);
      if ((d.log || []).length) out.push(d.log.slice(-3).join(' '));
      var last = (d.done || [])[(d.done || []).length - 1];
      if (last) {
        out.push(last.ok
          ? ('上一次：' + (last.title || last.bvid) + '，新算 ' + last.processed
             + ' 个分P（跳过 ' + last.skipped + '），共 ' + last.segments
             + ' 个分P有分段 · 刷新页面即可看到')
          : ('上一次失败：' + (last.error || '')));
      }
      el.segState.textContent = out.join('\n');
    }).catch(function () { /* 服务未起时静默 */ });
  }

  /* -------------------------------------------- 主界面：分段数据栏 */

  // 副行：正在跑就显示进度，否则显示「上次更新」——这两件事是轮流占用同一行的，
  // 因为用户在这一行只想问「数据是新的吗」。
  // 时间取持久化记录（seg_state.json），不用 segments.js 的 mtime：
  // 每次启动/换版本都会重写那个文件，mtime 会变成「刚刚」，等于撒谎。
  function segWhenText(d) {
    var queue = (d.queue || []).length;
    if (d.running || queue) {
      return '正在更新：还有 ' + (queue + (d.running ? 1 : 0)) + ' 个投稿排队'
        + (d.current ? '（当前 ' + d.current + '）' : '');
    }
    if (!d.ffmpeg) return '本机没找到 ffmpeg，分段功能不可用';
    var last = d.last || {};
    if (!last.at) return '尚未更新过 —— 点右侧按钮开始识别分段';
    var g = new Date(last.at * 1000);
    var today = new Date();
    var stamp = (g.toDateString() === today.toDateString())
      ? hhmm(g)
      : (g.getMonth() + 1) + '-' + pad2(g.getDate()) + ' ' + hhmm(g);
    if (!last.ok) return '上次更新失败（' + stamp + '）：' + (last.error || '未知错误');
    return '上次更新：' + stamp + ' · 新算 ' + (last.processed || 0) + ' 个分P'
      + (last.title ? ' · ' + last.title : '');
  }

  function segBar(d, cov) {
    var total = cov.total || 0, have = cov.have || 0;
    el.segCov.textContent = '分段数据：' + have + ' / ' + total + ' 个分P'
      + (total ? '（' + Math.round(have * 100 / total) + '%）' : '')
      + ' · ' + fmtNum(cov.segments || 0) + ' 段';
    el.segBtn.disabled = segBusy || !d.ffmpeg;
    el.segWhen.textContent = segWhenText(d);
    segProgress(d);
  }

  function durText(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + ' 秒';
    if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + pad2(sec % 60) + ' 秒';
    return Math.floor(sec / 3600) + ' 小时 ' + Math.floor((sec % 3600) / 60) + ' 分';
  }

  // 识别进度条：数字全部来自分析循环的实时上报（analyzed/total），
  // 剩余时间用「已完成音频秒数 / 已耗时」的实测速率推算 —— 不是拍脑袋。
  function segProgress(d) {
    var busy = d.running || ((d.queue || []).length > 0);
    if (!busy) { el.segProg.hidden = true; return; }
    el.segProg.hidden = false;

    var p = d.progress || {}, b = d.batch || {};
    // 音频与「已唱」浮层精修是并行的，各自上报各自的进度：
    // 工作量 = 音频已分析秒数 + 精修按同样长度折算，总工作量 = 音频总秒数 × phases。
    var at = p.audio_total || 0;
    var ad = Math.min(p.analyzed || 0, at || Infinity);
    var rr = p.refine_ratio || 0;
    var ph = p.phases || 1;
    var frac = null;
    var sub = at ? (ad + rr * at) / (at * ph) : 0;
    // 条的口径是「整批」：(已完成投稿数 + 当前投稿内的进度) / 总投稿数。
    // 这样跨分P、跨投稿都只增不减 —— 否则每个分P 跑完都会从 100% 跳回小百分比。
    var total = p.total || 0;
    if (b.total) {
      frac = (Math.min(b.done || 0, b.total)
        + (p.part_index ? ((p.part_index - 1) + sub) / p.parts : 0)) / b.total;
    } else if (p.part_index && p.parts) {
      frac = ((p.part_index - 1) + sub) / p.parts;
    } else if (total) {
      frac = sub;
    }
    if (frac !== null) {
      el.segProgFill.style.width = Math.max(1, Math.round(Math.min(1, Math.max(0, frac)) * 100)) + '%';
    }

    var out = [];
    if (b.total) out.push('第 ' + Math.min((b.done || 0) + 1, b.total) + ' / ' + b.total + ' 个投稿');
    if (p.parts > 1) out.push('分P ' + (p.part_index || 1) + '/' + p.parts);
    // 数字按「音频秒」显示，精修单独给百分比 —— 总进度（含精修）看条就够了，
    // 不把工作量口径（音频秒×phases）直接当秒数写出来误导人
    if (at) {
      out.push(ad < at ? ('音频 ' + fmtNum(ad) + ' / ' + fmtNum(at) + ' 秒')
                       : '音频 100%');
      if (ph > 1) out.push('浮层 ' + Math.round(rr * 100) + '%');
    }
    if (p.title && b.total > 1) out.push(p.title);
    out.push(p.stage || '准备中');

    // 速率要跑够一会儿才稳定，起步阶段先不给预计时间，免得数字乱跳
    var rate = (d.elapsed || 0) > 10 ? (d.done_seconds || 0) / d.elapsed : 0;
    if (rate > 0 && d.todo_seconds > 0) out.push('预计还需 ' + durText(d.todo_seconds / rate));
    else if (d.todo_seconds > 0) out.push('剩余约 ' + durText(d.todo_seconds) + ' 音频');
    el.segProgText.textContent = out.join(' · ');
  }

  // 「更新数据」：手动跑一次分段扫描，把所有还缺分段的分P 补齐（新回放优先）。
  // 与设置页的自动开关无关 —— 这是用户显式点的，不受 auto 与水位线限制。
  function segRefresh() {
    if (segBusy) return;
    segBusy = true;
    el.segBtn.disabled = true;
    el.segBtnText.textContent = '更新中…';
    var reset = function () {
      segBusy = false;
      el.segBtn.disabled = false;
      el.segBtnText.textContent = '更新数据';
    };
    postJSON('/api/segments/refresh', {}).then(function (d) {
      reset();
      if (!d.ok) { el.segWhen.textContent = d.error || '更新失败'; return; }
      if (!d.queued) {
        el.segWhen.textContent = d.missing_parts
          ? ('这 ' + d.missing_parts + ' 个分P 已在更新队列里')
          : '已是最新，没有缺分段的分P';
        return;
      }
      el.segWhen.textContent = '已排队 ' + d.queued + ' 个投稿（'
        + d.missing_parts + ' 个分P 待识别），正在后台更新…';
    }).catch(function (e) {
      reset();
      el.segWhen.textContent = '更新失败：' + (e && e.message ? e.message : e);
    });
  }

  /* ---------------------------------------------------------- 页面存活上报 */
  // 网页全关掉时把后台服务一起关掉：关闭/跳转用 sendBeacon 说一声（刷新会在宽限期内
  // 重新连上；bfcache 恢复不算关闭），心跳则兜住「浏览器崩溃、beacon 发不出去」的情况。

  var pageId = 'p' + Math.random().toString(36).slice(2) + Date.now().toString(36);

  function pageAlive() {
    fetch('/api/page/alive?cid=' + encodeURIComponent(pageId)).catch(function () {});
  }

  function pageSetup() {
    pageAlive();
    setInterval(pageAlive, 20000);
    window.addEventListener('pagehide', function (ev) {
      if (ev.persisted) return;                 // 进 bfcache：不是关闭，回来还要用
      try {
        navigator.sendBeacon('/api/page/bye',
          new Blob([JSON.stringify({ cid: pageId })], { type: 'application/json' }));
      } catch (e) { /* 没有 sendBeacon 的老浏览器：交给心跳兜底 */ }
    });
  }

  /* ---------------------------------------------------------- 视图路由 */

  var viewEntered = false;      // 路由时是否已播过入场动画
  var viewSwitchTimer = null;   // 切页时给 .wrap 临时开过渡，用完摘掉
  var veilTimer = null;         // 切换板块时延后盖过渡遮罩（先让用户看见灯的反馈）

  /* 切换板块的过渡收尾：新板块的内容已经渲染出来了，把遮罩撤掉。
     留一个最短停留 —— 数据来得太快时遮罩一闪而过，比不盖还刺眼。
     另有一道硬超时兜在 index.html 的内联脚本里（万一没走到这里，
     也不能把整个页面永久藏着）。 */
  function finishSwitchVeil() {
    var root = document.documentElement;
    if (!root.classList.contains('switching')) return;
    var at = 0;
    try {
      at = (JSON.parse(sessionStorage.getItem('xl_veil') || '{}') || {}).at || 0;
    } catch (e) { /* 忽略 */ }
    setTimeout(function () {
      root.classList.remove('switching');
      try { sessionStorage.removeItem('xl_veil'); } catch (e) { /* 忽略 */ }
    }, Math.max(0, 260 - (Date.now() - at)));
  }

  /* 让当前视图播一次入场动画（淡入 + 轻微上移，220ms）。
     dir: 1 = 从右边进（往右点的标签），-1 = 从左边进，0 = 只淡入（首屏）。
     remove 之后再读一次 offsetWidth 是必需的 —— 同一帧内 remove+add 会被浏览器
     合并，动画不会重播（连点同一标签、或在两个视图间来回切都要能重播）。 */
  function enterView(dir) {
    var s = document.querySelector('.views > .view.active');
    if (!s) return;
    s.style.setProperty('--enter-x', (dir > 0 ? 16 : dir < 0 ? -16 : 0) + 'px');
    s.classList.remove('view-enter');
    void s.offsetWidth;
    s.classList.add('view-enter');
  }

  /* 顶栏那颗红色胶囊：它不属于任何标签，只是在标签之间滑过去。
     instant = 直接就位（首次 / 改窗口大小），不给过渡 ——
     否则页面一打开胶囊会从最左边「滑」到第一项。 */
  var tabPillTimer = null;

  function moveTabPill(instant) {
    var nav = document.querySelector('.tb-tabs');
    var pill = document.getElementById('tb-pill');
    var tab = nav && nav.querySelector('.tab.active');
    if (!nav || !pill || !tab) return;
    if (instant) pill.classList.remove('ready');
    pill.style.width = tab.offsetWidth + 'px';
    pill.style.height = tab.offsetHeight + 'px';
    pill.style.transform = 'translateX(' + tab.offsetLeft + 'px)';
    if (instant) {
      void pill.offsetWidth;
      pill.classList.add('ready');
    }
  }

  function setView(name) {
    if (VIEWS.indexOf(name) < 0) name = 'live';
    var prev = state.view;
    state.view = name;
    if (prev !== name) {
      // 两列宽度在这 260ms 里平滑过渡（主站侧栏 404px ↔ 非直播 288px），
      // 只在切页这一下开 transition —— 常驻的话拖窗口会变得迟钝。
      //
      // 顺序很讲究：**必须先让 transition 生效，再去改列定义**。
      // 两件事写在同一帧里的话，浏览器拿「变化前」的样式（transition: none）
      // 去判断，根本不会启动过渡（实测列宽依然是「啪」地跳过去）。
      // 读一次 offsetWidth 强制结算样式，过渡属性就位之后再改 view-other。
      document.body.classList.add('view-switching');
      if (viewSwitchTimer) clearTimeout(viewSwitchTimer);
      viewSwitchTimer = setTimeout(function () {
        document.body.classList.remove('view-switching');
      }, 340);
      void document.body.offsetWidth;
    }
    document.body.classList.toggle('view-other', name !== 'live');
    document.body.classList.toggle('view-broadcast', name === 'broadcast');
    document.querySelectorAll('.tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-view') === name);
    });
    VIEWS.forEach(function (v) {
      var s = document.getElementById('view-' + v);
      if (s) s.classList.toggle('active', v === name);
    });
    moveTabPill();             // 胶囊滑到新激活的标签上
    if (prev !== name) {
      // 方向按标签条里的先后定：往右点就从右进，往左点就从左进
      var i0 = VIEWS.indexOf(prev), i1 = VIEWS.indexOf(name);
      enterView((i0 >= 0 && i1 >= 0) ? (i1 > i0 ? 1 : -1) : 0);
      viewEntered = true;      // 路由这一下已经播过，启动完成时别再播一次
    }

    // 监控室：切进来时才接流（不在首页就连 4 路，省带宽也省对面服务器）
    if (name === 'multi') { bindMulti(); renderMulti(); }
    // 离开监控室必须销毁：这几路是持续拉流的直播，不销毁就会在后台一直跑，
    // 反复进出还会累积孤儿连接（重连定时器也一并清掉）。
    if (prev === 'multi' && name !== 'multi') multiTeardown();
    // 微博：切进来时才取（微博接口慢又是外部服务，没必要在首页就拉）
    if (name === 'weibo') { bindWeibo(); renderWeibo(); }

    // 真的换了页面就从顶部看起 —— 否则在列表里滚了几屏之后切到设置页，
    // 会直接落到那一页的中段（实测停在 1127px，用户以为自己点错了）。
    if (prev !== name) window.scrollTo(0, 0);

    // 窄屏的标签条是横向滚动的（1080px 以上才会全部平铺），
    // 把当前页签滚进可见区，不然「设置」这种末尾项永远看不见。
    var act = document.querySelector('.tab.active');
    var strip = act && act.parentNode;
    if (act && strip && strip.scrollWidth > strip.clientWidth + 1) {
      var ar = act.getBoundingClientRect(), sr = strip.getBoundingClientRect();
      strip.scrollLeft += (ar.left - sr.left) - (sr.width - ar.width) / 2;
    }

    if (name === 'live') renderList();
    else if (name === 'broadcast') renderBroadcast();
    else if (name === 'schedule') renderSchedule();
    else if (name === 'categories') renderCategories();
    else if (name === 'about') renderAbout();
    else if (name === 'settings') renderSettings();

    if (name !== 'broadcast') {
      if (prev === 'broadcast') resumeReplay();
      chatStop();
      if (wheelTimer) {                              // 离开直播间页就停止轮询
        clearInterval(wheelTimer);
        wheelTimer = null;
      }
    }
    // 分段状态轮询：主界面要显示覆盖率与「上次更新」，设置页要显示日志与开关
    if (name === 'live' || name === 'settings') {
      segPoll();
      if (!segTimer) segTimer = setInterval(segPoll, 4000);
    } else if (segTimer) {
      clearInterval(segTimer);
      segTimer = null;
    }
  }

  function route() {
    setView((location.hash || '').replace(/^#\/?/, '') || 'live');
  }

  /* ---------------------------------------------------------- 交互 */

  // 只切布局类，不重载媒体，所以播放不中断
  function toggleTheater(on) {
    var next = typeof on === 'boolean' ? on : !document.body.classList.contains('theater');
    document.body.classList.toggle('theater', next);
    el.btnTheater.setAttribute('aria-pressed', String(next));
    el.btnTheater.textContent = next ? '退出宽屏' : '宽屏';
  }

  // 画质说明面板
  function toggleQuality(on) {
    var next = typeof on === 'boolean' ? on : el.qualityPanel.hidden;
    el.qualityPanel.hidden = !next;
    el.btnQuality.setAttribute('aria-expanded', String(next));
  }

  function syncSkip() {
    el.btnSkip.textContent = '跳过空白：' + (state.skip ? '开' : '关');
    var n = Object.keys(window.SEGMENTS || {}).length;
    el.btnSkip.title = n ? ('已标注 ' + n + ' 个分P') : '还没有任何标注，先在「标注」里标出片段';
  }

  /* ---------------------------------------------------------- 片段标注 */

  function currentSeg() {
    return state.cycle && state.cycle.segments.length
      ? state.cycle.segments[state.segIndex] : null;
  }

  // 把当前播放位置记为一个点：同一分P 内交替记「开始 / 结束」
  function addMark() {
    var seg = currentSeg();
    if (!seg) return;
    var at = Math.round(seg.t0 + (cyclePos() - seg.start));
    var last = state.marks[state.marks.length - 1];
    var kind = (last && last.cid === String(seg.cid) && last.kind === 'start') ? 'end' : 'start';
    state.marks.push({
      cid: String(seg.cid), bvid: seg.bvid, page: seg.page, t: at, kind: kind
    });
    store.set('xl_marks', state.marks);
    renderMarks();
  }

  function buildSegmentsJson() {
    var byCid = {};
    state.marks.forEach(function (m) {
      (byCid[m.cid] = byCid[m.cid] || []).push(m);
    });
    var out = {};
    Object.keys(byCid).forEach(function (cid) {
      var open = null;
      var list = [];
      byCid[cid].forEach(function (m) {
        if (m.kind === 'start') {
          open = m.t;
        } else if (open !== null && m.t > open) {
          list.push({ start: open, end: m.t, label: '' });
          open = null;
        }
      });
      if (list.length) out[cid] = list;
    });
    return 'window.SEGMENTS = ' + JSON.stringify(out, null, 1) + ';';
  }

  function renderMarks() {
    var seg = currentSeg();
    var cid = seg ? String(seg.cid) : null;
    var mine = state.marks.filter(function (m) { return m.cid === cid; });

    el.markCur.textContent = seg
      ? seg.program.title + ' · 分P ' + seg.page + ' · cid ' + seg.cid
        + ' · 本分P 已标 ' + mine.length + ' 个点'
      : '—';

    el.markList.innerHTML = mine.length
      ? mine.map(function (m) {
          return '<li>' + fmtClock(m.t) + '　<b>'
            + (m.kind === 'start' ? '开始' : '结束') + '</b></li>';
        }).join('')
      : '<li class="mp-empty">还没有标记（播放到片段开头按 M）</li>';

    el.markJson.value = buildSegmentsJson();
  }

  function toggleMarking(on) {
    var next = typeof on === 'boolean' ? on : el.markPanel.hidden;
    el.markPanel.hidden = !next;
    state.marking = next;
    el.btnMark.setAttribute('aria-pressed', String(next));
    // 标注时关掉「跳过空白」，否则会在已裁剪的片段里再标一次
    if (next && state.skip) {
      state.skip = false;
      store.set('xl_skip', false);
      syncSkip();
      rebuildCycle();
    }
    if (next) renderMarks();
  }

  /* ---------------------------------------------------------- 分段导航 */

  function chapterIndexNow() {
    var seg = currentSeg();
    if (!seg) return -1;
    var list = segmentsOf(seg.cid);
    if (!list || !list.length) return -1;
    var off = seg.t0 + (cyclePos() - seg.start);
    for (var i = 0; i < list.length; i++) {
      if (off < list[i].end) return i;
    }
    return -1;
  }

  // 标签按「时间点落在该段区间内」匹配，而不是精确匹配起点：
  // 这样重新跑自动分段后（段落边界会变），已有标签仍然能对上。
  function labelFor(lab, seg) {
    var exact = lab[String(seg.start)];
    if (exact) return exact;
    var best = null;
    for (var k in lab) {
      if (!Object.prototype.hasOwnProperty.call(lab, k)) continue;
      var t = parseInt(k, 10);
      if (t >= seg.start && t < seg.end) {
        if (!best || t < best.t) best = { t: t, v: lab[k] };
      }
    }
    return best ? best.v : null;
  }

  function renderChapters() {
    var seg = currentSeg();
    if (!seg) {
      el.stripLeftBody.innerHTML = '<div class="chap-empty">—</div>';
      el.chapList.innerHTML = '<div class="chap-empty">—</div>';
      return;
    }

    var p = seg.program;
    var isMusic = !!MUSIC_CATS[p.category];
    var groups = p.parts.map(function (part) {
      return { cid: String(part.cid), page: part.page, segs: segmentsOf(part.cid) || [] };
    });
    var total = groups.reduce(function (n, g) { return n + g.segs.length; }, 0);
    var songs = (window.SETLISTS || {})[String(seg.cid)] || [];
    var sub = [];
    if (songs.length) sub.push('歌单 ' + songs.length + ' 首');
    if (total) sub.push('共 ' + total + ' 段');
    el.stripLeftSub.textContent = sub.join(' · ');
    el.chaptersSub.textContent = sub.join(' · ');

    // 歌单（只有部分直播的浮层里才有）
    var songHtml = '';
    if (songs.length) {
      songHtml = '<div class="setlist"><div class="setlist-h">本场歌单（演唱顺序）</div>'
        + songs.map(function (s, i) {
            return '<div class="song"><span class="si">' + (i + 1) + '</span>'
              + '<span class="sn">' + esc(s) + '</span></div>';
          }).join('')
        + '</div>';
    }

    var segHtml = '';
    if (!total) {
      segHtml = '<div class="chap-empty">本场暂无分段。可用 '
        + '<code>tools/auto_segments.py</code> 自动标注，或在「标注」里手工标。</div>';
    } else {
      var curIdx = chapterIndexNow();
      var n = 0;
      groups.forEach(function (g) {
        if (!g.segs.length) return;
        if (groups.length > 1) segHtml += '<div class="chap-part">分P ' + g.page + '</div>';
        var same = String(seg.cid) === g.cid;
        var lab = (window.SEGLABELS || {})[g.cid] || {};
        g.segs.forEach(function (s, i) {
          n++;
          var cls = 'chap';
          if (same) {
            if (i === curIdx) cls += ' active';
            else if (curIdx >= 0 && i < curIdx) cls += ' past';
          }
          var L = labelFor(lab, s);
          // 没有读到任何画面文字的段：歌曲场按段长估算大约几首（中位一首约 3.8 分钟），
          // 其余场次没有歌，按「第 N 段」定位。不用「演唱」这类每行都一样的模糊标注。
          var est = Math.max(1, Math.round((s.end - s.start) / 228));
          var text = (L && L.label) ? L.label
            : (isMusic ? ('约 ' + est + ' 首') : ('第 ' + n + ' 段'));
          var tip = [];
          if (L && L.sub) tip.push(L.sub);
          tip.push(fmtClock(s.start) + ' – ' + fmtClock(s.end)
                   + '（' + ((s.end - s.start) / 60).toFixed(1) + ' 分钟）');
          if (!L || !L.label) tip.push(isMusic
            ? '该场画面未显示歌名或歌词，此处按段长估算'
            : '该场画面未显示文字信息，此处按段落定位');
          segHtml += '<button class="' + cls + '" data-cid="' + g.cid
            + '" data-start="' + s.start + '" title="' + esc(tip.join(' · ')) + '">'
            + '<span class="ci">' + n + '</span>'
            + '<span class="ct">' + fmtClock(s.start) + '</span>'
            + '<span class="cl">' + esc(text) + '</span>'
            + '<span class="cd">' + ((s.end - s.start) / 60).toFixed(1) + ' 分</span>'
            + '</button>';
        });
      });
    }

    var html = songHtml + segHtml;
    el.stripLeftBody.innerHTML = html;
    el.chapList.innerHTML = html;
    centerActive();
  }

  // 让当前片段保持在可视区域中间（只在重渲染时执行，不干扰手动滚动）
  function centerActive() {
    [el.stripLeftBody, el.chapList].forEach(function (box) {
      if (!box) return;
      var a = box.querySelector('.chap.active');
      if (!a) return;
      box.scrollTop = Math.max(0, a.offsetTop - box.clientHeight / 2 + a.clientHeight / 2);
    });
  }

  // 跳到某个片段起点：同一单元内直接 seek（瞬间完成），跨单元才重新取流
  function jumpToSegment(cid, start) {
    var segs = state.cycle.segments;
    for (var i = 0; i < segs.length; i++) {
      var u = segs[i];
      if (String(u.cid) !== String(cid)) continue;
      if (start < u.t0 || start >= u.t0 + u.duration) continue;

      var inPart = start - u.t0;                 // 该分P 内的秒数
      if (i === state.segIndex && state.mediaBase !== null && el.player.readyState > 0) {
        var targetCycle = u.start + inPart;
        state.mediaBase = inPart;
        state.cycleBase = targetCycle;
        try { el.player.currentTime = inPart; } catch (e) { /* 忽略 */ }
        el.player.play().catch(function () { /* 忽略 */ });
      } else {
        var total = state.cycle.total;
        state.drift = (u.start + inPart) - (now() - CFG.epoch) % total;
        state.drift = ((state.drift % total) + total) % total;
        state.mediaBase = null;
        applyPlayer(true);
      }
      if (state.view === 'schedule') renderSchedule();
      return;
    }
  }

  function renderUpNext() {
    if (!state.cycle || !state.cycle.segments.length) {
      el.stripRightBody.innerHTML = '<div class="chap-empty">—</div>';
      return;
    }
    var segs = state.cycle.segments, n = segs.length;
    var pos = cyclePos(), i = findSeg(pos);
    var cursor = now() - (pos - segs[i].start) + segs[i].duration;
    var out = [];
    var lastBvid = segs[i].bvid;

    for (var step = 1; step < n && out.length < 14; step++) {
      var s = segs[(i + step) % n];
      if (s.bvid !== lastBvid) {
        out.push({ p: s.program, at: cursor });
        lastBvid = s.bvid;
      }
      cursor += s.duration;
    }

    el.stripRightBody.innerHTML = out.map(function (o) {
      return '<button class="chap" data-bvid="' + o.p.bvid + '">'
        + '<span class="ct">' + esc(o.p.title) + '</span>'
        + '<span class="cd">' + hhmm(new Date(o.at * 1000)) + '</span>'
        + '</button>';
    }).join('');
  }

  /* ---------------------------------------------------------- 界面语言 */

  var hantConv = null;
  var converting = false;
  var hantObserver = null;

  function convertTree(root) {
    if (!hantConv || converting) return;
    converting = true;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = walker.nextNode())) {
      var v = n.nodeValue;
      if (v && /[\u4e00-\u9fff]/.test(v)) {
        var w = hantConv(v);
        if (w !== v) n.nodeValue = w;
      }
    }
    converting = false;
  }

  // 转出来的新内容也要跟着转，否则列表/面板重渲染后又会变回简体
  function applyHant() {
    convertTree(document.body);
    if (hantObserver) return;
    hantObserver = new MutationObserver(function (muts) {
      if (converting) return;
      muts.forEach(function (m) {
        Array.prototype.forEach.call(m.addedNodes, function (nd) {
          if (nd.nodeType === 1) convertTree(nd);
          else if (nd.nodeType === 3 && nd.nodeValue && hantConv) {
            nd.nodeValue = hantConv(nd.nodeValue);
          }
        });
      });
    });
    hantObserver.observe(document.body, { childList: true, subtree: true });
  }

  function setLang(lang, silent) {
    store.set('xl_lang', lang);
    if (lang !== 'zh-Hant') {
      if (!silent) location.reload();
      return;
    }
    if (window.OpenCC) {
      hantConv = OpenCC.Converter({ from: 'cn', to: 'tw' });
      applyHant();
      return;
    }
    // 词典约 1 MB，只在真正切到繁體时才下载，不影响首屏
    var sc = document.createElement('script');
    sc.src = 'assets/opencc-cn2t.js';
    sc.onload = function () {
      hantConv = OpenCC.Converter({ from: 'cn', to: 'tw' });
      applyHant();
    };
    sc.onerror = function () {
      store.set('xl_lang', 'zh-Hans');
      if (el.lang) el.lang.value = 'zh-Hans';
      el.npMeta.textContent = '繁體詞典載入失敗，已回到簡體';
    };
    document.head.appendChild(sc);
  }

  /* ---------------------------------------------------------- 服务自检 */

  // 页面必须通过 tools/serve.py 打开：直接双击 index.html 或走静态预览时，
  // /api/* 根本不存在，会表现为「二维码 Failed to fetch」+「播放卡死」。
  // 这里主动探测一次并把原因写在画面上，而不是让用户对着黑屏猜。
  function checkServer() {
    var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 5000);
    var opt = ctl ? { signal: ctl.signal } : {};
    // 这里只要确认「本机服务在不在」——用 /api/ping（纯本机、零上游请求）。
    // 原来打的是 /api/status，而它每次都会去问 B 站的 nav（约 100~150ms），
    // 于是每次加载都白等两次上游往返，正好和播放器抢带宽。
    return fetch('/api/ping', opt)
      .then(function (r) {
        clearTimeout(timer);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        state.offline = false;
        el.banner.hidden = true;
        return d;
      })
      .catch(function () {
        state.offline = true;
        el.banner.innerHTML = '<div>'
          + '<b>本机服务没有连上，所以无法播放。</b><br><br>'
          + '请不要直接双击 <code>index.html</code>，也不要在静态预览里看。<br>'
          + '正确做法：双击项目里的 <code>启动.bat</code>，<br>'
          + '然后访问 <code>http://127.0.0.1:8765/</code>。<br><br>'
          + '（播放需要本机服务代取 B 站的流：浏览器无法直接播放它。）'
          + '</div>';
        el.banner.hidden = false;
        el.npTitle.textContent = '未连接本机服务';
        el.npMeta.textContent = '播放需要 tools/serve.py 提供代理';
        return null;
      });
  }

  /* ---------------------------------------------------------- 小路状态 */

  // 右下角悬浮按钮 + 展开面板。
  //   数据源：/api/status-board（每次打开面板现取，服务端不再设有效缓存）
  //   新内容：拿实时清单里最新的几集来示意「小路最近发了什么」。
  //   为什么不用 B 站「动态」接口：它对服务端请求固定返回 412（见 serve.py 注释），
  //   所以这里用「最新回放 + 空间链接」表达同一件事，并如实标注来源。
  var statusState = {
    open: false,
    loading: false,
    data: null,
    seenPub: null          // 上次见到的最新投稿时间戳，用来判断「有新内容」
  };

  /* 「已读水位」必须按板块分开存：共用一份的话，切到副站时水位还是主站的时间戳，
     对方最新几集会被全部标成「新」（假红点）。 */
  var STATUS_SEEN_BASE = 'komichi.seenPub';
  function statusSeenKey() { return STATUS_SEEN_BASE + ':' + (ST || 'main'); }

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式忽略 */ } }

  function newPrograms(n) {
    var list = (state.all || []).slice().sort(function (a, b) {
      return b.pubdate - a.pubdate;
    });
    return list.slice(0, n || 3);
  }

  function hasNewContent() {
    var p = newPrograms(1)[0];
    if (!p) return false;
    var seen = parseInt(lsGet(statusSeenKey()) || '0', 10) || 0;
    return seen > 0 && p.pubdate > seen;
  }

  function markSeen() {
    var p = newPrograms(1)[0];
    if (p) lsSet(statusSeenKey(), String(p.pubdate));
  }

  function relDay(ts) {
    var d = new Date(ts * 1000);
    var days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return '今天 ' + hhmm(d);
    if (days === 1) return '昨天 ' + hhmm(d);
    if (days < 30) return days + ' 天前';
    return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  }

  function renderStatus() {
    var d = statusState.data;
    if (!d) return;

    var live = d.live || {};
    var on = !!live.living;
    var html = '';

    // ① 开播状态
    html += '<div class="status-block">'
      + '<div class="status-block-h">开播情况</div>'
      + '<a class="status-live' + (on ? ' on' : '') + '" href="' + esc(live.url || '#')
      + '" target="_blank" rel="noopener" style="text-decoration:none">';
    if (live.face) {
      html += '<img class="status-live-face" src="' + esc(live.face) + '" alt="" '
        + 'referrerpolicy="no-referrer">';
    }
    html += '<div class="status-live-main">'
      + '<div class="status-live-row">'
      + '<span class="status-pill' + (on ? ' on' : '') + '">' + (on ? '直播中' : '未开播')
      + '</span><span>' + esc(live.uname || '四时小路Komichi') + '</span></div>';
    if (on) {
      if (live.title) html += '<div class="status-live-title">' + esc(live.title) + '</div>';
      var bits = [];
      if (live.parent_area && live.area) bits.push(live.parent_area + ' · ' + live.area);
      else if (live.area) bits.push(live.area);
      if (live.online) bits.push('人气 ' + fmtNum(live.online));
      if (live.start > 0) bits.push('已播 ' + fmtDur(Math.max(0, Math.floor(Date.now() / 1000) - live.start)));
      if (bits.length) html += '<div class="status-live-meta">' + esc(bits.join(' · ')) + '</div>';
    } else {
      html += '<div class="status-live-meta">'
        + (d.live_error ? '状态获取失败，显示的是上一次结果' : '点这里去直播间')
        + '</div>';
    }
    html += '</div></a></div>';

    // ② 最新回放（有新投稿时打标）
    var recent = newPrograms(3);
    if (recent.length) {
      var seen = parseInt(lsGet(statusSeenKey()) || '0', 10) || 0;
      html += '<div class="status-block">'
        + '<div class="status-block-h">最新回放</div>';
      recent.forEach(function (p) {
        var isNew = seen > 0 && p.pubdate > seen;
        html += '<a class="status-item" href="' + esc(p.url) + '" target="_blank" rel="noopener">'
          + '<img class="status-item-thumb" src="' + esc(thumbSrc(p, '240w_150h_1c.webp')) + '" alt="" '
          + 'loading="lazy">'
          + '<div class="status-item-main">'
          + '<div class="status-item-title">' + esc(p.title)
          + (isNew ? '<span class="status-new">新</span>' : '') + '</div>'
          + '<div class="status-item-meta">' + esc(p.category) + ' · '
          + esc(relDay(p.pubdate)) + ' · ' + len2(p) + '</div>'
          + '</div></a>';
      });
      html += '</div>';
    }

    if (d.live_error || window.__LIVE_ERR) {
      html += '<div class="status-block"><div class="status-err">'
        + (d.live_error ? '开播接口：' + esc(d.live_error) + '<br>' : '')
        + (window.__LIVE_ERR ? '清单接口：' + esc(window.__LIVE_ERR) : '')
        + '</div></div>';
    }

    el.statusBody.innerHTML = html;
    el.statusLoading.hidden = true;

    // 底部：取数时间 + 空间入口
    var t = d.at ? new Date(d.at * 1000) : new Date();
    el.statusFoot.innerHTML = '<span>更新于 ' + hhmm(t) + ':' + pad2(t.getSeconds()) + '</span>'
      + '<a href="https://space.bilibili.com/' + esc(d.mid || '1512246445')
      + '/dynamic" target="_blank" rel="noopener">到 B 站看动态 →</a>';

    // 按钮态：在播 / 有新内容
    var living = false;
    if (d.live) living = !!d.live.living;
    el.statusFab.classList.toggle('living', living);
    el.statusFabDot.hidden = !(hasNewContent() || living);
  }

  function len2(p) {
    var n = (p.parts || []).length;
    return n > 1 ? n + ' 个分P' : fmtDur(p.duration);
  }

  function loadStatus(refresh) {
    if (statusState.loading) return;
    statusState.loading = true;
    el.statusLoading.hidden = false;
    el.statusLoading.textContent = '正在获取最新状态…';

    var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 12000);
    var url = '/api/status-board' + (refresh ? '?refresh=1' : '');

    fetch(url, ctl ? { signal: ctl.signal } : undefined)
      .then(function (r) {
        clearTimeout(timer);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        statusState.data = d;
        statusState.loading = false;
        renderStatus();
      })
      .catch(function (e) {
        clearTimeout(timer);
        statusState.loading = false;
        el.statusLoading.hidden = true;
        el.statusBody.innerHTML = '<div class="status-block"><div class="status-err">'
          + '拿不到' + FAB_LABEL + '：' + esc(e && e.message ? e.message : String(e))
          + '<br><br>请确认通过 <code>启动.bat</code> 打开页面（本机服务未启动时无法取数据）。'
          + '</div></div>';
        el.statusFoot.innerHTML = '<span>—</span>';
      });
  }

  function statusOpen() {
    statusState.open = true;
    el.statusPanel.hidden = false;
    el.statusFab.setAttribute('aria-expanded', 'true');
    loadStatus(true);            // 每次展开都现取，保证看到的是最新的
  }

  function statusClose() {
    statusState.open = false;
    el.statusPanel.hidden = true;
    el.statusFab.setAttribute('aria-expanded', 'false');
    markSeen();                  // 收起即视为「已看过」，红点消失
    el.statusFabDot.hidden = true;
  }

  function statusToggle() {
    if (statusState.open) statusClose(); else statusOpen();
  }

  /* ---------------------------------------------------------- 扫码登录 */

  var qrTimer = null;

  function loginClose() {
    el.loginPanel.hidden = true;
    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  }

  function refreshStatus(force) {
    // 服务端缓存 30 秒；登录/登出之后必须拿新的，所以那两处传 true
    fetch('/api/status' + (force ? '?refresh=1' : ''))
      .then(function (r) { return r.json(); }).then(function (d) {
      el.loginLabel.textContent = d.logged ? (d.uname || '已登录') : '登录';
      el.btnLogin.setAttribute('title', d.logged
        ? ('已登录：' + (d.uname || '') + (d.vip ? '（大会员）' : ''))
        : '扫码登录 B 站，解锁更高清晰度');
      el.btnLogout.hidden = !d.logged;
    }).catch(function () { /* 服务未启动时忽略 */ });
  }

  // 全程在本页完成，不跳转外部页面
  function openLogin() {
    el.loginPanel.hidden = false;
    el.loginQr.innerHTML = '';
    el.loginStatus.textContent = '正在获取二维码…';
    el.btnLogout.hidden = true;

    fetch('/api/login/qrcode').then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { el.loginStatus.textContent = '获取二维码失败：' + d.error; return; }
      el.loginQr.innerHTML = '';
      try {
        new QRCode(el.loginQr, {
          text: d.url, width: 152, height: 152,
          correctLevel: QRCode.CorrectLevel.M
        });
      } catch (e) {
        el.loginStatus.textContent = '二维码渲染失败：' + e.message;
        return;
      }
      el.loginStatus.textContent = '用 B 站手机 App 扫码登录';

      var key = d.key;
      if (qrTimer) clearInterval(qrTimer);
      qrTimer = setInterval(function () {
        fetch('/api/login/poll?key=' + encodeURIComponent(key))
          .then(function (r) { return r.json(); })
          .then(function (s) {
            if (s.error) { el.loginStatus.textContent = s.error; return; }
            if (s.code === 0) {
              clearInterval(qrTimer); qrTimer = null;
              el.loginStatus.textContent = s.jct === false
                ? '登录成功，但没取到 bili_jct（发弹幕要用），再扫一次通常就有了'
                : '登录成功，正在按新清晰度重新加载…';
              setTimeout(function () {
                loginClose();
                refreshStatus(true);
                refreshLiveCred();     // 弹幕凭据随登录一起到手，立刻反映到直播间页
                state.mediaBase = null;
                applyPlayer(true);
              }, 800);
            } else if (s.code === 86090) {
              el.loginStatus.textContent = '已扫码，请在手机上点确认';
            } else if (s.code === 86038) {
              el.loginStatus.textContent = '二维码已失效，请重新打开';
              clearInterval(qrTimer); qrTimer = null;
            } else {
              el.loginStatus.textContent = '等待扫码…';
            }
          }).catch(function () { /* 网络抖动忽略，下次轮询继续 */ });
      }, 2000);
    }).catch(function (e) {
      el.loginStatus.textContent = '获取二维码出错：' + e.message;
    });
  }

  function toggleChapters(on) {
    var next = typeof on === 'boolean' ? on : el.chaptersPanel.hidden;
    el.chaptersPanel.hidden = !next;
    el.btnChapters.setAttribute('aria-expanded', String(next));
    if (next) renderChapters();
  }

  function syncSound() {
    el.btnMutedef.textContent = '默认静音：' + (state.mutedDefault ? '开' : '关');

    // 「进入直播」在两个位置（Hero 与信息卡）都有，统一同步；剧场模式下信息卡是唯一入口
    document.querySelectorAll('[data-action="unmute"]').forEach(function (b) {
      b.textContent = state.muted ? '进入直播' : '声音已开启';
      b.disabled = !state.muted;
      b.classList.toggle('done', !state.muted);
    });

    el.muteHint.textContent = state.muted
      ? '静音播放中 · 点「进入直播」开启声音'
      : '已开启声音 · 若浏览器拦截自动播放，请点播放器内的播放键';
  }

  function renderChips() {
    var present = {};
    state.all.forEach(function (p) { present[p.category] = 1; });
    var cats = CAT_ORDER.filter(function (c) { return present[c]; });

    el.chips.innerHTML = ['全部'].concat(cats).map(function (c) {
      var on = c === '全部' ? !Object.keys(state.cats).length : !!state.cats[c];
      return '<button class="chip' + (on ? ' active' : '') + '" data-cat="' + c + '">' + c + '</button>';
    }).join('');
  }

  // 保证某节目在当前排期池内：不在就把它的分类加回筛选并重建时间轴
  function ensureInCycle(bvid) {
    for (var i = 0; i < state.all.length; i++) {
      var p = state.all[i];
      if (p.bvid !== bvid) continue;
      if (Object.keys(state.cats).length && !state.cats[p.category]) {
        state.cats[p.category] = 1;
        renderChips();
        renderList();
        rebuildCycle();
      }
      return true;
    }
    return false;
  }

  function playProgram(bvid) {
    ensureInCycle(bvid);
    var segs = state.cycle.segments;
    for (var i = 0; i < segs.length; i++) {
      if (segs[i].bvid === bvid) {
        var total = state.cycle.total;
        state.drift = segs[i].start - (now() - CFG.epoch) % total;
        state.drift = ((state.drift % total) + total) % total;
        applyPlayer(true);
        if (state.view === 'schedule') renderSchedule();
        return;
      }
    }
  }

  function bind() {
    // 小路状态：悬浮按钮展开/收起；点面板外或按 Esc 收起
    el.statusFab.addEventListener('click', statusToggle);
    el.btnStatusClose.addEventListener('click', statusClose);
    el.btnStatusRefresh.addEventListener('click', function () { loadStatus(true); });

    // 主界面「更新数据」：手动跑一次分段（补齐所有缺分段的分P）
    el.segBtn.addEventListener('click', segRefresh);

    document.addEventListener('click', function (e) {
      if (!statusState.open) return;
      if (el.statusDock.contains(e.target)) return;
      statusClose();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && statusState.open) statusClose();
    });

    el.chips.addEventListener('click', function (e) {
      var b = e.target.closest('.chip');
      if (!b) return;
      var c = b.getAttribute('data-cat');
      if (c === '全部') state.cats = {};
      else if (state.cats[c]) delete state.cats[c];
      else state.cats[c] = 1;
      renderChips();
      renderList();
      rebuildCycle();
    });

    // error 不冒泡，用捕获阶段统一处理；取不到图就隐藏，避免整排裂图
    ['rows', 'catRows'].forEach(function (key) {
      el[key].addEventListener('error', function (e) {
        if (!e.target || e.target.tagName !== 'IMG') return;
        e.target.style.display = 'none';
        if (String(e.target.src || '').indexOf('/api/img') >= 0) netFail();
      }, true);
    });

    el.rows.addEventListener('click', function (e) {
      var b = e.target.closest('[data-play]');
      if (b) playProgram(b.getAttribute('data-play'));
    });

    el.catRows.addEventListener('click', function (e) {
      var b = e.target.closest('[data-play]');
      if (b) playProgram(b.getAttribute('data-play'));
    });

    el.schRows.addEventListener('click', function (e) {
      var b = e.target.closest('[data-play]');
      if (b) playProgram(b.getAttribute('data-play'));
    });

    el.catCards.addEventListener('click', function (e) {
      var b = e.target.closest('[data-cat-focus]');
      if (!b) return;
      var v = b.getAttribute('data-cat-focus');
      state.catFocus = v || null;
      renderCategories();
    });

    document.querySelectorAll('[data-horizon]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.horizon = parseInt(b.getAttribute('data-horizon'), 10) || 0;
        document.querySelectorAll('[data-horizon]').forEach(function (x) {
          x.classList.toggle('active', x === b);
        });
        renderSchedule();
      });
    });

    el.q.addEventListener('input', function () {
      state.q = el.q.value.trim();
      state.page = 1;
      renderList();
    });

    document.querySelectorAll('.sort').forEach(function (b) {
      b.addEventListener('click', function () {
        state.sort = b.getAttribute('data-sort');
        document.querySelectorAll('.sort').forEach(function (x) {
          x.className = 'sort' + (x.getAttribute('data-sort') === state.sort ? ' active' : '');
        });
        state.page = 1;
        renderList();
      });
    });

    el.prev.addEventListener('click', function () {
      if (state.page > 1) { state.page--; renderList(); }
    });
    el.next.addEventListener('click', function () {
      state.page++; renderList();
    });

    document.querySelectorAll('[data-action]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (b.getAttribute('data-action') === 'unmute') {
          state.muted = false;
          el.player.muted = false;      // 原生播放器直接改属性，不打断播放
          syncSound();
        } else {
          state.drift = 0;              // 回到直播：重新按挂钟对齐
          state.mediaBase = null;
          applyPlayer(true);
        }
      });
    });

    el.btnMutedef.addEventListener('click', function () {
      state.mutedDefault = !state.mutedDefault;
      store.set(KEY_MUTED, state.mutedDefault);
      state.muted = state.mutedDefault;
      el.player.muted = state.muted;
      syncSound();
    });

    // 清晰度下拉：直播间里它是**直播画质**（原画/蓝光/超清），换挡要重开直播流；
    // 其它页面是回放分辨率，重新取该清晰度的流并从当前位置继续，都不离开本页
    el.quality.addEventListener('change', function () {
      var qn = parseInt(el.quality.value, 10);
      if (state.view === 'broadcast') {
        if (qn) startLive(qn);
        return;
      }
      var seg = state.cycle.segments[state.segIndex];
      if (!seg) return;
      var anchor = cyclePos();               // 当前频道位置（视频未就绪时用期望位置）
      var keep = seg.t0 + Math.max(0, anchor - seg.start);
      state.qn = qn || 80;
      store.set('xl_qn', state.qn);
      loadMedia(seg, keep, anchor);
    });

    el.btnTheater.addEventListener('click', function () { toggleTheater(); });

    // 播放地址有时效，过期或网络抖动时重新取一次，不要让画面卡死
    el.player.addEventListener('error', function () {
      if (state.offline) return;
      var seg = currentSeg();
      if (!seg) return;
      var k = seg.bvid + '#' + seg.page;
      if (state.retried[k]) {
        el.npMeta.textContent = '播放失败（已重试过一次）。点「回到直播」或换一集试试。';
        return;
      }
      state.retried[k] = 1;
      el.npMeta.textContent = '播放中断，正在重新取流…';
      setTimeout(function () {
        state.mediaBase = null;
        state.playingKey = null;
        applyPlayer(true);
      }, 1200);
    });

    el.lang.value = store.get('xl_lang', 'zh-Hans');
    el.lang.addEventListener('change', function () { setLang(el.lang.value); });

    el.btnQuality.addEventListener('click', function () { toggleQuality(); });

    /* ------------------------ 原生播放控件 ------------------------ */

    el.btnPlay = el.ctrlPlay;
    el.btnMute = el.ctrlMute;
    el.btnFs = el.ctrlFs;
    el.btnPlay.addEventListener('click', function () {
      if (!el.player || !el.player.src) return;
      if (el.player.paused) el.player.play().catch(function () {});
      else el.player.pause();
    });
    el.btnMute.addEventListener('click', function () {
      if (!el.player || !el.player.src) return;
      el.player.muted = !el.player.muted;
    });
    el.ctrlVol.addEventListener('input', function () {
      if (!el.player || !el.player.src) return;
      el.player.volume = parseFloat(this.value);
      el.player.muted = el.player.volume === 0;
    });
    el.btnFs.addEventListener('click', function () {
      if (!el.player || !el.player.requestFullscreen) return;
      try { el.player.requestFullscreen(); } catch (e) { /* 忽略 */ }
    });
    function seekAt(clientX) {
      if (!el.player || !isFinite(el.player.duration) || el.player.duration <= 0) return;
      var box = el.ctrlProgress.getBoundingClientRect();
      var r = Math.min(1, Math.max(0, (clientX - box.left) / box.width));
      try { el.player.currentTime = r * el.player.duration; } catch (e) { /* 忽略 */ }
    }
    el.ctrlProgress.addEventListener('mousedown', function (e) {
      seekAt(e.clientX);
      var move = function (ev) { seekAt(ev.clientX); };
      var up = function () {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
      e.preventDefault();
    });

    el.player.addEventListener('play', function () {
      el.ctrlPlay.classList.add('playing');
      el.playerBox.classList.remove('paused');
    });
    el.player.addEventListener('pause', function () {
      el.ctrlPlay.classList.remove('playing');
      el.playerBox.classList.add('paused');
    });
    el.player.addEventListener('volumechange', function () {
      el.btnMute.classList.toggle('muted', el.player.muted || el.player.volume === 0);
      el.ctrlVol.value = el.player.muted ? 0 : el.player.volume;
      el.btnMute.setAttribute('aria-label', el.player.muted ? '取消静音' : '静音');
    });
    el.player.addEventListener('loadedmetadata', function () {
      if (isFinite(el.player.duration)) el.ctrlDur.textContent = fmtClock(el.player.duration);
    });
    el.player.addEventListener('timeupdate', function () {
      el.ctrlCur.textContent = fmtClock(el.player.currentTime);
      if (!isFinite(el.player.duration) || el.player.duration <= 0) return;
      var r = el.player.currentTime / el.player.duration * 100;
      el.ctrlFilled.style.width = r + '%';
      el.ctrlDot.style.left = r + '%';
      try {
        if (el.player.buffered.length) {
          el.ctrlBuffer.style.width = (el.player.buffered.end(el.player.buffered.length - 1)
            / el.player.duration * 100) + '%';
        }
      } catch (e) { /* 忽略 */ }
    });

    el.btnMute.classList.toggle('muted', el.player.muted || el.player.volume === 0);
    el.ctrlVol.value = el.player.muted ? 0 : el.player.volume;
    el.btnQpClose.addEventListener('click', function () { toggleQuality(false); });

    el.btnLogin.addEventListener('click', openLogin);
    el.btnLogin2.addEventListener('click', function () { toggleQuality(false); openLogin(); });
    el.btnLoginClose.addEventListener('click', loginClose);
    el.btnLogout.addEventListener('click', function () {
      fetch('/api/logout').then(function () { return refreshStatus(true); }).then(function () {
        state.mediaBase = null;
        applyPlayer(true);
      });
    });

    el.btnChapters.addEventListener('click', function () { toggleChapters(); });

    [el.stripLeftBody, el.chapList].forEach(function (box) {
      box.addEventListener('click', function (e) {
        var b = e.target.closest('.chap[data-cid]');
        if (b) jumpToSegment(b.getAttribute('data-cid'), parseInt(b.getAttribute('data-start'), 10));
      });
    });

    el.stripRightBody.addEventListener('click', function (e) {
      var b = e.target.closest('.chap[data-bvid]');
      if (b) playProgram(b.getAttribute('data-bvid'));
    });

    el.btnSkip.addEventListener('click', function () {
      state.skip = !state.skip;
      store.set('xl_skip', state.skip);
      syncSkip();
      rebuildCycle();
      if (state.view === 'schedule') renderSchedule();
    });

    el.btnMark.addEventListener('click', function () { toggleMarking(); });

    el.btnMarkUndo.addEventListener('click', function () {
      var seg = currentSeg();
      if (!seg) return;
      for (var i = state.marks.length - 1; i >= 0; i--) {
        if (state.marks[i].cid === String(seg.cid)) { state.marks.splice(i, 1); break; }
      }
      store.set('xl_marks', state.marks);
      renderMarks();
    });

    el.btnMarkClear.addEventListener('click', function () {
      var seg = currentSeg();
      if (!seg) return;
      var cid = String(seg.cid);
      state.marks = state.marks.filter(function (m) { return m.cid !== cid; });
      store.set('xl_marks', state.marks);
      renderMarks();
    });

    // 重载播放器：走 applyPlayer(true)，按时间轴重算偏移，所以位置不丢
    el.btnReloadPlayer.addEventListener('click', function () {
      applyPlayer(true);
      el.btnReloadPlayer.textContent = '已重载';
      setTimeout(function () { el.btnReloadPlayer.textContent = '重载播放器（保持进度）'; }, 1600);
    });

    document.addEventListener('keydown', function (e) {
      var tag = (e.target && e.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;

      if ((e.key === 'm' || e.key === 'M') && state.marking) {
        e.preventDefault();
        addMark();
        return;
      }
      if (e.key !== 'Escape') return;
      if (!el.qualityPanel.hidden) { toggleQuality(false); return; }
      if (!el.markPanel.hidden) { toggleMarking(false); return; }
      if (!el.chaptersPanel.hidden) { toggleChapters(false); return; }
      if (document.body.classList.contains('theater')) toggleTheater(false);
    });

    el.btnRandom.addEventListener('click', function () {
      var pool = filtered();
      if (!pool.length) return;
      var weight = pool.reduce(function (n, p) { return n + p.score; }, 0);
      var r = Math.random() * weight;
      for (var i = 0; i < pool.length; i++) {
        r -= pool[i].score;
        if (r <= 0) { playProgram(pool[i].bvid); break; }
      }
    });

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) return;
      applyPlayer(false);
    });

    window.addEventListener('hashchange', route);
  }

  /* ---------------------------------------------------------- 启动 */

  function tick() {
    if (!state.cycle || !state.cycle.segments.length) return;
    if (state.view === 'broadcast') return;   // 直播流占着播放器，回放的换段逻辑别来抢
    if (state.loading) return;              // 取流/定位期间不做换段判断
    var pos = cyclePos();
    var i = findSeg(pos);
    if (i !== state.segIndex) {
      if (state.cycleStale) {
        // 后台抓到的新清单等到这里才生效：反正马上要换段，顺手把循环重建了
        state.cycleStale = false;
        rebuildCycle();
        return;
      }
      state.segIndex = i;
      state.playingKey = null;
      applyPlayer(true);
      if (state.view === 'schedule') renderSchedule();
      if (state.marking) renderMarks();
      return;
    }
    var seg = state.cycle.segments[i];
    var off = Math.max(0, pos - seg.start);
    // 快到段尾就把下一段预热，切换时不必等 B站
    if (seg.duration - off < 25 && state.cycle.segments.length > 1) {
      prefetchSegment(state.cycle.segments[(i + 1) % state.cycle.segments.length]);
    }
    el.npBar.style.width = Math.min(100, off / seg.duration * 100) + '%';
    el.npPos.textContent = fmtClock(off);
    // 同一单元内跨过片段边界时，只更新高亮，不打断播放
    var key = seg.bvid + '#' + seg.page + '#' + chapterIndexNow();
    if (key !== state.chapKey) {
      state.chapKey = key;
      renderChapters();
    }
  }

  function updateMetaText() {
    if (!state.meta.count) return;
    // 实时抓取时 generated_at 是「刚刚」，离线快照则是采集时刻。
    // 明确写出来，用户能一眼看出看到的是不是最新数据。
    var when = '';
    if (state.meta.generated_at) {
      // 实时抓取给的是 epoch 秒（数字），内置的离线快照给的是
      // 「YYYY-MM-DD HH:MM:SS」字符串（tools/collect.py 写的）。
      // 一律按 epoch 乘 1000 就会得到 Invalid Date，页面上显示成 NaN-NaN NaN:NaN。
      var raw = state.meta.generated_at;
      var g = typeof raw === 'number'
        ? new Date(raw * 1000)
        : new Date(String(raw).replace(' ', 'T'));
      if (!isNaN(g.getTime())) {
        when = state.meta.live
          ? ' · 实时数据 ' + hhmm(g)
          : ' · 快照于 ' + (g.getMonth() + 1) + '-' + pad2(g.getDate())
            + ' ' + hhmm(g);
      }
    }
    el.meta.textContent = '数据源：' + (state.meta.up_name || '')
      + ' · 系列「直播回放」 · ' + state.meta.count + ' 个节目' + when;
  }

  function boot(data) {
    state.all = data.programs || [];
    state.meta = data.meta || {};
    state.muted = state.mutedDefault;

    updateMetaText();

    renderChips();
    renderList();
    syncSound();
    syncSkip();

    var h = parseInt(state.horizon, 10);
    document.querySelectorAll('[data-horizon]').forEach(function (x) {
      x.classList.toggle('active', (parseInt(x.getAttribute('data-horizon'), 10) || 0) === h);
    });

    route();
    /* 清单刚渲染完：让内容淡入一次。切换板块是**整页重载**，没有这一下，
       新页面会「啪」地出现在眼前（副站还要等接口，落差更明显）。
       路由时已经播过的话就不重复播。 */
    if (!viewEntered) enterView(0);      // 首屏没有「前后」可言，只淡入
    moveTabPill(true);                   // 胶囊首次就位（不带过渡）
    finishSwitchVeil();                  // 若是「切板块」过来的，撤掉过渡遮罩
    prefetchOthers();                     // 空闲预热其它板块（切过去就不用现抓）
    if (store.get('xl_lang') === 'zh-Hant') setLang('zh-Hant', true);
    checkServer().then(function () {
      if (state.offline) return;        // 没有服务就不去取流，避免一堆无谓的失败请求
      rebuildCycle();
      refreshStatus();
      setInterval(tick, CFG.tickMs);

      // 状态按钮的红点：页面加载后先静静问一次开播状态；
      // 之后每分钟轮询一次，只在「在播」或有新投稿时亮起来 —— 不做弹窗骚扰。
      loadStatus(false);
      setInterval(function () {
        if (statusState.open) return;   // 面板开着时由用户手动刷新，避免抢焦点
        loadStatus(false);
      }, 60000);
    });
  }

  function fail(err) {
    el.rows.innerHTML = emptyRow(5, '未找到节目单数据。请先运行：<br><br>'
      + '<code>python tools/collect.py</code>');
    el.npTitle.textContent = '无数据';
    el.npMeta.textContent = String(err && err.message || err || '');
  }

  function init() {
    pageSetup();                 // 先报到：本服务靠页面心跳判断「网页是不是全关了」
    el = {
      netAlert: document.getElementById('net-alert'),
    keysList: document.getElementById('keys-list'),
    keysTip: document.getElementById('keys-tip'),
    keysReset: document.getElementById('keys-reset'),
    segAuto: document.getElementById('seg-auto'),
    segRun: document.getElementById('seg-run'),
    seriesRefresh: document.getElementById('series-refresh'),
    seriesState: document.getElementById('series-state'),
    segNote: document.getElementById('seg-note'),
    segState: document.getElementById('seg-state'),
    segCov: document.getElementById('seg-cov'),
    segWhen: document.getElementById('seg-when'),
    segBtn: document.getElementById('btn-seg-refresh'),
    segBtnText: document.getElementById('seg-btn-text'),
    segProg: document.getElementById('seg-prog'),
    segProgFill: document.getElementById('seg-prog-fill'),
    segProgText: document.getElementById('seg-prog-text'),
    protoLink: document.getElementById('proto-link'),
    protoHint: document.getElementById('proto-hint'),
    protoDirect: document.getElementById('proto-direct'),
    protoRegBtns: document.getElementById('proto-regbtns'),
    shortcutCreate: document.getElementById('shortcut-create'),
    shortcutState: document.getElementById('shortcut-state'),
    protoReg: document.getElementById('proto-reg'),
    protoUnreg: document.getElementById('proto-unreg'),
    protoState: document.getElementById('proto-state'),
    liveInfo: document.getElementById('live-info'),
    liveChat: document.getElementById('live-chat'),
    liveChatState: document.getElementById('live-chat-state'),
    liveCredState: document.getElementById('live-cred-state'),
    liveCredBox: document.getElementById('live-cred-box'),
    liveJct: document.getElementById('live-jct'),
    liveJctSave: document.getElementById('live-jct-save'),
    liveMsg: document.getElementById('live-msg'),
    liveSend: document.getElementById('live-send'),
    liveMsgSave: document.getElementById('live-msg-save'),
    quickRow: document.getElementById('quick-row'),
    quickLabel: document.getElementById('quick-label'),
    quickChips: document.getElementById('quick-chips'),
    quickEdit: document.getElementById('quick-edit'),
    liveResult: document.getElementById('live-result'),
    wheelMsg: document.getElementById('wheel-msg'),
    wheelMotto: document.getElementById('wheel-motto'),
    wheelInterval: document.getElementById('wheel-interval'),
    wheelCount: document.getElementById('wheel-count'),
    wheelStart: document.getElementById('wheel-start'),
    wheelStop: document.getElementById('wheel-stop'),
    wheelState: document.getElementById('wheel-state'),
    wheelPop: document.getElementById('wheel-pop'),
    wheelToggle: document.getElementById('wheel-toggle'),
    wheelClose: document.getElementById('wheel-close'),
    chips: document.getElementById('chips'),
      rows: document.getElementById('rows'),
      stat: document.getElementById('stat'),
      pageInfo: document.getElementById('page-info'),
      prev: document.getElementById('prev'),
      next: document.getElementById('next'),
      q: document.getElementById('q'),
      meta: document.getElementById('meta-line'),
      player: document.getElementById('player'),
      npCat: document.getElementById('np-cat'),
      npTitle: document.getElementById('np-title'),
      npMeta: document.getElementById('np-meta'),
      npBar: document.getElementById('np-bar'),
      npPos: document.getElementById('np-pos'),
      npDur: document.getElementById('np-dur'),
      liveBadge: document.getElementById('np-live'),
      muteHint: document.getElementById('mute-hint'),
      upnext: document.getElementById('next-list'),
      btnMutedef: document.getElementById('btn-mutedef'),
      btnRandom: document.getElementById('btn-random'),
      btnTheater: document.getElementById('btn-theater'),
      btnQuality: document.getElementById('btn-quality'),
      qualityPanel: document.getElementById('quality-panel'),
      btnQpClose: document.getElementById('btn-qp-close'),
      btnLogin: document.getElementById('btn-login'),
      playerBox: document.getElementById('player-box'),
      pvTitle: document.getElementById('pv-title'),
      ctrlBuffer: document.getElementById('ctrl-buffer'),
      ctrlDot: document.getElementById('ctrl-dot'),
      ctrlPlay: document.getElementById('ctrl-play'),
      ctrlMute: document.getElementById('ctrl-mute'),
      ctrlVol: document.getElementById('ctrl-vol'),
      ctrlFs: document.getElementById('ctrl-fs'),
      ctrlProgress: document.getElementById('ctrl-progress'),
      ctrlFilled: document.getElementById('ctrl-filled'),
      ctrlCur: document.getElementById('ctrl-cur'),
      ctrlDur: document.getElementById('ctrl-dur'),
      btnLogin2: document.getElementById('btn-login-2'),
      btnLoginClose: document.getElementById('btn-login-close'),
      btnLogout: document.getElementById('btn-logout'),
      banner: document.getElementById('offline-banner'),
      lang: document.getElementById('lang'),
      loginLabel: document.getElementById('login-label'),
      loginPanel: document.getElementById('login-panel'),
      loginQr: document.getElementById('login-qr'),
      loginStatus: document.getElementById('login-status'),
      quality: document.getElementById('quality'),
      btnReloadPlayer: document.getElementById('btn-reload-player'),
      btnSkip: document.getElementById('btn-skip'),
      btnChapters: document.getElementById('btn-chapters'),
      chaptersPanel: document.getElementById('chapters-panel'),
      chaptersSub: document.getElementById('chapters-sub'),
      chapList: document.getElementById('chap-list'),
      stripLeftSub: document.getElementById('strip-left-sub'),
      stripLeftBody: document.getElementById('strip-left-body'),
      stripRightBody: document.getElementById('strip-right-body'),
      btnMark: document.getElementById('btn-mark'),
      markPanel: document.getElementById('mark-panel'),
      markCur: document.getElementById('mark-cur'),
      markList: document.getElementById('mark-list'),
      markJson: document.getElementById('mark-json'),
      btnMarkUndo: document.getElementById('btn-mark-undo'),
      btnMarkClear: document.getElementById('btn-mark-clear'),
      schRows: document.getElementById('sch-rows'),
      schStat: document.getElementById('sch-stat'),
      catCards: document.getElementById('cat-cards'),
      catRows: document.getElementById('cat-rows'),
      catStat: document.getElementById('cat-stat'),
      aboutBody: document.getElementById('about-body'),
      statusDock: document.getElementById('status-dock'),
      statusFab: document.getElementById('status-fab'),
      statusFabDot: document.getElementById('status-fab-dot'),
      statusFabText: document.getElementById('status-fab-text'),
      statusPanelTitle: document.getElementById('status-panel-title'),
      statusPanel: document.getElementById('status-panel'),
      statusBody: document.getElementById('status-body'),
      statusLoading: document.getElementById('status-loading'),
      statusFoot: document.getElementById('status-foot'),
      btnStatusRefresh: document.getElementById('status-refresh'),
      btnStatusClose: document.getElementById('status-close')
    };

    bind();
    playArrivalSweep();

    // 数据来源优先级：
    //   ① /api/programs —— 由本机服务实时抓 B 站，打开页面即拿到最新投稿
    //   ② data/programs.js —— 内嵌离线快照（无服务或接口失败时用）
    //   ③ data/programs.json —— 最后兜底
    // 用「带超时的 Promise 竞速」而不是纯 fetch：接口卡住时不该让首屏一直转圈。
    function loadLocal() {
      /* 内置的这两份离线快照都是**主站（四时小路）**的。
         切到别的主播时拿它兜底 = 屏幕上写着羽啾、内容却是小路的，属于显示错人，
         所以副站一律返回空清单，让页面明确停在「这位还没有回放数据」上。 */
      /* 页面内置的那份快照只有主站的内容。多选时哪怕含主站也不能用它兜底 ——
         会把别人的内容顶掉，屏幕上就成了「选了两位、只显示一位」。 */
      if (!isMainOnly()) {
        return Promise.resolve({ programs: [], meta: { station: null, no_snapshot: true } });
      }
      if (window.PROGRAMS && window.PROGRAMS.programs && window.PROGRAMS.programs.length) {
        return Promise.resolve(window.PROGRAMS);
      }
      return fetch('data/programs.json').then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    /* index.html 里是写死加载主站那份 data/segments.js 的（主站行为不变）。
       副站的分段在各自数据目录里，这里单独取回覆盖 window.SEGMENTS —— 必须在 boot 之前
       完成，否则频道会拿主站的 cid 去匹配副站的分P，一个都对不上。 */
    // 从 segments.js 的文本里取出对象（格式固定是 `window.SEGMENTS = {...};`）
    function segObject(txt) {
      if (!txt) return null;
      var i = txt.indexOf('{'), k = txt.lastIndexOf('}');
      if (i < 0 || k <= i) return null;
      try { return JSON.parse(txt.slice(i, k + 1)); } catch (e) { return null; }
    }

    /* index.html 里静态加载的那份是**主站**的分段。多选时要把其余板块各自取回、
       并进同一个对象（cid 全局唯一，直接合并不冲突）。
       没选主站时先清空，免得主站的 cid 留在内存里白占地方。 */
    function loadStationSegments() {
      var mine = [], keepMain = false;
      ST_SET.forEach(function (id, i) {
        var isMain = MAIN_ID ? (id === MAIN_ID) : (i === 0);
        if (isMain) { keepMain = true; return; }
        mine.push(id);
      });
      if (!mine.length) return Promise.resolve();
      if (!keepMain) window.SEGMENTS = {};
      return Promise.all(mine.map(function (id) {
        return fetch('data/segments.js?station=' + encodeURIComponent(id))
          .then(function (r) { return r.ok ? r.text() : ''; })
          .then(segObject)
          .catch(function () { return null; });   // 离线时忽略，页面会提示没有分段
      })).then(function (objs) {
        var merged = window.SEGMENTS || {};
        objs.forEach(function (o) {
          if (!o) return;
          for (var k in o) {
            if (Object.prototype.hasOwnProperty.call(o, k)) merged[k] = o[k];
          }
        });
        window.SEGMENTS = merged;
      });
    }

    /* 取某个板块的清单。多板块时并行取回再合并 —— 媒体接口（playurl / dash / stream）
       本来就只认 bvid / cid、与板块无关，所以「混合播放多人」在前端合并清单就够了，
       服务端一行都不用动。 */
    function loadOne(id, timeoutMs) {
      var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctl) ctl.abort(); }, timeoutMs);
      var u = '/api/programs?refresh=1' + (id ? '&station=' + encodeURIComponent(id) : '');
      return fetch(u, ctl ? { signal: ctl.signal } : undefined)
        .then(function (r) {
          clearTimeout(timer);
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        });
    }

    function mergeLive(parts) {
      var all = [], seen = {}, fails = [], firstMeta = null, newest = 0;
      parts.forEach(function (d) {
        var head = (d.meta && d.meta.station) || null;
        if (d._fail) {
          fails.push(head ? (head.short || head.id) : d._fail);
          return;
        }
        if (!firstMeta && d.meta) firstMeta = d.meta;
        if (d.meta && (d.meta.generated_at || 0) > newest) newest = d.meta.generated_at || 0;
        (d.programs || []).forEach(function (p) {
          if (seen[p.bvid]) return;         // 同一条视频不会跨板块重复，保险起见去重
          seen[p.bvid] = 1;
          if (head) {
            // 打上来源标记：列表里的来源色点、以及「这条是谁的」提示都靠它
            p._sid = head.id;
            p._short = head.short || head.name || head.id;
            p._accent = head.accent || '';
          }
          all.push(p);
        });
      });
      if (!all.length) throw new Error('清单为空');
      return {
        programs: all,
        meta: {
          count: all.length,
          station: (firstMeta && firstMeta.station) || null,
          generated_at: newest || Math.floor(Date.now() / 1000),
          mixed: true,
          mixed_count: parts.length,
          mixed_failed: fails
        }
      };
    }

    function loadLive(timeoutMs) {
      var ids = ST_SET.length ? ST_SET.slice() : [''];
      if (ids.length === 1) {
        return loadOne(ids[0], timeoutMs).then(function (d) {
          if (!d || !d.programs || !d.programs.length) throw new Error('清单为空');
          return d;
        });
      }
      return Promise.all(ids.map(function (id) {
        // 某一位取不到不该把整条时间轴拖垮：记下名字，其余照常合并
        return loadOne(id, timeoutMs).catch(function (e) {
          return { programs: [], meta: { error: String((e && e.message) || e) }, _fail: id };
        });
      })).then(mergeLive);
    }

    // 起播不等服务端：先用手上这份清单（页面自带的 data/programs.js）立刻开播，
    // 抓最新清单放到后台做。实测 programs?refresh=1 要 1.2 秒，让它挡在起播前面
    // 就是白等 —— 拿到新清单后「软刷新」，不打断正在播的那一段。
    /* 内嵌的 window.PROGRAMS 是**主站（四时小路）**的离线快照，用来让首页秒开。
       切到别的主播时绝不能拿它启动 —— 否则屏幕上写着别人、内容却是小路的。
       副站一律走下面的实时接口，拿不到就停在「这位还没有回放数据」的说明上。 */
    /* 拿到新清单后「软刷新」：只换数据，不动正在跑的循环。
       循环里段的时间轴按 epoch 算的，重建会让当前位置映射到别的段、
       播放被打断重载（实测跳了一次）—— 所以标记 cycleStale，等下次自然换段再套用。 */
    function softApply(live) {
      if (!live || !live.programs || !live.programs.length) return false;
      var same = live.meta && live.meta.count === (state.meta || {}).count
                 && (state.all[0] || {}).bvid === live.programs[0].bvid;
      // 清单没变也要更新 meta：那里显示「实时数据 HH:MM」，
      // 是用户判断「看到的是不是最新」的唯一依据（verify_release 也看这个）
      state.meta = live.meta || state.meta;
      updateMetaText();
      if (same) return false;
      state.all = live.programs;
      state.cycleStale = true;
      renderChips();
      renderList();
      return true;
    }

    /* 过一会儿再问一次清单。服务端现在是「先把手上那份给出去、后台重抓」，
       所以首屏那次拿到的可能不是最新的 —— 后台抓完再来一次就补齐了。
       不这么做的话，新发布的回放要等下次刷新页面才出现。 */
    function refreshSoon(delayMs) {
      setTimeout(function () {
        loadLive(20000).then(function (live) {
          window.__LIVE_OK = true;
          softApply(live);
        }).catch(function (e) {
          window.__LIVE_ERR = e && e.message ? e.message : String(e);
        });
      }, delayMs || 0);
    }

    if (isMainOnly() && window.PROGRAMS && window.PROGRAMS.programs
        && window.PROGRAMS.programs.length) {
      boot(window.PROGRAMS);
      refreshSoon(0);
      prefetchLiveStatus();
    } else {
      loadStationSegments()
        .then(function () { return loadLive(20000); })
        .catch(function (e) {
          window.__LIVE_ERR = e && e.message ? e.message : String(e);
          return loadLocal();
        })
        .then(function (d) {
          // 副站拿不到清单（还没配 series_id）：给一句明确说明，
          // 不要留一片空白让人以为页面坏了
          if (ST && (!d || !((d.programs || []).length))) {
            var tip = document.getElementById('station-empty');
            if (tip) {
              tip.hidden = false;
              tip.textContent = '正在自动获取这位主播的回放清单…';
              // 把「为什么还没有」摊开说：自动获取失败时给出原因，
              // 而不是丢一句让人自己去改配置
              explainNoPrograms().then(function (msg) { tip.textContent = msg; });
            }
            return;
          }
          var r = boot(d);
          // 服务端明确说「这份旧了、正在后台重抓」→ 给它几秒，再取一次
          if (d && d.stale) refreshSoon(4000);
          return r;
        })
        .catch(fail);
    }
  }

  /* 副站清单为空时说清原因：服务端的回放来源状态（自动发现 / 手填 / 失败原因） */
  function explainNoPrograms() {
    return fetch('/api/series').then(function (r) { return r.json(); }).then(function (d) {
      var s = (d && d.series) || {};
      var who = ((d && d.station) || {}).short || '这位主播';
      if (s.error) {
        return '没能自动获取「' + who + '」的回放清单：' + s.error
          + '。稍后会自动重试（设置页有「重新获取」按钮）。'
          + '现在仍然可以看它的实时直播：进「直播间」或「监控室」。';
      }
      if (!s.id) {
        return '正在自动获取「' + who + '」的回放清单（主页 → 合集和系列 → 系列）…'
          + '稍等几秒刷新；同时也能看它的实时直播。';
      }
      return '已定位到「' + who + '」的回放系列 ' + s.id + '，正在拉取画面信息…'
        + '稍等一会儿刷新即可。';
    }).catch(function () {
      return '还没拿到这位主播的回放清单，后台会自动重试；也可以看它的实时直播。';
    });
  }

  // 提前问一次直播状态：等用户切到直播间时是热的（服务端缓存 60 秒）
  function prefetchLiveStatus() {
    fetch('/api/live/playinfo').catch(function () { /* 离线时忽略 */ });
  }


  /* 切换板块时把扫过动画交给新页面播：旧页面立刻重载，新页面一落地就把动画放完。
     用 sessionStorage 传一下 —— 重载会丢掉内存里的一切。 */
  function playArrivalSweep() {
    var accent = '';
    try {
      accent = sessionStorage.getItem('xl_sweep') || '';
      sessionStorage.removeItem('xl_sweep');
    } catch (e) { return; }
    var hex = themeHex(accent);
    if (!hex) return;
    var sel = document.getElementById('station-lamps');
    var b = sel ? sel.getBoundingClientRect() : null;
    themeSweep(hex, b ? b.left + b.width / 2 : window.innerWidth / 2,
               b ? b.bottom : 60);
  }

  /* 空闲时预热其它板块的清单。首次访问某位主播要现抓 1.5~2.6 秒，
     提前抓过之后再切过去就是瞬间。刻意错开间隔、每个会话每位只做一次、
     页面不可见就跳过 —— 别为了流畅度去惹 B 站风控（空间类接口本来就容易 412）。 */
  var PREFETCH_GAP = 7000;
  var prefetchStarted = false;

  function prefetchMark() {
    try { return JSON.parse(sessionStorage.getItem('xl_prefetch') || '{}') || {}; }
    catch (e) { return {}; }
  }

  function prefetchOthers() {
    if (prefetchStarted || state.offline) return;
    if (!STATIONS.length) {
      if (!prefetchStarted) {
        prefetchStarted = true;                 // 防重入：拉不到列表就不再试
        fetchStations().then(function () {
          prefetchStarted = false;
          prefetchOthers();
        });
      }
      return;
    }
    prefetchStarted = true;
    var done = prefetchMark();
    var todo = STATIONS.filter(function (s) {
      return ST_SET.indexOf(s.id) < 0 && s.has_programs && !done[s.id];
    }).map(function (s) { return s.id; });
    if (!todo.length) return;
    var i = 0;
    var next = function () {
      if (i >= todo.length) return;
      if (document.hidden) { setTimeout(next, PREFETCH_GAP); return; }
      var sid = todo[i++];
      done[sid] = 1;
      try { sessionStorage.setItem('xl_prefetch', JSON.stringify(done)); } catch (e) { /* 忽略 */ }
      fetch('/api/programs?station=' + encodeURIComponent(sid))
        .catch(function () { /* 离线忽略 */ })
        .then(function () { setTimeout(next, PREFETCH_GAP); });
    };
    setTimeout(next, 9000);        // 等首屏自己忙完再开始
  }

  /* ================= 主播切换器 =================
     切换 = 记下选择 + 整页刷新。切换不是高频操作，刷新能保证
     「所有视图的数据都属于同一位主播」—— 比逐个视图去重新拉取可靠得多
     （漏掉一处就会出现「界面是小路、数据是别人」的串台）。 */
  var STATIONS = [];

  /* 当前板块的「短名」，用在文案里（主站沿用原来的「小路」叫法，不改用户习惯）。
     applyStationBrand 会按板块更新它。 */
  var ST_SHORT = '小路';
  var FAB_LABEL = ST_SHORT + '状态';

  /* 整页底色/卡片底色是否跟随本板块的色调。
     主站（四时小路）**不上色** —— 它是「小路电台」本体，保持原来的黑红配色；
     其它板块各带一点自己的环境色，切换时能一眼看出换人了。
     由 fetchStations / 切换处理按 st.main 设置。 */
  var ST_TINT = false;

  /* 副站板块的品牌文案 —— 主站一个字都不改（还是「二十四时小路电台」原样）。
     这些位置原本都写死成小路的：大标题、顶栏站名、版权署名、数据来源链接。
     切到别的主播时不改，就成了反向错位：标题写着小路、内容却是枝堇的。 */
  function applyStationBrand(st) {
    if (!st) return;
    var short = st.short || st.name || '';

    // 右下角浮标跟着板块改名：主站沿用原来的「小路状态」，别的主播用各自短名。
    // 这一段对所有板块都执行（下面那些品牌文案只改副站）。
    ST_SHORT = st.main ? '小路' : short;
    FAB_LABEL = ST_SHORT + '状态';
    if (el.statusFab) el.statusFab.title = FAB_LABEL;
    if (el.statusFabText) el.statusFabText.textContent = FAB_LABEL;
    if (el.statusPanelTitle) el.statusPanelTitle.textContent = FAB_LABEL;

    if (st.main) return;
    var label = '二十四时' + short + '电台';
    function setTxt(id, txt) {
      var e = document.getElementById(id);
      if (e) e.textContent = txt;
    }
    setTxt('tb-brand', st.name || short);
    setTxt('tb-cur', label);
    setTxt('live-title', label);
    setTxt('live-sub', '这里收录了' + short + '全部的直播回放。'
      + '随时打开都在直播中，自动排期、自动去重，不重复。');
    var sp = document.getElementById('tb-space');
    if (sp && st.mid) sp.setAttribute('href', 'https://space.bilibili.com/' + st.mid);
    setTxt('gbtxt', short + 'B站空间');
    var cr = document.getElementById('cr-name');
    if (cr) {
      cr.textContent = st.name || short;
      if (st.mid) cr.setAttribute('href', 'https://space.bilibili.com/' + st.mid);
    }
    var cs = document.getElementById('cr-src');
    var series = st.series || {};
    if (cs) {
      if (st.mid && series.id) {
        cs.setAttribute('href', 'https://space.bilibili.com/' + st.mid
          + '/lists/' + series.id + '?type=series');
      }
      cs.textContent = '直播回放系列' + (series.total ? '（' + series.total + ' 场）' : '');
    }
    document.title = label + ' · ' + short + ' 24 小时直播回放';
  }

  function fetchStations() {
    return fetch('/api/stations').then(function (r) { return r.json(); })
      .then(function (d) {
        STATIONS = (d && d.stations) || [];
        var mains = STATIONS.filter(function (s) { return s.main; });
        if (mains[0]) {
          MAIN_ID = mains[0].id;
          try { localStorage.setItem(MAIN_KEY, MAIN_ID); } catch (e) { /* 隐私模式 */ }
        }
        // 顺便把当前板块的主题色落定（不再放动画：首屏那次由缓存值负责，避免开屏就闪一下）
        var curId = ST || ((mains[0] || {}).id || '');
        var cur = findStation(curId);
        if (cur) {
          ST_TINT = stationTint(ST_SET);
          applyTheme(mixAccents(ST_SET) || cur.accent);   // 多板块点亮时用混出来的色
          applyStationBrand(cur);
        }
        renderMixedNote();          // 提示条要等名单回来才知道短名
        // 名单刚回来，补一次渲染：如果这一页本来就是微博视图（刷新时 URL 停在
        // #/weibo），switchView 那次渲染发生在名单到达之前 —— 那时 findStation()
        // 查不到人，页面会写成「这位板块还没有配置微博」，而且不会自己纠正。
        if (state.view === 'weibo') renderWeibo();
        return STATIONS;
      })
      .catch(function () { STATIONS = []; return STATIONS; });
  }

  /* ---------------------------------------------------------- 板块色灯 */

  var lampTimer = null;

  /* 灭灯的暗版：按固定比例压暗（不是混进黑色），这样各板块的灯灭掉时一样暗 */
  function dimHex(hex, k) {
    var h = themeHex(hex);
    if (!h) return '#17171c';
    var out = '#';
    for (var i = 1; i <= 5; i += 2) {
      var v = Math.round(parseInt(h.substr(i, 2), 16) * k);
      out += (v < 16 ? '0' : '') + v.toString(16);
    }
    return out;
  }

  function renderLamps(live) {
    var box = document.getElementById('station-lamps');
    if (!box) return;
    box.innerHTML = STATIONS.map(function (s) {
      var on = ST_SET.indexOf(s.id) >= 0;
      var name = (s.short || s.name) + (s.main ? '（主站）' : '');
      var hint = on ? ' · 已点亮，点一下取消' : ' · 点一下把 TA 也加进来（混合播放）';
      var accent = s.accent || '#8a8a95';
      return '<button type="button" class="lamp' + (on ? ' on' : '') + '"'
        + ' data-id="' + esc(s.id) + '"'
        + ' style="--lc:' + esc(accent) + ';--lcd:' + esc(dimHex(accent, 0.26)) + '"'
        + ' aria-pressed="' + (on ? 'true' : 'false') + '"'
        + ' aria-label="' + esc(name + hint) + '"'
        + ' title="' + esc(name + hint) + '"></button>';
    }).join('');
    // 主位在播时给它的灯加一圈绿边（原来的小圆点就是这个作用）
    if (live) {
      var b = box.querySelector('[data-id="' + ST + '"]');
      if (b) b.classList.add('living');
    }
  }

  function toggleStation(id) {
    var i = ST_SET.indexOf(id);
    if (i >= 0) ST_SET.splice(i, 1);
    else ST_SET.push(id);
    if (!ST_SET.length) {
      // 不允许全灭：页面总得有个主体。回到主站。
      var mains = STATIONS.filter(function (s) { return s.main; });
      ST_SET = [(mains[0] || STATIONS[0] || { id: 'komichi' }).id];
    }
    ST = ST_SET[0];
    saveStations();
    renderLamps();
    // 视觉反馈不等重载：主题色/扫过动画立刻按新的主位走
    var nxt = findStation(ST);
    if (nxt && nxt.accent) {
      // 点亮的板块不止一个时，主题色取它们混出来的那一个（扫过动画同色）
      var mixed = mixAccents(ST_SET) || nxt.accent;
      ST_TINT = stationTint(ST_SET);
      applyTheme(mixed);
      try { sessionStorage.setItem('xl_sweep', mixed); } catch (e) { /* 忽略 */ }
    }
    /* 重载那一小段会露出「骨架态」（框架在、内容是空的），和切换前的画面一比
       就是一次突变 —— 用户说的「明显卡顿」主要就是它。
       所以这里先把底色记下来、过一小会儿再盖遮罩：
         · 不立刻盖，是为了让用户先看见「灯亮了 / 颜色变了」这个反馈；
         · 记的是**当前**（已经过渡到目标板块）的 --bg，新页面在最早时机用同一个色铺底，
           于是重载前后的背景是连续的，中间那段空窗就变成「颜色停留」。
       具体怎么用见 index.html 里那段内联脚本。 */
    if (veilTimer) clearTimeout(veilTimer);
    veilTimer = setTimeout(function () {
      try {
        sessionStorage.setItem('xl_veil', JSON.stringify({
          bg: getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#08080a',
          at: Date.now()
        }));
      } catch (e) { /* 忽略 */ }
      document.documentElement.classList.add('switching');
    }, 90);        // 跟着缩短的合并延迟一起提前，保证重载那一下遮罩已经盖得差不多
    /* 延迟重载：把连着点几下的操作合并成一次重载（点亮 4 位只刷一次页面）。
       数据源变了必须重建时间轴，整页重载是最省事也最不容易错的做法
       —— 清单与分段都带缓存，重载后列表基本是立刻出来的。

       实测这段延迟是整个切换耗时的绝对大头：点灯→重载开始 342ms，
       而重载本身（重载开始→首行渲染）只要 **22ms**。
       340ms 是照着「连点也要能合并」定的，但人的连点间隔多在 150~250ms，
       220ms 一样合得住，单点却少了 120ms。 */
    if (lampTimer) clearTimeout(lampTimer);
    lampTimer = setTimeout(function () { location.reload(); }, 220);
  }

  function mountStationPicker() {
    var box = document.getElementById('station-lamps');
    if (!box) return;
    fetchStations().then(function (list) {
      if (!list.length) return;
      // 选择里可能有已经不存在的板块（用户改了 stations.json）→ 剔除；空了就回主站
      var ids = list.map(function (s) { return s.id; });
      ST_SET = ST_SET.filter(function (x) { return ids.indexOf(x) >= 0; });
      if (!ST_SET.length) {
        var mains = list.filter(function (s) { return s.main; });
        ST_SET = [(mains[0] || list[0]).id];
        ST = ST_SET[0];
        saveStations();
      }
      renderLamps();
      renderMixedNote();
      box.addEventListener('click', function (e) {
        var b = e.target && e.target.closest ? e.target.closest('.lamp') : null;
        if (b) toggleStation(b.getAttribute('data-id'));
      });
      var note = document.getElementById('mixed-note');
      if (note) {
        note.addEventListener('click', function (e) {
          var b = e.target && e.target.closest ? e.target.closest('.mst') : null;
          if (!b) return;
          var id = b.getAttribute('data-src');
          state.onlySrc = (state.onlySrc === id) ? '' : id;   // 再点一下恢复全部
          renderList();
        });
      }
      fetch('/api/live/playinfo').then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.living) renderLamps(true); })
        .catch(function () { /* 离线忽略 */ });
    });
  }

  /* 混合播放的说明条。多位时列表里会交替出现几个人的内容，
     不说明一下会被当成「数据串了」。 */
  function renderMixedNote() {
    var box = document.getElementById('mixed-note');
    if (!box) return;
    if (ST_SET.length < 2) { box.hidden = true; return; }
    var names = ST_SET.map(function (id) {
      var s = findStation(id);
      return { id: id, name: s ? (s.short || s.name) : id, color: (s && s.accent) || '#8a8a95' };
    });
    /* 每位给一个可点的胶囊（带条数）：
       列表是按发布时间倒序的，内容少的那位可能排到好几页之后 ——
       点一下名字就能单独看 TA，顺便确认「确实合进来了」。 */
    var counts = {};
    (state.all || []).forEach(function (p) {
      if (p._sid) counts[p._sid] = (counts[p._sid] || 0) + 1;
    });
    box.innerHTML = '正在<b>混合播放</b>：'
      + names.map(function (n) {
          var on = state.onlySrc === n.id;
          return '<button type="button" class="mst' + (on ? ' on' : '') + '"'
            + ' data-src="' + esc(n.id) + '" style="--lc:' + esc(n.color) + '"'
            + ' title="' + esc(n.name + '（' + (counts[n.id] || 0) + ' 个节目）'
              + (on ? '，点一下看全部' : '，点一下只看 TA')) + '">'
            + '<i></i>' + esc(n.name) + '<b>' + (counts[n.id] || 0) + '</b></button>';
        }).join('<span class="msep">+</span>')
      + '<span class="mcnt">' + (state.onlySrc
          ? '只在看这一位，点名字恢复全部'
          : '共 ' + (state.all || []).length + ' 个节目') + '</span>';
    box.hidden = false;
  }

  /* ================= 监控室（多路同看） =================
     一个视图同时满足「同时看多人」与「监控室」：每格一路，可切内容源。
     音频策略：默认全部静音，点 🔊 只开这一路 —— 几路声音混在一起没法听。 */
  var multiPlayers = [];
  /* 重连定时器也要一起纳管：断流时 connect() 会挂一个 3 秒后重连的 setTimeout，
     只清播放器不清定时器的话，回调会在**已经离开监控室**之后照样跑，
     在已弃用的 <video> 上再接一路流 —— 反复进出就会累积孤儿连接。 */
  var multiTimers = [];

  function multiTeardown() {
    multiTimers.forEach(function (t) { clearTimeout(t); });
    multiTimers = [];
    multiPlayers.forEach(function (p) {
      try { p.destroy(); } catch (e) { /* 已经销毁 */ }
    });
    multiPlayers = [];
  }

  function multiQnChoice() {
    var s = document.getElementById('multi-qn');
    return s ? s.value : 'min';
  }

  function multiPickQn(choice, qualities) {
    var list = (qualities || []).slice();
    if (!list.length) return 250;
    var lowest = list.reduce(function (a, b) { return (a.qn <= b.qn ? a : b); });
    if (choice === 'min') return lowest.qn;
    var want = parseInt(choice, 10);
    var hit = list.filter(function (q) { return q.qn === want; })[0];
    return hit ? hit.qn : lowest.qn;      // 该主播没有这一档就退到最低档
  }

  function multiCellHtml(s) {
    var accent = s.accent || '#8a8a95';
    var picked = ST_SET.indexOf(s.id) >= 0;
    return '<div class="ms-cell loading' + (picked ? ' picked' : '') + '"'
      + ' data-id="' + esc(s.id) + '" style="--lc:' + esc(accent) + '">'
      + '<video muted playsinline></video>'
      + '<div class="ms-off"><b>' + esc(s.short || s.name) + '</b>'
      + '<span class="ms-off-sub">正在获取开播状态…</span></div>'
      + '<div class="ms-bar">'
      + '<span class="ms-tag" style="background:' + esc(accent) + '">' + esc(s.tag || '') + '</span>'
      + '<span class="ms-name">' + esc(s.short || s.name) + '</span>'
      + '<span class="ms-state" data-role="state">—</span>'
      + '<span class="ms-btns">'
      + '<button class="ms-btn" data-act="solo" type="button" title="只开这一路的声音">🔇</button>'
      + '<button class="ms-btn" data-act="zoom" type="button" title="放大 / 还原">⤢</button>'
      + '</span></div></div>';
  }

  function startMultiCell(s, cell) {
    var video = cell.querySelector('video');
    var off = cell.querySelector('.ms-off');
    var sub = cell.querySelector('.ms-off-sub');
    var stateEl = cell.querySelector('[data-role="state"]');
    var maxTry = 2;                      // 断流重连次数

    function live(title) {
      stateEl.textContent = title || '直播中';
      if (off) off.style.display = 'none';
    }

    function offline(text) {
      if (stateEl) stateEl.textContent = text;
      if (off) {
        off.style.display = '';
        sub.textContent = text;
      }
      cell.classList.remove('loading');
    }

    function connect(qualities, attempt) {
      if (typeof mpegts === 'undefined') { offline('播放库缺失（assets/mpegts.js）'); return; }
      var qn = multiPickQn(multiQnChoice(), qualities);
      var p = mpegts.createPlayer({
        type: 'flv', isLive: true,
        url: '/api/live/stream?station=' + encodeURIComponent(s.id) + '&qn=' + qn,
        enableStashBuffer: false,
        liveBufferLatencyChasing: true
      });
      p.attachMediaElement(video);
      p.load();
      try {
        var pr = p.play();
        if (pr && typeof pr.catch === 'function') pr.catch(function () { /* 等用户手势 */ });
      } catch (e) { /* 浏览器可能要求手势 */ }
      p.on(mpegts.Events.ERROR, function () {
        try { p.destroy(); } catch (e) { /* 已销毁 */ }
        if (attempt < maxTry) {
          stateEl.textContent = '连接中断，重连中…（' + (attempt + 1) + '/' + maxTry + '）';
          multiTimers.push(setTimeout(function () { connect(qualities, attempt + 1); }, 3000));
        } else {
          offline('连接中断，可点「刷新开播状态」重试');
        }
      });
      multiPlayers.push(p);
    }

    fetch('/api/live/playinfo?station=' + encodeURIComponent(s.id))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (state.view !== 'multi') return;   // 同上：等开播状态回来时可能已经切走了
        cell.classList.remove('loading');
        if (!d || !d.living) {
          offline(d && d.title ? ('未开播 · ' + d.title) : '未开播');
          return;
        }
        live(d.title);
        connect(d.qualities, 0);
      })
      .catch(function () { offline('开播状态获取失败'); });
  }

  function multiSolo(cell) {
    var btn = cell.querySelector('[data-act="solo"]');
    var on = !btn.classList.contains('on');
    Array.prototype.forEach.call(document.querySelectorAll('.ms-cell'), function (c) {
      var v = c.querySelector('video');
      var b = c.querySelector('[data-act="solo"]');
      var self = (c === cell);
      var audible = on && self;
      if (v) v.muted = !audible;
      if (b) {
        b.classList.toggle('on', audible);
        b.textContent = audible ? '🔊' : '🔇';
      }
      c.classList.toggle('muted-off', audible);
    });
  }

  function multiZoom(cell) {
    var grid = document.getElementById('multi-grid');
    if (!grid) return;
    var on = !cell.classList.contains('lead');
    Array.prototype.forEach.call(grid.querySelectorAll('.ms-cell'), function (c) {
      c.classList.remove('lead');
    });
    if (on) cell.classList.add('lead');
    grid.classList.toggle('solo', on);
  }

  function bindMulti() {
    var grid = document.getElementById('multi-grid');
    if (!grid || grid.getAttribute('data-bound') === '1') return;
    grid.setAttribute('data-bound', '1');
    grid.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.ms-btn') : null;
      if (!btn) return;
      var cell = btn.closest('.ms-cell');
      if (!cell) return;
      var act = btn.getAttribute('data-act');
      if (act === 'solo') multiSolo(cell);
      if (act === 'zoom') multiZoom(cell);
    });
    var rf = document.getElementById('multi-refresh');
    if (rf) rf.addEventListener('click', function () { renderMulti(); });
    var qs = document.getElementById('multi-qn');
    if (qs) qs.addEventListener('change', function () { renderMulti(); });
  }

  function renderMulti() {
    var grid = document.getElementById('multi-grid');
    if (!grid) return;
    multiTeardown();
    grid.classList.remove('solo');
    fetchStations().then(function (list) {
      // 名单是异步回来的：等它回来时用户可能已经切走了 —— 这时不该再接流
      if (state.view !== 'multi') return;
      if (!list.length) {
        grid.innerHTML = '<p class="sub">没读到主播列表（data/stations.json）。</p>';
        return;
      }
      grid.innerHTML = list.map(multiCellHtml).join('');
      var note = document.getElementById('multi-note');
      if (note) {
        note.textContent = list.length + ' 路 · 声音默认关闭，点 🔊 独奏；点 ⤢ 放大';
      }
      list.forEach(function (s) {
        var cell = grid.querySelector('.ms-cell[data-id="' + s.id + '"]');
        if (cell) startMultiCell(s, cell);
      });
    });
  }


  /* ---------------------------------------------------------- 微博 */

  /* 微博正文必须登录才给（游客只能看资料卡），所以这一页有两种「连接」方式：
     顶上的按钮开一个专用浏览器窗口、由本服务把 cookie 读回来（用户在里面顺手登录）；
     下面折叠区还能手动粘贴 cookie。两者都存本机文件，不出这台机器。 */
  var wbState = { data: null, loading: false, polling: false, bound: false };

  function wbSetTxt(id, txt) {
    var e = document.getElementById(id);
    if (e) e.textContent = txt;
  }

  function weiboTip(html) {
    var n = document.getElementById('wb-note');
    if (!n) return;
    if (!html) { n.hidden = true; return; }
    n.innerHTML = html;
    n.hidden = false;
  }

  function wbImgUrl(u) {
    return '/api/img?u=' + b64url(u);
  }

  function renderWeibo() {
    var list = document.getElementById('wb-list');
    if (!list) return;
    var st = findStation(ST) || {};
    var name = st.short || st.name || '';
    wbSetTxt('wb-title', (name || '') + (name ? '的' : '') + '微博');
    if (!st.weibo) {
      wbSetTxt('wb-sub', '这位板块还没有配置微博。');
      weiboTip('在 <code>data/stations.json</code> 里给它加一个 '
        + '<code>"weibo": "微博UID"</code> 就会出现在这里。');
      document.getElementById('wb-profile').hidden = true;
      list.innerHTML = '';
      return;
    }
    wbSetTxt('wb-sub', '来自微博主页的最新内容（weibo.com/u/' + st.weibo + '）。');
    loadWeibo();
  }

  function loadWeibo(force) {
    var list = document.getElementById('wb-list');
    if (!list || wbState.loading) return;
    wbState.loading = true;
    if (!wbState.data) {
      list.innerHTML = '<div class="wb-skel"></div><div class="wb-skel"></div>';
    }
    fetch('/api/weibo' + (force ? '?refresh=1' : ''))
      .then(function (r) { return r.json(); })
      .then(function (d) { wbState.data = d; wbState.loading = false; paintWeibo(d); })
      .catch(function (e) {
        wbState.loading = false;
        list.innerHTML = '';
        weiboTip('取微博失败：<b>' + esc(e && e.message ? e.message : String(e)) + '</b>');
      });
  }

  function paintWeibo(d) {
    var pf = d.profile || null;
    var box = document.getElementById('wb-profile');
    var list = document.getElementById('wb-list');
    var foot = document.getElementById('wb-foot');

    if (pf) {
      box.hidden = false;
      box.innerHTML =
        (pf.avatar ? '<img class="wb-avatar" src="' + esc(wbImgUrl(pf.avatar))
                      + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '')
        + '<div class="wb-pf-main">'
        + '<div class="wb-pf-row"><span class="wb-pf-name">' + esc(pf.name) + '</span>'
        + (pf.verified ? '<span class="wb-verified">' + esc(pf.verified) + '</span>' : '')
        + '</div>'
        + (pf.desc ? '<div class="wb-pf-desc">' + esc(pf.desc) + '</div>' : '')
        + '<div class="wb-pf-stats">粉丝 <b>' + fmtNum(pf.followers) + '</b> · 关注 <b>'
        + fmtNum(pf.follows) + '</b> · 微博 <b>' + fmtNum(pf.posts) + '</b></div>'
        + '<a class="wb-pf-link" href="' + esc(pf.home) + '" target="_blank" rel="noopener">'
        + '在微博打开主页 →</a>'
        + '</div>';
    } else {
      box.hidden = true;
    }

    var posts = d.posts || [];
    if (posts.length) {
      list.innerHTML = posts.map(wbItem).join('');
      weiboTip('');
    } else {
      list.innerHTML = '';
      if (d.need_login) {
        weiboTip(wbLoginNote(d.logged));
      } else if (d.error) {
        weiboTip('没取到内容：<b>' + esc(d.error) + '</b>');
      } else {
        weiboTip('这位最近没有公开微博。');
      }
    }
    if (foot) {
      foot.hidden = !posts.length;
      foot.textContent = '共 ' + posts.length + ' 条 · 点卡片可在微博查看原帖';
    }
  }

  function wbLoginNote(logged) {
    return '<span>微博现在<b>要求登录</b>才展示正文（游客只能看到上面的资料卡）。</span>'
      + '<button class="btn small primary" id="wb-connect" type="button">'
      + (logged ? '去登录微博' : '登录微博') + '</button>'
      + '<span id="wb-connect-state">'
      + (logged
          ? '当前是<b>访客态</b>：再登录一次，正文就会自动解锁。'
          : '会弹出一个<b>电脑版</b>微博窗口，扫码或账号登录都行；'
            + '登录完成后这边会自动解锁，不用回来点别的。')
      + '</span>';
  }

  function wbItem(p) {
    var pics = (p.pics || []).slice(0, 9);
    var html = '<div class="wb-item">'
      + '<div class="wb-item-h"><span class="wb-time">' + esc(p.at) + '</span>'
      + (p.from ? '<span>来自 ' + esc(p.from) + '</span>' : '')
      + (p.long ? '<span>长文</span>' : '')
      + '</div>'
      + '<div class="wb-text">' + esc(p.text) + '</div>';
    if (pics.length) {
      html += '<div class="wb-pics' + (pics.length === 1 ? ' one' : '') + '">'
        + pics.map(function (u) {
            return '<img loading="lazy" alt="" referrerpolicy="no-referrer" src="'
              + esc(wbImgUrl(u)) + '">';
          }).join('') + '</div>';
    }
    if (p.retweet) {
      html += '<div class="wb-retweet">@' + esc(p.retweet.name || '') + '：'
        + esc(p.retweet.text)
        + ((p.retweet.pics || []).length
           ? '<div class="wb-pics one">' + p.retweet.pics.map(function (u) {
               return '<img loading="lazy" alt="" referrerpolicy="no-referrer" src="'
                 + esc(wbImgUrl(u)) + '">';
             }).join('') + '</div>'
           : '')
        + '</div>';
    }
    html += '<div class="wb-acts">'
      + '<span>转发 ' + fmtNum(p.reposts) + '</span>'
      + '<span>评论 ' + fmtNum(p.comments) + '</span>'
      + '<span>赞 ' + fmtNum(p.likes) + '</span>'
      + (p.bid ? '<a class="wb-open" target="_blank" rel="noopener" href="https://weibo.com/'
                 + esc((wbState.data && wbState.data.uid) || '') + '/' + esc(p.bid)
                 + '">去微博看 →</a>' : '')
      + '</div></div>';
    return html;
  }

  /* 连接：开一个专用浏览器窗口，用户在里头（可顺手登录），
     本服务通过调试协议把 cookie 读回来。 */
  function wbConnect() {
    var btn = document.getElementById('wb-connect');
    var tip = document.getElementById('wb-connect-state');
    if (btn) { btn.disabled = true; btn.textContent = '正在打开…'; }
    postJSON('/api/weibo/login', {})
      .then(function (d) {
        if (d.error) {
          if (btn) { btn.disabled = false; btn.textContent = '登录微博'; }
          if (tip) tip.textContent = d.error;
          return;
        }
        if (tip) tip.innerHTML = '已打开<b>电脑版</b>微博窗口，请在里面登录'
          + '（扫码 / 账号都行；不登录也能看资料卡）…';
        wbPollStart();
      })
      .catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = '登录微博'; }
        if (tip) tip.textContent = '打开失败，请重试';
      });
  }

  function wbPollStart() {
    if (wbState.polling) return;
    wbState.polling = true;
    var n = 0;
    (function tick() {
      if (!wbState.polling) return;
      if (state.view !== 'weibo' || ++n > 180) { wbState.polling = false; return; }
      fetch('/api/weibo/login/poll')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var tip = document.getElementById('wb-connect-state');
          if (d.state === 'visitor') {
            if (tip) {
              tip.innerHTML = '已连上（<b>访客</b>）：资料卡可看。'
                + '在那个窗口里登录微博，就能看到正文 —— 登录后会<b>自动完成</b>。';
            }
            loadWeibo(true);          // 访客态也要把资料卡刷出来
          } else if (d.state === 'done') {
            wbState.polling = false;
            if (tip) tip.innerHTML = '已登录' + (d.nick ? '：<b>' + esc(d.nick) + '</b>' : '')
              + '，正文已解锁。';
            loadWeibo(true);
            return;
          } else if (d.state === 'timeout') {
            wbState.polling = false;
            if (tip) tip.textContent = d.error || '等待超时，可以再点一次。';
            return;
          }
          setTimeout(tick, 2000);
        })
        .catch(function () { setTimeout(tick, 3000); });
    })();
  }

  function bindWeibo() {
    if (wbState.bound) return;
    wbState.bound = true;
    var save = document.getElementById('wb-cookie-save');
    var clear = document.getElementById('wb-cookie-clear');
    var state1 = document.getElementById('wb-cookie-state');
    // 「连接微博」按钮是渲染出来的，用事件委托接
    var note = document.getElementById('wb-note');
    if (note) {
      note.addEventListener('click', function (e) {
        var b = e.target && e.target.closest ? e.target.closest('#wb-connect') : null;
        if (b) wbConnect();
      });
    }
    if (save) {
      save.addEventListener('click', function () {
        var v = document.getElementById('wb-cookie').value.trim();
        state1.textContent = '正在验证…';
        postJSON('/api/weibo/cookie', { cookie: v }).then(function (d) {
          state1.textContent = d.error || ('已保存：能取到 ' + (d.posts || 0) + ' 条微博 ✓');
          loadWeibo(true);
        }).catch(function () { state1.textContent = '保存失败'; });
      });
    }
    if (clear) {
      clear.addEventListener('click', function () {
        postJSON('/api/weibo/logout', {}).then(function () {
          state1.textContent = '已清除';
          document.getElementById('wb-cookie').value = '';
          wbState.data = null;
          loadWeibo(true);
        });
      });
    }
  }

  function bootAux() {
    mountStationPicker();
    // 窗口/顶栏折行后标签位置会变，胶囊要跟着重算；拖动时不必实时跟，
    // 停一下再就位（用 instant，免得胶囊在拖动过程中乱滑）。
    window.addEventListener('resize', function () {
      if (tabPillTimer) clearTimeout(tabPillTimer);
      tabPillTimer = setTimeout(function () { moveTabPill(true); }, 120);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(); bootAux(); });
  } else {
    init();
    bootAux();
  }
})();
