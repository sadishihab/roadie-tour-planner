// Headless Chromium check for frontend/index.html at narrow widths (used by test_frontend.py; no network).
// usage: node browser_overflow.js <input.json>  -> JSON. Every request is answered from the input, nothing leaves the machine.
'use strict';
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let pw;
for (const where of [undefined, process.env.NODE_PATH, '/opt/node-tools/node_modules']) {
  try { pw = require(where ? require.resolve('playwright-core', { paths: [where] }) : 'playwright-core'); break; } catch (e) { /* next */ }
}
if (!pw) { process.stdout.write(JSON.stringify({ skip: 'playwright-core not found' })); process.exit(0); }

(async () => {
  const browser = await pw.chromium.launch({ executablePath: input.chrome, args: ['--no-sandbox'] });
  const out = [];
  for (const width of input.widths) {
    const page = await browser.newPage({ viewport: { width, height: 800 } });
    await page.route('**/*', (route) => {
      const u = new URL(route.request().url());
      const hit = input.routes[u.pathname];
      if (!hit) { return route.fulfill({ status: 404, body: '{}' }); }
      return route.fulfill({ status: 200, contentType: hit.type, headers: hit.headers || {}, body: hit.body });
    });
    await page.goto('http://roadie.test/');
    await page.waitForSelector('#gallery .card');
    const measure = () => page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth }));
    const row = { width, gallery: await measure(), plans: {} };
    row.galleryCards = await page.evaluate(() => document.querySelectorAll('#gallery .card').length);
    for (let i = 0; i < row.galleryCards; i++) {
      await page.locator('#gallery .card').nth(i).click();
      await page.waitForSelector('#plan-view:not([hidden]) #plan section');
      await page.evaluate(() => document.querySelectorAll('#plan details').forEach((d) => { d.open = true; }));
      const name = await page.evaluate(() => document.querySelector('#plan h2, #plan h1').textContent);
      const m = await measure();
      m.small = await page.evaluate(() => {
        const heights = (sel) => Array.from(document.querySelectorAll(sel)).filter((e) => e.offsetParent).map((e) => e.getBoundingClientRect().height);
        return { buttons: heights('#plan button'), cityRows: heights('#plan section details > summary') };
      });
      row.plans[i + ':' + name] = m;
      await page.click('#back');
      await page.waitForSelector('#gallery .card', { state: 'visible' });
    }
    row.home = await measure();
    row.homeButtons = await page.evaluate(() => Array.from(document.querySelectorAll('#home button')).filter((e) => e.offsetParent).map((e) => e.getBoundingClientRect().height));
    await page.close();
    out.push(row);
  }
  await browser.close();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
