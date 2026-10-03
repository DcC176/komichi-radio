/* 冒烟测试：「跳过空白」开关是不是真的不动画面
 *
 * 依赖 jsdom（用技能 frontend-jsdom-smoke 自带的脚手架）：
 *   NODE_PATH="$HOME/.workbuddy/binaries/node/workspace/node_modules" node tools/tests/smoke_skip_switch.js
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
// 「跳过空白」是不是真的不动画面：多场景 × 计数断言
// 手段：让 <video> 表现为加载成功（mock playurl + 手动派发 loadedmetadata/seeked），
//      mediaBase 才会真的被设置 → cyclePos 走 currentTime 这条真实分支；
//      然后统计 seek 次数、重新取流次数、标题变化。

const { loadPage, sleep, click, text } = require(HARNESS);

const IGNORE = [/Could not load script.*(mpegts|dash)/, /Not implemented/i, /ENOTFOUND/i];
const SP = Number(process.env.SP || 1);

async function open() {
  for (let i = 0; i < 4; i++) {
    const r = await loadPage('http://127.0.0.1:8765/', { ignore: IGNORE });
    await sleep(2200);
    if (r.dom.window.document.querySelectorAll('#rows tr').length > 1) return r;
    r.dom.window.close();
  }
  throw new Error('页面没就绪');
}

function instrument(w, seeks, loads) {
  const doc = w.document, pl = doc.getElementById('player');
  let anchorVal = 0, anchorMs = Date.now();
  const nowT = () => anchorVal + (Date.now() - anchorMs) * SP / 1000;
  Object.defineProperty(pl, 'readyState', { get: () => 4, configurable: true });
  Object.defineProperty(pl, 'currentTime', {
    get: nowT,
    set: (v) => {
      seeks.push({ from: +nowT().toFixed(2), to: +Number(v).toFixed(2), at: Date.now() });
      anchorVal = Number(v); anchorMs = Date.now();
      setTimeout(() => { try { pl.dispatchEvent(new w.Event('seeked')); } catch (e) {} }, 15);
    },
    configurable: true
  });
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
  const orig = w.fetch;
  w.fetch = (u, o) => {
    const s = String(u);
    const m = /\/api\/(dash|playurl)\?bvid=([^&]+)&cid=(\d+)/.exec(s);
    if (m) loads.push({ kind: m[1], cid: m[3], at: Date.now() });
    if (s.indexOf('/api/dash?') >= 0)
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ error: '测试：DASH 屏蔽' }) });
    if (s.indexOf('/api/playurl?') >= 0)
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        media: 'http://127.0.0.1:8765/fake.mp4', quality: 64,
        accept: [{ qn: 64, desc: '720P' }]
      }) });
    if (s.indexOf('/api/stream') >= 0) return Promise.resolve({ ok: false, status: 599, json: () => Promise.resolve({}) });
    return orig(u, o);
  };
}

(async () => {
  const r = await open();
  const dom = r.dom, w = dom.window, doc = w.document, errors = r.errors;
  const seeks = [], loads = [];
  instrument(w, seeks, loads);

  const np = () => ({
    title: text(dom, '#np-title').trim(),
    pos: text(dom, '#np-pos').trim(),
    dur: text(dom, '#np-dur').trim()
  });
  const settled = async (ms) => {
    let guard = 0;
    while (guard++ < 80) {
      await sleep(250);
      if (!seeks.length) continue;
      const lastEv = Math.max(seeks[seeks.length - 1].at, loads.length ? loads[loads.length - 1].at : 0);
      if (Date.now() - lastEv > ms) return true;
    }
    return false;
  };
  const probe = (ms, cb) => {
    const before = { s: seeks.length, l: loads.length, t: np() };
    cb();
    return new Promise(res => setTimeout(() => res({
      seeks: seeks.length - before.s, loads: loads.length - before.l,
      before: before.t, after: np(),
      seeksDetail: seeks.slice(before.s), loadsDetail: loads.slice(before.l)
    }), ms));
  };

  let pass = 0, fail = 0;
  // 只有真实速度（SP=1）下「不动画面」才是可断言的：加速播放时点击后本来就可能
  // 恰好赶上自然换段，那不是 bug。加速模式只用来跑场景 D（看后续支是否按新规则）。
  const strict = SP === 1;
  const check = (name, got, want) => {
    if (!strict) { console.log('   [跳过] %s：加速模式不断言（实际 %s）', name, got); return; }
    const ok = got === want;
    ok ? pass++ : fail++;
    console.log('   [%s] %s：实际 %s，期望 %s', ok ? 'PASS' : 'FAIL', name, got, want);
  };
  const dump = (res) => {
    res.seeksDetail.forEach(s => console.log('        seek %s → %s', s.from, s.to));
    res.loadsDetail.forEach(s => console.log('        取流 %s cid=%s', s.kind, s.cid));
  };

  const btn = doc.getElementById('btn-skip');

  /* --- A：稳定播放中点击一次 --- */
  await settled(2000);
  console.log('\nA. 稳定播放中点击一次');
  console.log('   点击前', JSON.stringify(np()));
  let res = await probe(2500, () => btn.click());
  console.log('   点击后', JSON.stringify(res.after));
  check('seek 次数', res.seeks, 0);
  check('重新取流次数', res.loads, 0);
  check('标题未变', res.after.title === res.before.title, true);
  dump(res);

  /* --- B：连点三次（开→关→开）--- */
  await settled(1500);
  console.log('\nB. 连点三次');
  res = await probe(2500, () => { btn.click(); btn.click(); btn.click(); });
  console.log('   前', JSON.stringify(res.before), '\n   后', JSON.stringify(res.after));
  check('seek 次数', res.seeks, 0);
  check('重新取流次数', res.loads, 0);
  check('标题未变', res.after.title === res.before.title, true);
  dump(res);

  /* --- C：点了列表里的另一支之后，豁免转移到新支 --- */
  console.log('\nC. 点列表换到别的支之后（豁免应转移到新支）');
  const rowBtn = doc.querySelector('#rows tr td.col-act .play-btn[data-play]');
  res = await probe(2500, () => { if (rowBtn) rowBtn.click(); });
  console.log('   换支后', JSON.stringify(res.after));
  check('换支确实重新取了流', res.loads >= 1, true);
  await settled(2000);
  res = await probe(2500, () => btn.click());
  console.log('   再点开关', JSON.stringify(res.before), '→', JSON.stringify(res.after));
  check('新支上切换也不动画面（seek 0）', res.seeks, 0);
  check('新支上切换也不重载（取流 0）', res.loads, 0);
  check('标题未变', res.after.title === res.before.title, true);
  dump(res);

  /* --- D（仅加速模式）：豁免只保护当前支，下一支起按新规则 --- */
  if (SP > 1) {
    console.log('\nD. 加速播放 %dx：豁免应只保当前支，后续支按新规则', SP);
    if (text(dom, '#btn-skip').indexOf('开') < 0) btn.click();   // 确保处于「开」
    await settled(1500);
    const before = np();
    const t0 = Date.now();
    let last = before.title;
    const seen = [];
    const guardSize = { s: seeks.length, l: loads.length };
    while (Date.now() - t0 < 45000 && seen.length < 4) {
      await sleep(200);
      const c = np();
      if (c.title !== last) { seen.push(c); last = c.title; }
    }
    console.log('   当前支: %s [%s]', before.title.slice(0, 24), before.dur);
    seen.forEach(s => console.log('   换到:   %s [%s]', s.title.slice(0, 24), s.dur));
    const short = seen.filter(s => {
      const m = /^(?:(\d+):)?(\d+):(\d+)$/.exec(s.dur);
      return m && ((+m[1] || 0) * 60 + (+m[2])) < 20;      // 20 分钟以内算「片段」
    });
    // 这一条在加速模式下才有意义（要等到自然换支），必须硬断言
    const ok = short.length >= 1;
    ok ? pass++ : fail++;
    console.log('   [%s] 后续支是按「跳过空白」切出来的片段：%s',
      ok ? 'PASS' : 'FAIL', short.length + '/' + seen.length + ' 支短于 20 分钟');
  }

  console.log('\n   报错:', errors.length ? errors.slice(0, 3) : '无');
  console.log('   结果: %d PASS / %d FAIL', pass, fail);
  dom.window.close();
  process.exit(fail ? 1 : 0);
})();
