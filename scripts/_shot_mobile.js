/* 手机视口实测：登录后逐页打开，量"是否横向溢出"并截图。
   溢出的判定看 documentElement.scrollWidth 有没有超过视口宽——
   这是移动端最常见的坏味道：整页能左右拖，卡片被切掉一截。 */
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

const EXE = 'C:/Users/10517/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:5000';
const OUT = 'F:/bise4/output/mobile';
const PAGES = [
  ['/', 'fde'],
  ['/tasks', 'tasks'],
  ['/orders', 'orders'],
  ['/ledgers', 'ledgers'],
  ['/kb', 'kb'],
  ['/notices', 'notices'],
  ['/reports', 'reports'],
  ['/audit', 'audit'],
  ['/system', 'system'],
  ['/admin/users', 'admin_users'],
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    locale: 'zh-CN',
  });
  const page = await ctx.newPage();

  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'admin123');
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
    page.click('button[type=submit], input[type=submit]'),
  ]);
  console.log('登录后 URL:', page.url());

  const rows = [];
  for (const [url, name] of PAGES) {
    const resp = await page.goto(BASE + url, { waitUntil: 'networkidle' }).catch(() => null);
    await page.waitForTimeout(400);
    const m = await page.evaluate(() => {
      const de = document.documentElement;
      const over = [];
      document.querySelectorAll('body *').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > window.innerWidth + 1) {
          // 只记"自己溢出父级"的，忽略本来就在横向滚动容器里的
          if (el.closest('.tw')) return;
          over.push(el.tagName.toLowerCase() + '.' + (el.className || '').toString().split(' ')[0]
            + ' right=' + Math.round(r.right));
        }
      });
      return {
        scrollW: de.scrollWidth, innerW: window.innerWidth,
        over: over.slice(0, 6),
        hasNav: !!document.getElementById('navToggle'),
        navVisible: (() => { const b = document.getElementById('navToggle'); if (!b) return false;
          return getComputedStyle(b).display !== 'none'; })(),
        sideX: (() => { const s = document.getElementById('sidebar'); if (!s) return null;
          return Math.round(s.getBoundingClientRect().left); })(),
        tables: Array.from(document.querySelectorAll('.tw')).map((w) => ({
          need: w.scrollWidth > w.clientWidth + 1,
          sw: w.scrollWidth, cw: w.clientWidth,
        })),
      };
    });
    const ok = m.scrollW <= m.innerW + 1;
    rows.push({ name, status: resp ? resp.status() : 0, ok, ...m });
    console.log(
      (ok ? '  OK  ' : ' 溢出 ') + name.padEnd(12) +
      ' http=' + (resp ? resp.status() : 0) +
      ' scrollW=' + m.scrollW + '/' + m.innerW +
      ' 汉堡=' + (m.navVisible ? '显示' : '隐藏') +
      ' 侧栏left=' + m.sideX +
      ' 表格' + m.tables.map((t) => (t.need ? '可滑' : '自适应') + '(' + t.sw + '/' + t.cw + ')').join(',') +
      (m.over.length ? ' 溢出元素:' + m.over.join(' | ') : '')
    );
    await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: false });
  }

  // 抽屉：点汉堡 → 侧栏应移到 0，遮罩可见；点遮罩 → 收回
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);
  await page.click('#navToggle');
  await page.waitForTimeout(420);
  const opened = await page.evaluate(() => {
    const s = document.getElementById('sidebar');
    const mask = document.getElementById('sideMask');
    return {
      left: Math.round(s.getBoundingClientRect().left),
      maskShown: !mask.hidden && getComputedStyle(mask).display !== 'none',
      bodyLocked: getComputedStyle(document.body).overflow === 'hidden',
      expanded: document.getElementById('navToggle').getAttribute('aria-expanded'),
    };
  });
  console.log('抽屉打开:', JSON.stringify(opened));
  await page.screenshot({ path: path.join(OUT, 'drawer-open.png') });

  await page.click('#sideMask', { position: { x: 360, y: 400 } });
  await page.waitForTimeout(420);
  const closed = await page.evaluate(() => ({
    left: Math.round(document.getElementById('sidebar').getBoundingClientRect().left),
    maskHidden: document.getElementById('sideMask').hidden,
    expanded: document.getElementById('navToggle').getAttribute('aria-expanded'),
  }));
  console.log('抽屉关闭:', JSON.stringify(closed));

  // 桌面视口回归：1280 宽下侧栏必须回到原位、汉堡必须消失
  const dctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const dp = await dctx.newPage();
  await dp.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await dp.fill('input[name=username]', 'admin');
  await dp.fill('input[name=password]', 'admin123');
  await Promise.all([dp.waitForNavigation({ waitUntil: 'domcontentloaded' }), dp.click('button[type=submit], input[type=submit]')]);
  await dp.goto(BASE + '/orders', { waitUntil: 'networkidle' });
  const desk = await dp.evaluate(() => ({
    sideW: Math.round(document.getElementById('sidebar').getBoundingClientRect().width),
    toggle: getComputedStyle(document.getElementById('navToggle')).display,
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
  }));
  console.log('桌面 1280:', JSON.stringify(desk));
  await dp.screenshot({ path: path.join(OUT, 'desktop-orders.png') });

  fs.writeFileSync(path.join(OUT, 'report.json'), JSON.stringify({ rows, opened, closed, desk }, null, 2));
  const bad = rows.filter((r) => !r.ok);
  console.log('\n===== 横向溢出的页面:', bad.length ? bad.map((b) => b.name).join(', ') : '无', '=====');
  await browser.close();
})();
