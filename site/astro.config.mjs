// @ts-check
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';
import { SITE_URL } from './site.config.mjs';
import { HOME_UPDATED } from './src/data/updated.mjs';

// Each content page's `updated:` frontmatter date becomes its sitemap
// <lastmod>, so the sitemap and the page's "Last updated" line read the same
// value.
const contentDir = fileURLToPath(new URL('./src/content/pages/', import.meta.url));
/** @param {string} dir @returns {string[]} */
const walk = (dir) =>
  readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? walk(p) : p.endsWith('.md') ? [p] : [];
  });
/** @type {Map<string, string>} */
const lastmod = new Map([['/', HOME_UPDATED]]);
for (const file of walk(contentDir)) {
  const m = readFileSync(file, 'utf8').match(/^updated:\s*['"]?(\d{4}-\d{2}-\d{2})/m);
  if (m) lastmod.set('/' + relative(contentDir, file).replace(/\.md$/, '') + '/', m[1]);
}

export default defineConfig({
  site: SITE_URL,
  trailingSlash: 'always',
  // Inline the stylesheet: one small file, so no render-blocking request.
  build: { format: 'directory', inlineStylesheets: 'always' },
  integrations: [
    sitemap({
      serialize(item) {
        const date = lastmod.get(new URL(item.url).pathname);
        if (date) item.lastmod = new Date(date).toISOString();
        return item;
      },
    }),
  ],
  markdown: {
    shikiConfig: {
      themes: { light: 'github-light-default', dark: 'github-dark-default' },
      defaultColor: false,
      wrap: false,
    },
  },
});
