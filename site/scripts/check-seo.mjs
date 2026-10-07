// Checks the built site in dist/ for the SEO and GEO contract: every page has
// a title (<= 60 chars), a description (<= 155), a canonical URL on SITE_URL,
// Open Graph and Twitter tags, exactly one <h1>, JSON-LD that parses, and is
// listed in the sitemap; internal links resolve; robots.txt, llms.txt and
// llms-full.txt exist. Run after `npm run build`. Exits 1 on any failure.
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SITE_URL } from '../site.config.mjs';

const dist = fileURLToPath(new URL('../dist/', import.meta.url));
const walk = (d) =>
  readdirSync(d).flatMap((f) => {
    const p = join(d, f);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
const decode = (s) =>
  s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');

const errors = [];
const fail = (page, msg) => errors.push(`${page}: ${msg}`);
const meta = (html, attr, name) => {
  const re = new RegExp(`<meta[^>]*${attr}="${name}"[^>]*>`, 'i');
  const tag = html.match(re)?.[0];
  return tag ? decode(tag.match(/content="([^"]*)"/)?.[1] ?? '') : undefined;
};

const htmlFiles = walk(dist).filter((f) => f.endsWith('.html'));
const paths = htmlFiles.map((f) => '/' + relative(dist, f).replace(/index\.html$/, ''));
const sitemap = readFileSync(join(dist, 'sitemap-0.xml'), 'utf8');
const rows = [];

for (const [i, file] of htmlFiles.entries()) {
  const path = paths[i];
  const html = readFileSync(file, 'utf8');
  const title = decode(html.match(/<title>([^<]*)<\/title>/)?.[1] ?? '');
  const desc = meta(html, 'name', 'description') ?? '';
  const canonical = html.match(/<link rel="canonical" href="([^"]+)"/)?.[1];
  const h1s = html.match(/<h1[\s>]/g)?.length ?? 0;
  const h1 = decode((html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/)?.[1] ?? '').replace(/<[^>]+>/g, ''));

  if (!title) fail(path, 'no <title>');
  if (title.length > 60) fail(path, `title is ${title.length} chars (> 60): ${title}`);
  if (!desc) fail(path, 'no meta description');
  if (desc.length > 155) fail(path, `description is ${desc.length} chars (> 155)`);
  if (canonical !== SITE_URL + path) fail(path, `canonical ${canonical} is not ${SITE_URL + path}`);
  if (h1s !== 1) fail(path, `${h1s} <h1> elements`);
  for (const [attr, name] of [
    ['property', 'og:title'], ['property', 'og:description'], ['property', 'og:url'], ['property', 'og:image'],
    ['name', 'twitter:card'], ['name', 'twitter:image'],
  ]) if (!meta(html, attr, name)) fail(path, `no ${name}`);
  if (!/<html lang="en">/.test(html)) fail(path, 'no lang');
  if (!/Last updated <time datetime="\d{4}-\d{2}-\d{2}">/.test(html)) fail(path, 'no "Last updated" date');

  const types = [];
  for (const m of html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)) {
    try {
      const obj = JSON.parse(m[1]);
      types.push(obj['@type']);
      if (obj['@context'] !== 'https://schema.org') fail(path, `JSON-LD ${obj['@type']} has no schema.org @context`);
    } catch (e) {
      fail(path, `JSON-LD does not parse: ${e.message}`);
    }
  }
  if (!types.includes('BreadcrumbList')) fail(path, 'no BreadcrumbList');
  if (path === '/' && !types.includes('SoftwareApplication')) fail(path, 'no SoftwareApplication');
  if (path !== '/' && !types.includes('TechArticle')) fail(path, 'no TechArticle');
  if (/id="faq"/.test(html) && !types.includes('FAQPage')) fail(path, 'FAQ section without FAQPage');
  if (!sitemap.includes(`<loc>${SITE_URL + path}</loc>`)) fail(path, 'not in sitemap');

  for (const m of html.matchAll(/href="(\/[^"#]*)(#[^"]*)?"/g)) {
    const target = m[1];
    if (target.startsWith('/_astro/')) continue;
    if (!existsSync(join(dist, target)) && !existsSync(join(dist, target, 'index.html'))) fail(path, `broken link ${target}`);
    if (!/\.[a-z0-9]+$/i.test(target) && !target.endsWith('/')) fail(path, `link without trailing slash ${target}`);
  }
  rows.push({ path, title, titleLen: title.length, descLen: desc.length, h1, jsonld: types.join(', ') });
}

for (const f of ['robots.txt', 'llms.txt', 'llms-full.txt', 'sitemap-index.xml', 'og.png', '_redirects', '_headers']) {
  if (!existsSync(join(dist, f))) fail('/', `missing ${f}`);
}
const robots = readFileSync(join(dist, 'robots.txt'), 'utf8');
for (const bot of ['GPTBot', 'PerplexityBot', 'Google-Extended', 'ClaudeBot']) {
  if (!new RegExp(`User-agent: ${bot}\\nAllow: /`).test(robots)) fail('/robots.txt', `${bot} not allowed`);
}
if (/Disallow:\s*\//.test(robots)) fail('/robots.txt', 'disallows something');
const llms = readFileSync(join(dist, 'llms.txt'), 'utf8');
for (const p of paths) if (!llms.includes(SITE_URL + p)) fail('/llms.txt', `does not link ${p}`);
const full = readFileSync(join(dist, 'llms-full.txt'), 'utf8');
for (const r of rows) if (!full.includes(`# ${r.h1}`)) fail('/llms-full.txt', `has no section for ${r.path}`);

console.table(rows.map(({ path, titleLen, descLen, jsonld }) => ({ path, titleLen, descLen, jsonld })));
console.log(`${rows.length} pages, sitemap ${sitemap.match(/<loc>/g).length} URLs, llms.txt ${llms.length} B, llms-full.txt ${full.length} B`);
if (errors.length) {
  console.error(`\n${errors.length} problem(s):\n` + errors.map((e) => `  - ${e}`).join('\n'));
  process.exit(1);
}
console.log('SEO checks passed.');
