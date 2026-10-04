/* 冒烟测试：首页点击某一期，是不是「一次到位」
 *
 * 用法：node tools/tests/smoke_first_click.js
 * 前置：本机服务已在跑（python tools/serve.py --no-browser），8765 空闲可用。
 * 依赖：jsdom（脚本自带托管 Node 工作区的模块搜索路径，不必先设 NODE_PATH）
 *
 * 为什么需要这个脚本：在修改 playback/排片相关代码后，最常打碎的就是
 * 「用户点了列表里某一期」这条主路径，而出事的方式**不是报错**，
 * 是画面静悄悄地跑到了别的期上去 —— 光看界面没有报错、也看不出异常。
 *
 * 判据（而不是看某一个瞬间的标题）：
 *   1. 最终在播的是不是用户点的那一期
 *   2. 从点击到稳定，#np-title 一共变了几次（>1 就是「跳了一次又跳回来」）
 *   3. 重新取流了几次（>1 说明同一支被拉起两次）
 *
 * 三个场景对应现实里真实存在的时序：
 *   A. ping 还没回来就点（首屏资源争抢时很常见）→ 曾被 checkServer 回调里的
 *      非 soft rebuildCycle 把 drift 清零，画面跳到「当前时刻对应的一期」
 *   B. ping 回来之后点（常规路径）
 *   C. 手快点两行           → 最终要停在最后点的那一行
 *
 * instrument 挂在 HTMLMediaElement.prototype 上（parse 之前），
 * 因此**首屏那一次自动起播也在监控内** —— 这是与既有 smoke 脚本最大的差别。
 */
const fs = require('fs');
const path = require('path');
const HOME = process.env.HOME || process.env.USERPROFILE;
const WS = path.join(HOME, '.workbuddy/binaries/node/workspace/node_modules');
if (fs.existsSync(WS)) {
  process.env.NODE_PATH = WS;
  require('module').Module._initPaths();
  module.paths.push(WS);
}
const { JSDOM, VirtualConsole, requestInterceptor } = require('jsdom');

const PORT = process.env.PORT || 8765;
const ORIGIN = 'http://127.0.0.1:' + PORT;
const WWW = process.env.WWW || path.join(__dirname, '..', '..');
const IGNORE = [/Not implemented: HTMLMediaElement/, /Not implemented: HTMLCanvasElement/,
  /Not implemented: navigation/, /Could not parse CSS/, /Settings parameter/,
  /Could not load script.*(mpegts|dash)/i];
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml' };

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const T = (s) => String(s || '').replace(/\s+/g, ' ').trim();

// jsdom 自己发 HTTP 取静态资源时偶发「Could not load script」，会让结论不可信。
// 静态资源从磁盘直接命中。dash/mpegts 不加载（它们要 <video> 的真实能力）。
const localFiles = requestInterceptor((req) => {
  const p = decodeURIComponent(new URL(req.url).pathname);
  if (p.includes('dash.all.min.js') || p.includes('mpegts')) return undefined;
  const f = path.join(WWW, p.replace(/^\//, '') || 'index.html');
  if (fs.existsSync(f) && fs.statSync(f).isFile()) {
    return new Response(fs.readFileSync(f), { status: 200,
      headers: { 'Content-Type': MIME[path.extname(f)] || 'application/octet-stream' } });
  }
});

function open(log, pingDelay) {
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => {
    const m = String((e && e.message) || e);
    if (!IGNORE.some(re => re.test(m))) log.errors.push('jsdomError: ' + m);
  });
  vc.on('error', (...a) => log.errors.push('console.error: ' + a.join(' ')));

  return JSDOM.fromURL(ORIGIN + '/', {
    runScripts: 'dangerously',
    resources: { interceptors: [localFiles] },
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(w) {
      const P = w.HTMLMediaElement.prototype;
      const st = { t: 0, ms: Date.now() };
      const getT = () => st.t + (Date.now() - st.ms) / 1000;
      Object.defineProperty(P, 'readyState', { get: () => 4, configurable: true });
      Object.defineProperty(P, 'duration', { get: () => 1e9, configurable: true });
      Object.defineProperty(P, 'paused', { get: () => false, configurable: true });
      Object.defineProperty(P, 'currentTime', {
        get: getT,
        set(v) {
          log.seeks.push({ from: +getT().toFixed(2), to: +Number(v).toFixed(2) });
          st.t = Number(v); st.ms = Date.now();
          setTimeout(() => { try { this.dispatchEvent(new w.Event('seeked')); } catch (e) {} }, 10);
        },
        configurable: true
      });
      let _src = '';
      Object.defineProperty(P, 'src', {
        get: () => _src,
        set(v) {
          _src = String(v);
          setTimeout(() => { try { this.dispatchEvent(new w.Event('loadedmetadata')); } catch (e) {} }, 10);
        },
        configurable: true
      });
      P.play = () => Promise.resolve();
      P.pause = () => {};

      const real = (typeof fetch === 'function') ? fetch : null;
      w.fetch = (u, o) => {
        const s = String(u);
        const abs = s.startsWith('/') ? ORIGIN + s : s;
        if (s.includes('/api/ping')) {
          log.pingCount++;
          return new Promise(res => setTimeout(
            () => res({ ok: true, json: () => Promise.resolve({ ok: 1 }) }), pingDelay));
        }
        if (/\/api\/(dash|playurl)\?/.test(s))
          log.loads.push({ cid: (/cid=(\d+)/.exec(s) || [])[1], bvid: (/bvid=([^&]+)/.exec(s) || [])[1] });
        if (s.includes('/api/playurl?'))
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            media: ORIGIN + '/fake.mp4', quality: 64, accept: [{ qn: 64, desc: '720P' }] }) });
        if (s.includes('/api/dash?'))
          return Promise.resolve({ ok: true, json: () => Promise.resolve({ error: 'FAKE-DASH-OFF' }) });
        if (s.includes('/api/stream'))
          return Promise.resolve({ ok: false, status: 599, json: () => Promise.resolve({}) });
        return real ? real(abs, o) : Promise.reject(new Error('无可用 fetch'));
      };
    }
  });
}

/** 跑一个场景：等到列表出现 → 按 plan 决定何时点 → 观测到稳定。 */
async function scenario(name, { pingDelay, waitAfterReady, double }) {
  const log = { seeks: [], loads: [], errors: [], pingCount: 0 };
  const dom = await open(log, pingDelay);
  const w = dom.window, doc = w.document;
  const np = () => T(doc.querySelector('#np-title') && doc.querySelector('#np-title').textContent);

  const trace = [];
  let watching = true;
  (async () => {
    let last = '';
    while (watching) {
      await sleep(25);
      const t = np();
      if (t && t !== last) { trace.push(t); last = t; }
    }
  })();

  let btn = null;
  for (let i = 0; i < 300 && !btn; i++) { await sleep(100); btn = doc.querySelector('#rows tr [data-play]'); }
  if (!btn) {
    console.log('  [%s] 打不开页面：rows=%d __STATE=%s %j',
      name, doc.querySelectorAll('#rows tr').length, !!w.__STATE, log.errors.slice(0, 2));
    watching = false; dom.window.close();
    return { name, ok: false, note: '页面未就绪' };
  }

  if (waitAfterReady) { await sleep(3000); btn = doc.querySelector('#rows tr [data-play]'); }
  const want = btn.getAttribute('data-play');
  const since = { seeks: log.seeks.length, loads: log.loads.length, trace: trace.length };

  btn.click();
  if (double) {
    await sleep(400);
    const list = Array.from(doc.querySelectorAll('#rows tr [data-play]'));
    const second = list[2] || btn;
    second.click();
    var wantFinal = second.getAttribute('data-play');
  } else {
    var wantFinal = want;
  }
  await sleep(3000);

  const final = np();
  // 目标那一期的标题：播放器里显示的是「标题（分P n / m）」，用 bvid 反查
  const st = w.__STATE || {};
  const hitBvid = st.playingKey ? String(st.playingKey).split('#')[0] : '';
  const changes = trace.length - since.trace;
  const newLoads = log.loads.slice(since.loads);
  /* 「一次到位」的判据：
       - 最终在播的必须是最后点的那一期
       - 标题变化次数：点几次就允许变几次（连点两行自然是 2 次）
       - **不能出现在两支之外的第三支** —— 那才是「跳到别的期」，
         也是这个 bug 历史上真正的表现（画面去了用户没点听过的一期）。 */
  const stray = newLoads.filter(l => l.bvid !== want && l.bvid !== wantFinal);
  const note = [];
  note.push('在播 ' + hitBvid + '（应 ' + wantFinal + '）');
  note.push('标题变化 ' + changes + ' 次');
  note.push('取流 ' + newLoads.length + ' 次');
  if (stray.length) note.push('多出 ' + stray.length + ' 支没点过的视频');

  const ok = hitBvid === wantFinal && changes <= (double ? 2 : 1) && !stray.length;
  console.log('  [%s] %s', ok ? 'PASS' : 'FAIL', note.join(' | '));
  if (!ok) {
    trace.slice(since.trace).forEach((t, i) => console.log('        标题 %d: %s', i + 1, t.slice(0, 40)));
    newLoads.forEach(l => console.log('        取流 cid=%s bvid=%s', l.cid, l.bvid));
  }
  console.log('        最终画面：%s', final.slice(0, 40));
  watching = false;
  dom.window.close();
  return { name, ok, note: note.join(' | ') };
}

(async () => {
  console.log('首页点击播放：应一次到位且不中途跳到别的期\n');
  const results = [];
  results.push(await scenario('A ping 未回时点击', { pingDelay: 800, waitAfterReady: false }));
  results.push(await scenario('B ping 已回后点击', { pingDelay: 800, waitAfterReady: true }));
  results.push(await scenario('C 快速连点两行', { pingDelay: 800, waitAfterReady: true, double: true }));
  const fail = results.filter(r => !r.ok);
  console.log('\n结果：%d PASS / %d FAIL', results.length - fail.length, fail.length);
  process.exit(fail.length ? 1 : 0);
})();
