/* 断点边界与极端屏幕：手机竖/横、小平板、320 小屏、桌面。
   只关心两件事——整页能不能左右拖（溢出），抽屉断点行为对不对。 */
const { chromium } = require('playwright-core');

const EXE = 'C:/Users/10517/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';
const BASE = 'http://127.0.0.1:5000';
const PAGES = ['/', '/tasks', '/orders', '/ledgers', '/kb', '/notices', '/reports', '/audit', '/system', '/admin/users', '/portal'];
const VIEWS = [
  [320, 568, 'iPhone SE 1 竖'],
  [360, 800, '安卓常见 竖'],
  [390, 844, 'iPhone 14 竖'],
  [430, 932, 'iPhone 15PM 竖'],
  [844, 390, '手机 横屏'],
  [768, 1024, 'iPad 竖'],
  [912, 1366, 'iPad Pro 竖'],
  [1024, 768, 'iPad 横屏'],
  [1440, 900, '桌面'],
];

(async () => {
  const browser = await chromium.launch({ executablePath: EXE });
  let bad = 0;
  for (const [w, h, label] of VIEWS) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, isMobile: w < 700, hasTouch: w < 700 });
    const page = await ctx.newPage();
    await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
    await page.fill('input[name=username]', 'admin');
    await page.fill('input[name=password]', 'admin123');
    await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.click('button[type=submit], input[type=submit]')]);

    const issues = [];
    for (const url of PAGES) {
      await page.goto(BASE + url, { waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(220);
      const r = await page.evaluate(() => {
        const de = document.documentElement;
        const tgl = document.getElementById('navToggle');
        const tglShown = tgl && getComputedStyle(tgl).display !== 'none';
        const side = document.getElementById('sidebar');
        const sideLeft = side ? Math.round(side.getBoundingClientRect().left) : null;
        const sideW = side ? Math.round(side.getBoundingClientRect().width) : null;
        return {
          scrollW: de.scrollWidth, innerW: window.innerWidth,
          tglShown, sideLeft, sideW,
        };
      });
      if (r.scrollW > r.innerW + 1) { issues.push(url + ' 横向溢出 ' + r.scrollW + '>' + r.innerW); }
      // 断点一致性：窄屏汉堡必须出现且侧栏收在屏外；宽屏正好相反
      // 居民端用底部导航、压根没有侧栏，汉堡断言对它不成立，只查溢出
      if (url === '/portal') continue;
      const narrow = r.innerW <= 900;
      if (narrow && !r.tglShown) issues.push(url + ' 窄屏没汉堡');
      if (narrow && r.sideLeft >= 0) issues.push(url + ' 窄屏侧栏没收起 left=' + r.sideLeft);
      if (!narrow && r.tglShown) issues.push(url + ' 宽屏不该有汉堡');
      if (!narrow && r.sideW !== 230) issues.push(url + ' 宽屏侧栏宽=' + r.sideW);
    }
    if (issues.length) bad += issues.length;
    console.log((issues.length ? ' 问题 ' : '  OK  ') + label.padEnd(16) + w + 'x' + h +
      (issues.length ? '\n        ' + issues.join('\n        ') : ''));
    await ctx.close();
  }
  console.log(bad ? `\n===== ${bad} 处问题 =====` : '\n===== 全部视口通过 =====');
  await browser.close();
})();
