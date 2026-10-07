import type { APIRoute } from 'astro';

// Everything is public, for search engines and AI crawlers alike. The AI
// crawlers are named explicitly so the intent is unambiguous to anyone (or any
// bot) reading the file.
const agents = [
  '*',
  'Googlebot',
  'Bingbot',
  'GPTBot',
  'OAI-SearchBot',
  'ChatGPT-User',
  'PerplexityBot',
  'Perplexity-User',
  'Google-Extended',
  'ClaudeBot',
  'Claude-SearchBot',
  'Claude-User',
  'Applebot-Extended',
  'CCBot',
];

export const GET: APIRoute = ({ site }) => {
  const body = [
    ...agents.flatMap((a) => [`User-agent: ${a}`, 'Allow: /', '']),
    `Sitemap: ${new URL('/sitemap-index.xml', site).href}`,
    '',
  ].join('\n');
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
