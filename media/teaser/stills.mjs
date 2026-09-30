// Usage: node stills.mjs <outDir> <frame>[:name] ...
// Renders several stills from a single bundle + browser session.
import {bundle} from '@remotion/bundler';
import {openBrowser, renderStill, selectComposition} from '@remotion/renderer';
import fs from 'node:fs';
import path from 'node:path';

const [outDir, ...frames] = process.argv.slice(2);
if (!outDir || frames.length === 0) {
  console.error('usage: node stills.mjs <outDir> <frame>[:name] ...');
  process.exit(1);
}
fs.mkdirSync(outDir, {recursive: true});
const serveUrl = await bundle({entryPoint: path.resolve('src/index.ts')});
const browser = await openBrowser('chrome');
const composition = await selectComposition({serveUrl, id: 'RoostTeaser', puppeteerInstance: browser});
for (const spec of frames) {
  const [n, name] = spec.split(':');
  const output = path.join(outDir, `${name ?? `f${n}`}.png`);
  await renderStill({composition, serveUrl, frame: Number(n), output, puppeteerInstance: browser});
  console.log(output);
}
await browser.close({silent: true});
