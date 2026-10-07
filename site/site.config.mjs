// The site's few deploy-time settings, read from the environment so that
// Cloudflare Pages can set them without a code change.
//
//   SITE_URL                  the public origin, used for canonical URLs, the
//                             sitemap, robots.txt and llms.txt. The domain is not
//                             final: change it here or set SITE_URL in Pages.
//   GOOGLE_SITE_VERIFICATION  the content of Search Console's
//                             <meta name="google-site-verification"> tag. Empty
//                             (the default) omits the tag.

export const SITE_URL = (process.env.SITE_URL || 'https://roostagents.com').replace(/\/+$/, '');

export const GOOGLE_SITE_VERIFICATION = (process.env.GOOGLE_SITE_VERIFICATION || '').trim();

export const REPO_URL = 'https://github.com/JerryChaox/roost';
