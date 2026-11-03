/* 对线上实例做手机视口验收：能登录、无横向溢出、抽屉可用。
   和本地 _shot_mobile.js 的区别只有三点：带 sharecode 进站、超时放宽、
   截图单独存（online-*.png）。 */
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

const EXE = 'C:/Users/10517/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';
const BASE = 'https://community-agent.app.workbuddy.host';
const ENTRY = BASE + '/?sharecode=XMM2LKNQOR';
const OUT = 'F:/bise4/output/mobile';
const PAGES = [['/', 'fde'], ['/orders', 'orders'], ['/notices', 'notices'],
               ['/audit', 'audit'], ['/system', 'system'], ['/admin/users', 'admin_users']];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: EXE });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
    isMobile: true, hasTouch: true, locale: 'zh-CN',
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(60000);

  await page.goto(ENTRY, { waitUntil: 'domcontentloaded', timeout: 90000 });
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'admin123');
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 90000 }),
    page.click('button[type=submit], input[type=submit]'),
  ]);
  console.log('登录后:', page.url());

  let bad = 0;
  for (const [url, name] of PAGES) {
    await page.goto(BASE + url, { waitUntil: 'networkidle', timeout: 90000 }).catch(() => {});
    await page.waitForTimeout(600);
    const m = await page.evaluate(() => {
      const de = document.documentElement;
      const b = document.getElementById('navToggle');
      return {
        sw: de.scrollWidth, iw: window.innerWidth,
        nav: !!b && getComputedStyle(b).display !== 'none',
        sideLeft: Math.round(document.getElementById('sidebar').getBoundingClientRect().left),
        tw: document.querySelectorAll('.tw').length,
      };
    });
    const ok = m.sw <= m.iw + 1 && m.nav && m.sideLeft < 0;
    if (!ok) bad++;
    console.log((ok ? '  OK  ' : ' 问题 ') + name.padEnd(12) +
      ' http视口=' + m.iw + ' scrollW=' + m.sw + ' 汉堡=' + (m.nav ? '有' : '无') +
      ' 侧栏left=' + m.sideLeft + ' 表格容器=' + m.tw);
    await page.screenshot({ path: path.join(OUT, 'online-' + name + '.png') });
  }

  // 线上抽屉
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await page.waitForTimeout(500);
  await page.click('#navToggle');
  await page.waitForTimeout(500);
  const dr = await page.evaluate(() => ({
    left: Math.round(document.getElementById('sidebar').getBoundingClientRect().left),
    mask: !document.getElementById('sideMask').hidden,
  }));
  console.log('线上抽屉:', JSON.stringify(dr));
  await page.screenshot({ path: path.join(OUT, 'online-drawer.png') });

  console.log(bad ? '\n===== ' + bad + ' 页有问题 =====' : '\n===== 线上手机验收通过 =====');
  await browser.close();
})();
