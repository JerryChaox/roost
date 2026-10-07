// JSON-LD builders. Every URL is absolute and built from the one SITE_URL
// (astro.config `site`).
import { REPO_URL } from '../../site.config.mjs';
import { plain } from './text';

export const SITE_NAME = 'roost';
export const OG_IMAGE = '/og.png';

const abs = (site: URL, path: string) => new URL(path, site).href;

const publisher = (site: URL) => ({
  '@type': 'Organization',
  name: 'roost',
  url: site.href,
  logo: abs(site, '/icon-512.png'),
  sameAs: [REPO_URL],
});

export function softwareApplication(site: URL, description: string) {
  return {
    '@context': 'https://schema.org',
    '@type': 'SoftwareApplication',
    name: 'roost',
    description,
    url: site.href,
    applicationCategory: 'DeveloperApplication',
    applicationSubCategory: 'AI agent infrastructure',
    operatingSystem: 'Linux, macOS',
    license: 'https://www.apache.org/licenses/LICENSE-2.0',
    isAccessibleForFree: true,
    softwareVersion: 'v1alpha1',
    codeRepository: REPO_URL,
    downloadUrl: REPO_URL,
    image: abs(site, OG_IMAGE),
    offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
    publisher: publisher(site),
  };
}

export function website(site: URL, description: string) {
  return {
    '@context': 'https://schema.org',
    '@type': 'WebSite',
    name: SITE_NAME,
    url: site.href,
    description,
    inLanguage: 'en',
  };
}

export function techArticle(
  site: URL,
  a: { path: string; headline: string; description: string; published: string; updated: string; section: string },
) {
  return {
    '@context': 'https://schema.org',
    '@type': 'TechArticle',
    headline: a.headline,
    description: a.description,
    url: abs(site, a.path),
    mainEntityOfPage: abs(site, a.path),
    datePublished: a.published,
    dateModified: a.updated,
    articleSection: a.section,
    inLanguage: 'en',
    image: abs(site, OG_IMAGE),
    author: publisher(site),
    publisher: publisher(site),
    about: { '@type': 'SoftwareApplication', name: 'roost', url: site.href },
  };
}

export function faqPage(site: URL, path: string, faq: { q: string; a: string }[]) {
  return {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    url: abs(site, path),
    mainEntity: faq.map((f) => ({
      '@type': 'Question',
      name: plain(f.q),
      acceptedAnswer: { '@type': 'Answer', text: plain(f.a) },
    })),
  };
}

export function breadcrumbs(site: URL, items: { name: string; path: string }[]) {
  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: items.map((it, i) => ({
      '@type': 'ListItem',
      position: i + 1,
      name: it.name,
      item: abs(site, it.path),
    })),
  };
}
