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
  var VIEWS = ['live', 'schedule', 'categories', 'about'];
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

  function rebuildCycle() {
    var pool = state.all.filter(function (p) {
      return !Object.keys(state.cats).length || state.cats[p.category];
    });
    state.cycle = buildCycle(pool);
    state.drift = 0;
    state.segIndex = -1;
    state.playingKey = null;
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
    dashPlayer.updateSettings({
      debug: { logLevel: dashjs.Debug.LOG_LEVEL_NONE },
      streaming: { buffer: { stableBufferTime: 12, fastSwitchEnabled: false } }
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
  function thumbSrc(p, size) {
    // size 是完整后缀（如 '240w_150h_1c.webp'），与主列表调用约定一致
    return '/api/img?u=' + b64url(p.thumb.replace('@320w_200h_1c.webp', '@' + size));
  }

  function programRow(p) {
    return '<tr data-bvid="' + p.bvid + '">'
      + '<td class="col-title"><div class="cell-title">'
      + '<img loading="lazy" decoding="async" alt="" '
             + 'src="' + esc(thumbSrc(p, '160w_100h_1c.webp')) + '" '
             + 'srcset="' + esc(thumbSrc(p, '160w_100h_1c.webp')) + ' 1x, '
             + esc(thumbSrc(p, '240w_150h_1c.webp')) + ' 1.5x, '
             + esc(thumbSrc(p, '320w_200h_1c.webp')) + ' 2x">'
      + '<div class="tt"><span class="n">' + esc(p.title) + '</span>'
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
        + '<div class="tt"><span class="n">' + esc(p.title) + '</span>'
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

  /* ---------------------------------------------------------- 视图路由 */

  function setView(name) {
    if (VIEWS.indexOf(name) < 0) name = 'live';
    state.view = name;
    document.body.classList.toggle('view-other', name !== 'live');
    document.querySelectorAll('.tab').forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-view') === name);
    });
    VIEWS.forEach(function (v) {
      var s = document.getElementById('view-' + v);
      if (s) s.classList.toggle('active', v === name);
    });

    if (name === 'live') renderList();
    else if (name === 'schedule') renderSchedule();
    else if (name === 'categories') renderCategories();
    else if (name === 'about') renderAbout();
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
    return fetch('/api/status', opt)
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

  var STATUS_SEEN_KEY = 'komichi.seenPub';

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
    var seen = parseInt(lsGet(STATUS_SEEN_KEY) || '0', 10) || 0;
    return seen > 0 && p.pubdate > seen;
  }

  function markSeen() {
    var p = newPrograms(1)[0];
    if (p) lsSet(STATUS_SEEN_KEY, String(p.pubdate));
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
      var seen = parseInt(lsGet(STATUS_SEEN_KEY) || '0', 10) || 0;
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
          + '拿不到小路状态：' + esc(e && e.message ? e.message : String(e))
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

  function refreshStatus() {
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (d) {
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
              el.loginStatus.textContent = '登录成功，正在按新清晰度重新加载…';
              setTimeout(function () {
                loginClose();
                refreshStatus();
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
        if (e.target && e.target.tagName === 'IMG') e.target.style.display = 'none';
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

    // 清晰度：重新取该清晰度的流，并从当前位置继续，不离开本页
    el.quality.addEventListener('change', function () {
      var seg = state.cycle.segments[state.segIndex];
      if (!seg) return;
      var anchor = cyclePos();               // 当前频道位置（视频未就绪时用期望位置）
      var keep = seg.t0 + Math.max(0, anchor - seg.start);
      state.qn = parseInt(el.quality.value, 10) || 80;
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
      fetch('/api/logout').then(function () { return refreshStatus(); }).then(function () {
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
    if (state.loading) return;              // 取流/定位期间不做换段判断
    var pos = cyclePos();
    var i = findSeg(pos);
    if (i !== state.segIndex) {
      state.segIndex = i;
      state.playingKey = null;
      applyPlayer(true);
      if (state.view === 'schedule') renderSchedule();
      if (state.marking) renderMarks();
      return;
    }
    var seg = state.cycle.segments[i];
    var off = Math.max(0, pos - seg.start);
    el.npBar.style.width = Math.min(100, off / seg.duration * 100) + '%';
    el.npPos.textContent = fmtClock(off);
    // 同一单元内跨过片段边界时，只更新高亮，不打断播放
    var key = seg.bvid + '#' + seg.page + '#' + chapterIndexNow();
    if (key !== state.chapKey) {
      state.chapKey = key;
      renderChapters();
    }
  }

  function boot(data) {
    state.all = data.programs || [];
    state.meta = data.meta || {};
    state.muted = state.mutedDefault;

    if (state.meta.count) {
      // 实时抓取时 generated_at 是「刚刚」，离线快照则是采集时刻。
      // 明确写出来，用户能一眼看出看到的是不是最新数据。
      var when = '';
      if (state.meta.generated_at) {
        var g = new Date(state.meta.generated_at * 1000);
        when = state.meta.live
          ? ' · 实时数据 ' + hhmm(g)
          : ' · 快照于 ' + (g.getMonth() + 1) + '-' + pad2(g.getDate())
            + ' ' + hhmm(g);
      }
      el.meta.textContent = '数据源：' + (state.meta.up_name || '')
        + ' · 系列「直播回放」 · ' + state.meta.count + ' 个节目' + when;
    }

    renderChips();
    renderList();
    syncSound();
    syncSkip();

    var h = parseInt(state.horizon, 10);
    document.querySelectorAll('[data-horizon]').forEach(function (x) {
      x.classList.toggle('active', (parseInt(x.getAttribute('data-horizon'), 10) || 0) === h);
    });

    route();
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
    el = {
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
      statusPanel: document.getElementById('status-panel'),
      statusBody: document.getElementById('status-body'),
      statusLoading: document.getElementById('status-loading'),
      statusFoot: document.getElementById('status-foot'),
      btnStatusRefresh: document.getElementById('status-refresh'),
      btnStatusClose: document.getElementById('status-close')
    };

    bind();

    // 数据来源优先级：
    //   ① /api/programs —— 由本机服务实时抓 B 站，打开页面即拿到最新投稿
    //   ② data/programs.js —— 内嵌离线快照（无服务或接口失败时用）
    //   ③ data/programs.json —— 最后兜底
    // 用「带超时的 Promise 竞速」而不是纯 fetch：接口卡住时不该让首屏一直转圈。
    function loadLocal() {
      if (window.PROGRAMS && window.PROGRAMS.programs && window.PROGRAMS.programs.length) {
        return Promise.resolve(window.PROGRAMS);
      }
      return fetch('data/programs.json').then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    function loadLive(timeoutMs) {
      var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctl) ctl.abort(); }, timeoutMs);
      return fetch('/api/programs?refresh=1', ctl ? { signal: ctl.signal } : undefined)
        .then(function (r) {
          clearTimeout(timer);
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (d) {
          if (!d || !d.programs || !d.programs.length) throw new Error('清单为空');
          return d;
        });
    }

    loadLive(20000)
      .catch(function (e) {
        window.__LIVE_ERR = e && e.message ? e.message : String(e);
        return loadLocal();
      })
      .then(boot)
      .catch(fail);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
