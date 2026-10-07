import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import { homeMarkdown } from '../data/home';
import { HOME_UPDATED } from '../data/updated.mjs';
import { absolutize } from '../lib/text';

// Every page's content as Markdown, from the same sources the HTML pages
// render: the home page's data module and each page's Markdown file.
export const GET: APIRoute = async ({ site }) => {
  const origin = new URL('/', site).href.replace(/\/$/, '');
  const pages = (await getCollection('pages')).sort((a, b) => a.data.order - b.data.order);
  const parts = [
    `<!-- ${origin}/ · last updated ${HOME_UPDATED} -->`,
    homeMarkdown(),
    ...pages.map((p) => {
      const d = p.data;
      const faq = d.faq.length ? ['', '## FAQ', '', ...d.faq.flatMap((f) => [`### ${f.q}`, '', f.a, ''])].join('\n') : '';
      return [
        `<!-- ${origin}/${p.id}/ · last updated ${d.updated} -->`,
        `# ${d.h1}`,
        '',
        d.badge ? `Version: ${d.badge}\n` : '',
        `**Short answer:** ${d.answer}`,
        '',
        (p.body ?? '').trim(),
        faq,
      ].join('\n');
    }),
  ];
  const body = absolutize(parts.join('\n\n---\n\n'), origin) + '\n';
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
