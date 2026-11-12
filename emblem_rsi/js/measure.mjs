// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 EMBLEM-NLP
// Measures the viscous-letters page in headless Chromium through its small test hook (window.__vl).
// Usage: node measure.mjs --page PAGE.html --out OUT.json [--signature SIG.json]
// Needs puppeteer-core and @sparticuz/chromium; point EMBLEM_RSI_NODE_MODULES at the folder holding them.
// createRequire lets this ES module load CommonJS packages from an arbitrary node_modules folder
import { createRequire } from 'module';
import fs from 'fs';  // synchronous file reads and writes for the page, the signature and the result
import path from 'path';  // joins and resolves the node_modules location in a platform-neutral way

// Read the value that follows a `--name` flag on the command line, or undefined when the flag is absent.
// Parameter k is the flag name without dashes; argv[0] is node itself, so a real flag is found at index 1 or later.
// tiny parser: find the flag and return the next argument
const arg = k => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : undefined; };
// page HTML to test, output JSON path, and the optional audio signature JSON
const PAGE = arg('page'), OUT = arg('out'), SIGFILE = arg('signature');
// without a page and an output path there is nothing to do: print usage and exit with status 2
if (!PAGE || !OUT) { console.error('usage: node measure.mjs --page PAGE.html --out OUT.json [--signature SIG.json]'); process.exit(2); }
// package folder from the environment (set by loop.py's --node-modules), else ./node_modules
const NM = process.env.EMBLEM_RSI_NODE_MODULES || path.join(process.cwd(), 'node_modules');
// a require() rooted in that folder; noop.js need not exist, it only anchors the lookup
const req = createRequire(path.join(path.resolve(NM), 'noop.js'));
let puppeteer, chromium;  // filled in below once the packages load
// load puppeteer-core and the serverless Chromium build, unwrapping ES default exports when present
try { const p = req('puppeteer-core'); puppeteer = p.default ?? p; const c = req('@sparticuz/chromium'); chromium = c.default ?? c; }
// missing packages are a setup error: say where they were looked for and exit 2
catch (e) { console.error(`cannot load puppeteer-core / @sparticuz/chromium from ${NM}: ${e.message}`); process.exit(2); }

const BODY = fs.readFileSync(PAGE, 'utf8');  // the page body exactly as written; it is wrapped in a skeleton below
// Wrap the body in the same minimal document a published page gets: UTF-8, a phone viewport, a system font,
// and a rule that keeps [hidden] elements hidden even if page CSS overrides display.
// the full HTML served to the browser
const SK = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>body{margin:0;font:14px system-ui,sans-serif}[hidden]{display:none!important}</style></head><body>${BODY}</body></html>`;
// Content Security Policy copied from the hosting sandbox: inline scripts plus the five allowed CDNs, Google Fonts,
// data/blob media and workers, and no network connections, so the test fails the same way production would.
// the policy header value sent along with the page
const CSP = "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net/npm/ https://unpkg.com https://cdn.tailwindcss.com https://code.jquery.com; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; img-src data: blob:; media-src data: blob:; worker-src blob:; connect-src 'none'";
// parsed signature, or null when the run has none
const sig = SIGFILE ? JSON.parse(fs.readFileSync(SIGFILE, 'utf8')) : null;

// Start headless Chromium sized like a modern phone: 393 x 852 CSS pixels at 2x density, touch enabled, so
// layout overflow and frame cost are measured for the device class the page targets.
// launch with the serverless Chromium flags and binary; 'shell' is the lightweight headless mode
const browser = await puppeteer.launch({ args: [...chromium.args], executablePath: await chromium.executablePath(), headless: 'shell',
  // mobile viewport settings shared by every page opened below
  defaultViewport: { width: 393, height: 852, deviceScaleFactor: 2, isMobile: true, hasTouch: true } });

// Open the page in a fresh tab and wait until its test hook is ready.
// Parameter reduce (boolean) emulates the reduced-motion accessibility preference when true.
// Returns { page, errors }, where errors collects console errors and uncaught exceptions for the error metric.
// Every network request is intercepted: the page is served from memory and everything else is blocked.
async function open(reduce) {
  const page = await browser.newPage(); const errors = [];  // new tab plus a list that the listeners below fill in
  // record console errors, except ERR_FAILED messages caused by the deliberately blocked requests
  page.on('console', m => { if (m.type() === 'error' && !/ERR_FAILED/.test(m.text())) errors.push(m.text()); });
  // uncaught JavaScript exceptions count as errors too
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await page.setRequestInterception(true);  // every request must go through the handler below
  // serve the wrapped page with the CSP for the fake origin; abort every other request (CDNs, fonts, images)
  page.on('request', r => r.url() === 'https://test.local/' ? r.respond({ status: 200, contentType: 'text/html; charset=utf-8', headers: { 'content-security-policy': CSP }, body: SK }) : r.abort());
  // ask the page to behave as if the user prefers reduced motion
  if (reduce) await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
  // a fake https origin, so the page runs in a secure context like production; resolves on the load event
  await page.goto('https://test.local/', { waitUntil: 'load' });
  // wait up to 40 s for window.__vl with its stats() helper and for the #msg loading message to be hidden
  await page.waitForFunction('window.__vl && window.__vl.stats && document.getElementById("msg").hidden', { timeout: 40000 });
  return { page, errors };  // hand back the tab and its error list
}
// Pearson correlation coefficient of two numeric arrays over their common length; returns -1..1.
// Returns 0 when either series is constant (zero variance), so a flat response never looks correlated.
const pearson = (a, b) => {
  // compare only the overlapping part; accumulate sums for the means
  const n = Math.min(a.length, b.length); let ma = 0, mb = 0;
  // sum both series, then divide by n to get their means
  for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i]; } ma /= n; mb /= n;
  // covariance and the two variances, accumulated from deviations from the means
  let sab = 0, saa = 0, sbb = 0; for (let i = 0; i < n; i++) { const x = a[i] - ma, y = b[i] - mb; sab += x * y; saa += x * x; sbb += y * y; }
  // normalised covariance, guarded against a zero denominator
  return saa > 0 && sbb > 0 ? sab / Math.sqrt(saa * sbb) : 0;
};

// metrics feed the rubric directly; detail keeps per-liquid numbers for people reading the file
const metrics = {}, detail = { liquids: {} };
// First pass: the normal page, with motion enabled.
{
  const { page, errors } = await open(false);  // open a tab without reduced motion
  // horizontal overflow in CSS pixels: how far the document is wider than the viewport, 0 when it fits
  metrics.layout_overflow_px = await page.evaluate('Math.max(0, document.documentElement.scrollWidth - innerWidth)');
  // freeze the animation clock so the probe advances the simulation in exact steps
  await page.evaluate('window.__vl.set({paused:true})');
  // worst case across liquids for rest legibility, shaken legibility and displacement ratio, and the ripple flag
  let restMin = 9, vibMin = 9, ratioMin = 9, allRip = 1;
  for (const lq of ['lava', 'honey', 'mercury', 'oil']) {  // test each simulated liquid in turn
    // choose the liquid with the 55 Hz preset, no sound and no tilt, pour the letters again, simulate 6 s
    await page.evaluate(`window.__vl.set({liquid:'${lq}',preset:'55',level:0,tilt:0}); window.__vl.repour(true); window.__vl.run(6)`);
    // stats at rest: letter overlap with the target shapes (IoU) and RMS displacement
    const rest = await page.evaluate('window.__vl.stats()');
    // drive the vibration at 70% level and simulate 3 more seconds
    await page.evaluate('window.__vl.set({level:0.7}); window.__vl.run(3)');
    // the same statistics measured while the letters are shaking
    const vib = await page.evaluate('window.__vl.stats()');
    // how much more the letters move when shaken than at rest; rest is floored at 0.3 spacing to avoid tiny divisors
    const ratio = vib.dispRmsSpacings / Math.max(rest.dispRmsSpacings, 0.3);
    // keep the worst value of each across liquids; any liquid without ripples clears the flag
    restMin = Math.min(restMin, rest.iou); vibMin = Math.min(vibMin, vib.iou); ratioMin = Math.min(ratioMin, ratio); if (!(vib.rippleA > 0)) allRip = 0;
    // per-liquid detail, rounded to keep the JSON readable
    detail.liquids[lq] = { rest_iou: +rest.iou.toFixed(4), vib_iou: +vib.iou.toFixed(4), rest_disp: +rest.dispRmsSpacings.toFixed(3), vib_disp: +vib.dispRmsSpacings.toFixed(3), ripple_amp: +vib.rippleA.toFixed(4) };
  }
  // the four movement metrics, rounded the same way as the detail
  metrics.rest_iou_min = +restMin.toFixed(4); metrics.vib_iou_min = +vibMin.toFixed(4); metrics.vib_disp_ratio_min = +ratioMin.toFixed(3); metrics.ripples_all_on = allRip;
  if (sig) {  // the signature checks need a signature file
    // reset to lava at rest and settle for 4 s before the sound starts
    await page.evaluate('window.__vl.set({liquid:"lava",level:0,tilt:0}); window.__vl.repour(true); window.__vl.run(4)');
    // inject the signature into the page and set the level to 60%
    await page.evaluate(`window.__vl.loadSignature(${JSON.stringify(sig)}); window.__vl.set({level:0.6})`);
    // Sample in the middle of each 0.1 s timeline bin (sigT = 0.05 + 0.1 i) so rounding can never put a sample in the neighbouring bin.
    // sample count: one per 0.1 s timeline bin, skipping the last two, at most 200 (20 s)
    const amps = []; const n = Math.min(sig.timeline.rms.length - 2, 200);
    await page.evaluate('window.__vl.run(0.05)');  // move to the middle of the first bin
    // advance one bin at a time and read the page's vibration amplitude each step
    for (let i = 0; i < n; i++) { if (i > 0) await page.evaluate('window.__vl.run(0.1)'); amps.push(await page.evaluate('window.__vl.stats().amp')); }
    const rms = sig.timeline.rms;  // the signature's own loudness envelope, already normalised 0..1
    // correlation between page amplitude and the signature envelope: does the page follow the music?
    metrics.signature_amp_corr = +pearson(amps, rms.slice(0, n)).toFixed(3);
    // final state, which includes the pitch f (Hz) the page settled on
    const st = await page.evaluate('window.__vl.stats()');
    // trust the signature's bass pitch only inside 30..220 Hz and at confidence 0.25 or more
    const sp = sig.spectrum || {}, usable = sp.low_fundamental_hz >= 30 && sp.low_fundamental_hz <= 220 && (sp.low_fundamental_confidence || 0) >= 0.25;
    const expected = usable ? sp.low_fundamental_hz : 55;  // otherwise the page should fall back to its 55 Hz preset
    // pitch error in Hz between what the page uses and what it should use
    metrics.signature_f0_err_hz = +Math.abs(st.f - expected).toFixed(3);
    // context for a reader: expected and actual pitch, preset name and the range of sampled amplitudes
    detail.signature = { expected_hz: expected, page_hz: +st.f.toFixed(2), preset: st.preset, amp_min: +Math.min(...amps).toFixed(3), amp_max: +Math.max(...amps).toFixed(3), samples: n };
  // without a signature both metrics are null, which the rubric scores as not measured
  } else { metrics.signature_amp_corr = null; metrics.signature_f0_err_hz = null; }
  // standard state for the cost test: lava, 55 Hz preset, 45% level, no tilt, fresh pour
  await page.evaluate('window.__vl.set({liquid:"lava",preset:"55",level:0.45,tilt:0}); window.__vl.repour(true)');
  // time 20 frames, split into simulation (advance) and drawing
  const c = await page.evaluate('window.__vl.costSplit(20)');
  metrics.frame_ms = +(c.advanceMs + c.drawMs).toFixed(2);  // milliseconds per frame, simulation plus drawing
  // number of console errors in this pass, and the first five messages for diagnosis
  metrics.console_errors = errors.length; detail.console = errors.slice(0, 5);
  await page.close();  // close the tab before the second pass
}
// Second pass: a fresh tab that prefers reduced motion; the page should start paused and stay legible.
{
  const { page, errors } = await open(true);  // open with reduced motion emulated
  const s = await page.evaluate('window.__vl.stats()');  // read the state the page chose by itself
  // pass (1) only if paused, letters at least 80% overlapping their shapes, and no errors
  metrics.reduced_motion_ok = s.paused && s.iou >= 0.8 && errors.length === 0 ? 1 : 0;
  detail.reduced = { paused: s.paused, iou: +s.iou.toFixed(4) };  // keep the raw values for the report
  metrics.console_errors += errors.length;  // errors from this pass add to the first pass's count
  await page.close();  // close the second tab before shutting the browser down
}
const ua = await browser.version(); await browser.close();  // record the browser version, then shut Chromium down
// write metrics, detail and an environment note saying frame_ms comes from software rendering
fs.writeFileSync(OUT, JSON.stringify({ metrics, detail, env: { browser: ua, headless: 'chromium shell, software rendering; frame_ms is not a device measurement' } }, null, 2));
// one summary line on stdout, e.g. "measured rest_iou_min=0.9 ..."
console.log('measured', Object.entries(metrics).map(([k, v]) => `${k}=${v}`).join(' '));
