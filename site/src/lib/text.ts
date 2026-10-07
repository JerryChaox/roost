// Tiny inline Markdown for short strings kept in frontmatter or data files
// (direct answers, FAQ answers): `code`, **bold** and [text](url). Anything
// else is escaped. The same strings go to JSON-LD and llms.txt as plain text
// through `plain`.

const escape = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export function inline(s: string): string {
  const parts = s.split(/(`[^`]+`)/g);
  return parts
    .map((part) => {
      if (part.startsWith('`') && part.endsWith('`') && part.length > 1) {
        return `<code>${escape(part.slice(1, -1))}</code>`;
      }
      return escape(part)
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text, href) => {
          const external = /^https?:/.test(href);
          return `<a href="${href}"${external ? ' rel="noopener"' : ''}>${text}</a>`;
        });
    })
    .join('');
}

/** The string without inline Markdown, for JSON-LD and meta tags. */
export function plain(s: string): string {
  return s
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
}

/** Absolute URLs in Markdown links, for llms-full.txt read out of context. */
export function absolutize(md: string, site: string): string {
  return md.replace(/\]\((\/[^)\s]*)\)/g, (_m, path) => `](${site}${path})`);
}

export function formatDate(iso: string): string {
  return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  });
}
