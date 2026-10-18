#!/usr/bin/env node
// Optional browser check; parcel discovery is also testable without browser access.
// BASE=http://127.0.0.1:8361 node scripts/check_overflow.js (needs playwright resolvable).
const BASE = process.env.BASE || "http://127.0.0.1:8361";
const PAGES = ["/", "/fields/", "/fields/?view=list", "/advisor/", "/advisor/about",
  "/forecast/plan", "/kpi/", "/operations/", "/inventory/", "/share/", "/activity/",
  "/stations/", "PARCEL"];

function discoverParcelPages(hrefs) {
  const candidates = hrefs.filter(href => typeof href === 'string' && href.startsWith('/') && !href.startsWith('//'))
    .map(href => ({ href, url: new URL(href, 'http://parcel-check.invalid') }));
  const workspace = candidates.find(({ url }) => /^\/fields\/parcels\/[1-9][0-9]*$/.test(url.pathname));
  if (workspace) {
    const id = workspace.url.pathname.split('/').pop();
    const farm = workspace.url.searchParams.get('farm');
    const canonical = `/advisor/parcels/${id}${farm ? '?' + new URLSearchParams({ farm }) : ''}`;
    return [workspace.href, canonical];
  }
  const canonical = candidates.find(({ url }) => /^\/advisor\/parcels\/[1-9][0-9]*$/.test(url.pathname));
  if (canonical) return [canonical.href];
  throw new Error('No parcel reading page discovered on Crop protection; parcel coverage is required');
}

async function main() {
  const { chromium } = require("playwright");
  const browser = await chromium.launch();
  let failed = false;
  try {
    for (const sunlight of [false, true]) {
      const ctx = await browser.newContext({ viewport: { width: 375, height: 812 } });
      if (sunlight) await ctx.addCookies([{ name: "sunlight", value: "1", url: BASE }]);
      const page = await ctx.newPage();
      for (const requestedPath of PAGES) {
        let paths = [requestedPath];
        if (requestedPath === "PARCEL") {
          await page.goto(BASE + "/advisor/", { waitUntil: "networkidle" });
          const hrefs = await page.evaluate(() => [...document.querySelectorAll('a[href]')]
            .map(a => a.getAttribute('href')));
          paths = discoverParcelPages(hrefs);
        }
        for (const path of paths) {
          await page.goto(BASE + path, { waitUntil: "networkidle" });
          await page.waitForTimeout(2000);
          const r = await page.evaluate(() => {
            const el = document.documentElement;
            const wide = [...document.querySelectorAll("body *")]
              .filter(e => e.getBoundingClientRect().right > el.clientWidth + 0.5)
              .slice(0, 5).map(e => e.tagName + "." + String(e.className).slice(0, 40));
            return { sw: el.scrollWidth, cw: el.clientWidth, wide };
          });
          const ok = r.sw <= r.cw;
          failed = failed || !ok;
          console.log(`${ok ? "ok  " : "FAIL"} ${sunlight ? "sunlight" : "normal  "} ${path} ${r.sw}/${r.cw}${ok ? "" : " " + r.wide.join(", ")}`);
        }
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  process.exitCode = failed ? 1 : 0;
}

module.exports = { discoverParcelPages };
if (require.main === module) main().catch(error => {
  console.error(`FAIL parcel/page coverage: ${error.message}`);
  process.exitCode = 1;
});
