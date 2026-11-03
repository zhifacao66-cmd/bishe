/* 前端运行时验收：真浏览器打开 10 个页面，收控制台报错；
   再验两件新东西 —— 分页真的能翻、请求失败真的会弹提示条。
   （一次性验收脚本，不进回归；静态契约见 scripts/smoke_frontend.py） */
const { chromium } = require('playwright-core');

const EXE = 'C:/Users/10517/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:5000';
const PAGES = ['/', '/tasks', '/orders', '/ledgers', '/notices', '/kb',
               '/reports', '/audit', '/system', '/admin/users'];

(async () => {
  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();

  const problems = [];
  page.on('console', m => {
    if (m.type() === 'error') problems.push(`console: ${m.text().slice(0, 160)}`);
  });
  page.on('pageerror', e => problems.push(`pageerror: ${String(e).slice(0, 160)}`));

  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', 'admin123');
  await page.click('button[type="submit"], .btn-primary');
  await page.waitForLoadState('domcontentloaded');
  console.log('登录后地址:', page.url());

  for (const path of PAGES) {
    const before = problems.length;
    await page.goto(BASE + path, { waitUntil: 'networkidle' });
    await page.waitForTimeout(350);
    const rows = await page.evaluate(() => {
      const t = document.querySelector('#rows, #tbody, tbody');
      return t ? t.querySelectorAll('tr').length : -1;
    });
    console.log(`${(problems.length === before ? 'OK  ' : 'ERR ')} ${path.padEnd(14)} 行数=${rows}`);
  }

  // ---- 分页：用桩数据把 4 页灌进接口，验"翻页真的换内容、页码真的进网址" ----
  // 演示库只有 4 条公告，一页装得下，翻不出第 2 页；接口本身的语义
  // （越界夹取、逐页不重不漏）已由 smoke_frontend.py 覆盖，这里只验前端接线。
  const p3 = await ctx.newPage();
  await p3.route('**/api/notices**', async route => {
    const u = new URL(route.request().url());
    const page = Math.min(4, Math.max(1, parseInt(u.searchParams.get('page') || '1', 10) || 1));
    const items = [1, 2].map(i => ({
      id: page * 10 + i, title: `第${page}页第${i}条`, issuer: '测试单位', doc_no: '—',
      status: 'published', kind: 'original', recv_status: 'received', pinned: false,
      pinned_at: '', parse_note: '', local_count: 0, local_published: 0,
    }));
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ code: 0, msg: 'ok', data: {
        items, counts: { published: 8, pending: 0, offline: 0, todo: 0, pinned: 0,
                         matched: 8, page, size: 2, pages: 4, shown: 2, truncated: true },
        max_pinned: 3 } }),
    });
  });
  await p3.goto(BASE + '/notices', { waitUntil: 'domcontentloaded' });
  await p3.waitForTimeout(400);
  const pg1 = await p3.evaluate(() => ({
    text: document.getElementById('rows').innerText.replace(/\s+/g, ' ').slice(0, 40),
    info: document.getElementById('pgInfo').textContent,
    hint: document.getElementById('listHint').textContent,
    prevDis: document.getElementById('pgPrev').disabled,
    nextDis: document.getElementById('pgNext').disabled,
  }));
  console.log('第 1 页:', JSON.stringify(pg1));
  await p3.click('#pgNext');
  await p3.waitForTimeout(500);
  const pg2 = await p3.evaluate(() => ({
    url: location.search,
    text: document.getElementById('rows').innerText.replace(/\s+/g, ' ').slice(0, 40),
    info: document.getElementById('pgInfo').textContent,
    prevDis: document.getElementById('pgPrev').disabled,
  }));
  console.log('第 2 页:', JSON.stringify(pg2));
  console.log(pg2.text.indexOf('第2页') >= 0 ? 'OK   翻页后内容换了' : 'ERR  翻页后内容没换');
  console.log(/page=2/.test(pg2.url) ? 'OK   网址带 page=2' : 'ERR  网址没带 page: ' + pg2.url);
  console.log(pg1.prevDis === true && pg2.prevDis === false ? 'OK   上一页按钮状态正确' : 'ERR  上一页按钮状态不对');

  // ---- 失败提示条：拦掉接口，页面必须自己说出来 ----
  const p2 = await ctx.newPage();
  let blocked = 0;
  await p2.route('**/api/tasks*', r => { blocked++; return r.abort(); });
  await p2.goto(BASE + '/tasks', { waitUntil: 'domcontentloaded' });
  await p2.waitForTimeout(1500);
  const bar = await p2.evaluate(() => {
    const b = document.querySelector('.netbar');
    return b ? { show: b.classList.contains('show'),
                 text: (b.querySelector('.netbar-t') || {}).textContent,
                 kind: b.dataset.kind } : null;
  });
  console.log('拦截次数:', blocked, '| 提示条:', JSON.stringify(bar));

  // ---- 恢复后提示条自己消失 ----
  await p2.unroute('**/api/tasks*');
  await p2.reload({ waitUntil: 'networkidle' });
  await p2.waitForTimeout(800);
  const gone = await p2.evaluate(() => {
    const b = document.querySelector('.netbar');
    return b ? b.classList.contains('show') : false;
  });
  console.log(gone ? 'ERR  恢复后提示条还在' : 'OK   恢复后提示条已收起');

  // ---- 手机视口：分页按钮可点、提示条不溢出 ----
  const m = await ctx.newPage();
  await m.setViewportSize({ width: 390, height: 844 });
  await m.goto(BASE + '/notices', { waitUntil: 'networkidle' });
  const mob = await m.evaluate(() => {
    const b = document.getElementById('pgNext').getBoundingClientRect();
    return { w: Math.round(b.width), h: Math.round(b.height),
             overflow: document.documentElement.scrollWidth > window.innerWidth };
  });
  console.log('手机分页按钮:', JSON.stringify(mob));

  console.log('\n控制台问题合计:', problems.length);
  problems.slice(0, 12).forEach(x => console.log('  -', x));
  await browser.close();
  process.exit(problems.length ? 1 : 0);
})();
