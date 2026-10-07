// Renders the Open Graph image (public/og.png, 1200x630) and the PNG icons
// from HTML with the site's own fonts, using Playwright's Chromium (or the
// installed Chrome with PW_CHANNEL=chrome). The outputs are committed, so a
// Pages build does not need a browser; run `npm run og` after changing them.
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import sharp from 'sharp';

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const font = (pkg, file) =>
  `data:font/woff2;base64,${readFileSync(here(`../node_modules/@fontsource-variable/${pkg}/files/${file}`)).toString('base64')}`;

const fonts = `
@font-face { font-family: D; src: url(${font('bricolage-grotesque', 'bricolage-grotesque-latin-wght-normal.woff2')}); font-weight: 200 800; }
@font-face { font-family: B; src: url(${font('atkinson-hyperlegible-next', 'atkinson-hyperlegible-next-latin-wght-normal.woff2')}); font-weight: 200 800; }
@font-face { font-family: M; src: url(${font('jetbrains-mono', 'jetbrains-mono-latin-wght-normal.woff2')}); font-weight: 100 800; }`;

const mark = (fill = '#1b1a17', eye = '#fbfaf6') => `
<svg viewBox="0 0 32 32" width="100%" height="100%">
  <path d="M7 21.5c0-6.4 4.6-11.5 10.4-11.5 3.1 0 5.6 1.5 6.9 3.8L29 15.4l-4.1 1.3c.1.5.1 1 .1 1.5 0 1.3-.3 2.4-.9 3.3Z" fill="${fill}"/>
  <circle cx="20.6" cy="14.6" r="1.35" fill="${eye}"/>
  <path d="M10.5 21.5 7.5 25" stroke="${fill}" stroke-width="2" stroke-linecap="round" fill="none"/>
  <rect x="3" y="24.5" width="26" height="2.6" rx="1.3" fill="#d4581a"/>
</svg>`;

const og = `<!doctype html><html><head><style>${fonts}
* { margin: 0; box-sizing: border-box; }
body { width: 1200px; height: 630px; background: #fbfaf6; color: #1b1a17; font-family: B; position: relative; overflow: hidden; }
.dots { position: absolute; inset: 0; background: radial-gradient(circle at 1px 1px, rgba(27,26,23,.14) 1px, transparent 0) 0 0 / 24px 24px; }
.glow { position: absolute; inset: 0; background: radial-gradient(50% 70% at 88% 18%, rgba(196,84,27,.16), transparent 70%), linear-gradient(to bottom, transparent 40%, #fbfaf6 92%); }
.in { position: absolute; inset: 0; padding: 64px 72px; display: flex; flex-direction: column; }
.brand { display: flex; align-items: center; gap: 16px; font-family: D; font-weight: 760; font-size: 44px; letter-spacing: -1.5px; }
.brand i { width: 58px; height: 58px; display: block; }
.k { margin-top: 54px; font-family: M; font-weight: 600; font-size: 20px; letter-spacing: 3px; text-transform: uppercase; color: #9c3c0b; }
h1 { margin-top: 16px; font-family: D; font-weight: 760; font-size: 84px; line-height: 0.98; letter-spacing: -3px; max-width: 980px; }
p { margin-top: 26px; font-size: 27px; line-height: 1.4; color: #45423b; max-width: 1060px; }
.f { margin-top: auto; display: flex; justify-content: space-between; align-items: center; font-family: M; font-size: 19px; color: #66625a; border-top: 2px solid #1b1a17; padding-top: 20px; }
.f b { color: #1b1a17; font-weight: 600; }
</style></head><body><div class="dots"></div><div class="glow"></div>
<div class="in">
  <div class="brand"><i>${mark()}</i>roost</div>
  <div class="k">Open-source infrastructure for AI employees</div>
  <h1>One durable agent per customer.</h1>
  <p>Its own long-lived computer and transcripts. Survives crashes, never runs twice.</p>
  <div class="f"><span>Built on <b>Pi Durable</b> and <b>E2B</b> · Apache-2.0</span><span>github.com/JerryChaox/roost</span></div>
</div></body></html>`;

const icon = (size, pad) => `<!doctype html><html><head><style>
* { margin: 0; } body { width: ${size}px; height: ${size}px; background: #fbfaf6; display: grid; place-items: center; }
i { display: block; width: ${size - 2 * pad}px; height: ${size - 2 * pad}px; }
</style></head><body><i>${mark()}</i></body></html>`;

const browser = await chromium.launch(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {});
async function shot(html, w, h, out) {
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
  await page.setContent(html, { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  const png = await page.screenshot({ type: 'png' });
  await page.close();
  const optimized = await sharp(png).png({ compressionLevel: 9, palette: true, quality: 90, effort: 10 }).toBuffer();
  writeFileSync(here(`../public/${out}`), optimized);
  console.log(`public/${out}: ${w}x${h}, ${optimized.length} bytes`);
}

await shot(og, 1200, 630, 'og.png');
await shot(icon(512, 64), 512, 512, 'icon-512.png');
await shot(icon(180, 22), 180, 180, 'apple-touch-icon.png');
await shot(icon(32, 1), 32, 32, 'favicon-32.png');
await browser.close();
