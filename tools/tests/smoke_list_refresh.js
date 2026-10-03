/* 冒烟测试：后台抓到新清单（cycleStale）时画面会不会被拽走
 *
 * 依赖 jsdom（用技能 frontend-jsdom-smoke 自带的脚手架）：
 *   NODE_PATH="$HOME/.workbuddy/binaries/node/workspace/node_modules" node tools/tests/smoke_list_refresh.js
 * 前置：本机服务已在跑（python tools/serve.py --no-browser），8765 空闲可用。
 *
 * 原理：在 jsdom 里把 <video> 伪装成「加载成功」——mock 掉取流接口、手动派发
 * loadedmetadata/seeked，于是 mediaBase 真的会被设上，cyclePos 才会走 currentTime
 * 这条真实分支（不然所有观测都是在看挂钟 advancing，结论不可信）。
 * 然后统计 seek 次数、重新取流次数、画面标题变化 —— 它们才是「画面动没动」的判据。
 */
const fs = require('fs');
const path = require('path');
const HOME = process.env.HOME || process.env.USERPROFILE;
const CANDIDATES = [
  process.env.HARNESS,
  path.join(__dirname, 'harness.js'),
  path.join(HOME, '.workbuddy/skills/frontend-jsdom-smoke/scripts/harness.js')
].filter(Boolean);
// 把 jsdom 所在的 node_modules 直接加进 require 搜索路径，省得每次都先设 NODE_PATH
const WS = path.join(HOME, '.workbuddy/binaries/node/workspace/node_modules');
// 把 jsdom 所在的 node_modules 挂到全局 require 搜索路径上（harness.js 自己也要 require
// jsdom，所以必须走 NODE_PATH + 重建模块路径，光改本模块的 paths 不够）
if (fs.existsSync(WS)) {
  process.env.NODE_PATH = WS;
  require('module').Module._initPaths();
  module.paths.push(WS);
}
const HARNESS = CANDIDATES.find(p => fs.existsSync(p));
if (!HARNESS) {
  console.error('找不到 harness.js，请设置 HARNESS 环境变量指向它的路径');
  process.exit(2);
}
// 场景：后台抓到新清单（cycleStale）时，画面会不会被拽走
// 手段：mock /api/programs?refresh=1，在原清单前面插一期假的（softApply 判定为「变了」
//      → 置 cycleStale），再把播放位置快进到当前支即将播完处，逼 tick 消费 cycleStale。

const { loadPage, sleep, click, text } = require(HARNESS);

const IGNORE = [/Could not load script.*(mpegts|dash)/, /Not implemented/i, /ENOTFOUND/i];
const SP = Number(process.env.SP || 1);

(async () => {
  let r = null;
  for (let i = 0; i < 4; i++) {
    const c = await loadPage('http://127.0.0.1:8765/', { ignore: IGNORE });
    await sleep(2200);
    if (c.dom.window.document.querySelectorAll('#rows tr').length > 1) { r = c; break; }
    c.dom.window.close();
  }
  if (!r) { console.log('页面没就绪'); process.exit(1); }
  const w = r.dom.window, doc = w.document, errors = r.errors, seeks = [], loads = [];

  /* --- 假播放器 --- */
  const pl = doc.getElementById('player');
  let anchorVal = 0, anchorMs = Date.now();
  const nowT = () => anchorVal + (Date.now() - anchorMs) * SP / 1000;
  Object.defineProperty(pl, 'currentTime', {
    get: nowT,
    set: (v) => {
      seeks.push({ from: +nowT().toFixed(1), to: +Number(v).toFixed(1), at: Date.now(), ext: !!pl.__ext });
      anchorVal = Number(v); anchorMs = Date.now();
      setTimeout(() => { try { pl.dispatchEvent(new w.Event('seeked')); } catch (e) {} }, 15);
    },
    configurable: true
  });
  Object.defineProperty(pl, 'readyState', { get: () => 4, configurable: true });
  Object.defineProperty(pl, 'duration', { get: () => 1e9, configurable: true });
  pl.play = () => Promise.resolve();
  let _src = '';
  Object.defineProperty(pl, 'src', {
    get: () => _src,
    set: (v) => {
      _src = String(v);
      setTimeout(() => { try { pl.dispatchEvent(new w.Event('loadedmetadata')); } catch (e) {} }, 20);
    },
    configurable: true
  });
  pl.__jumpTo = (v) => { pl.__ext = true; pl.currentTime = v; pl.__ext = false; };

  /* --- 假取流 + 假「新清单」 --- */
  const orig = w.fetch;
  let real = null;
  w.fetch = (u, o) => {
    const s = String(u);
    const m = /\/api\/(dash|playurl)\?bvid=([^&]+)&cid=(\d+)/.exec(s);
    if (m) loads.push({ kind: m[1], cid: m[3], at: Date.now() });
    if (s.indexOf('/api/dash?') >= 0)
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ error: '测试：DASH 屏蔽' }) });
    if (s.indexOf('/api/playurl?') >= 0)
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        media: 'http://127.0.0.1:8765/fake.mp4', quality: 64, accept: [{ qn: 64, desc: '720P' }] }) });
    if (s.indexOf('/api/stream') >= 0)
      return Promise.resolve({ ok: false, status: 599, json: () => Promise.resolve({}) });
    if (s.indexOf('/api/programs?refresh=1') >= 0) {
      // 第一次拿到真清单后存下来；之后每次都「多出一期新的」
      const p = orig(s, o).then(rr => rr.json()).then(d => {
        real = d;
        return d;
      });
      return p.then(d => {
        const out = JSON.parse(JSON.stringify(d || {}));
        const first = (out.programs && out.programs[0]) || null;
        if (!first) return out;
        const clone = JSON.parse(JSON.stringify(first));
        clone.bvid = 'BV1FAKEXQNEW0000';
        clone.title = '【测试】刚上架的一期新回放';
        clone.cid = (clone.cid || 0) + 9000001;
        clone.parts = [{ page: 1, cid: 99000001, duration: 600 }];
        out.programs = [clone].concat(out.programs);
        out.meta = Object.assign({}, out.meta, { count: (out.meta || {}).count + 1 });
        return out;
      });
    }
    return orig(u, o);
  };

  const np = () => ({
    title: (text(r.dom, '#np-title') || '').trim(),
    pos: (text(r.dom, '#np-pos') || '').trim(),
    dur: (text(r.dom, '#np-dur') || '').trim()
  });
  const clock = (s) => { const m = /^(?:(\d+):)?(\d+):(\d+)$/.exec(s || ''); return m ? (+m[1] || 0) * 3600 + (+m[2]) * 60 + (+m[3]) : NaN; };
  const settled = async (ms) => {
    let g = 0;
    while (g++ < 80) {
      await sleep(250);
      if (!seeks.length) continue;
      const lastEv = Math.max(seeks[seeks.length - 1].at, loads.length ? loads[loads.length - 1].at : 0);
      if (Date.now() - lastEv > ms) return true;
    }
    return false;
  };

  await settled(2500);
  const st = w.__STATE;
  const dumpState = (tag) => {
    if (!st) { console.log("    [%s] 旧版无 __STATE 出口", tag); return; }
    const s = st.cycle.segments[st.segIndex] || {};
    console.log('    [%s] stale=%s exempt=%s idx=%d/%d cid=%s dur=%s total=%s aimVer=%s aimCid=%s '
      + 'aimPos=%s base=%s media=%s pos=%s',
      tag, st.cycleStale, st.exemptCid, st.segIndex, st.cycle.segments.length,
      s.cid, s.duration, Math.round(st.cycle.total), st.aimVer, st.aimCid,
      st.aimPos === null ? '-' : Math.round(st.aimPos), Math.round(st.cycleBase),
      st.mediaBase, '-');
  };
  console.log('  稳定后：', JSON.stringify(np()));
  dumpState('稳定时');

  // 快进到当前支快播完：把 currentTime 推到「距离段尾 3 秒」的位置
  const total = clock(np().dur), at = clock(np().pos);
  console.log('  当前支 时长 %s 秒，已播 %s 秒 → 快进到段尾 -3s', total, at);
  pl.__jumpTo(nowT() + (total - at - 3));

  const before = np();
  let last = before.title;
  let prevStale = st && st.cycleStale, prevIdx = st && st.segIndex;
  const t0 = Date.now();
  const events = [];
  while (Date.now() - t0 < 6000) {
    await sleep(200);
    if (st && (st.cycleStale !== prevStale || st.segIndex !== prevIdx)) {
      prevStale = st.cycleStale; prevIdx = st.segIndex;
      dumpState('tick ' + String(Date.now() - t0) + 'ms');
    }
    const c = np();
    if (c.title !== last) {
      events.push(Date.now() - t0 + 'ms → ' + c.title.slice(0, 30) + ' [' + c.dur + ']');
      last = c.title;
    }
  }
  dumpState('结束');
  console.log('\n  越过段尾之后：');
  events.forEach(e => console.log('   ', e));
  const internal = seeks.filter(s => !s.ext);
  seeks.forEach(s => console.log('    seek %s → %s  (+%dms%s)',
    s.from, s.to, s.at - t0, s.ext ? ' 外部快进' : ''));
  loads.forEach(s => console.log('    取流 %s cid=%s (+%dms)', s.kind, s.cid, s.at - t0));
  const moved = events.length;
  console.log('\n  换支 %d 次，应用内部 seek %d 次，重新取流 %d 次', moved, internal.length, loads.length);
  console.log('  报错:', errors.length ? errors.slice(0, 3) : '无');
  w.close();
  process.exit(0);
})();
