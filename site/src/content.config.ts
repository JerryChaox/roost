import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

// One collection for every page except the home page. The entry id is the
// URL path ("guides/ai-agent-crash-recovery"), and the frontmatter carries
// everything the head, the JSON-LD and llms.txt need, so each fact has one
// place.
const pages = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/pages' }),
  schema: z.object({
    // <title>; checked to be at most 60 characters by scripts/check-seo.mjs
    title: z.string(),
    // meta description; at most 155 characters
    description: z.string(),
    // the one H1: the query people type
    h1: z.string(),
    // the 2–3 sentence direct answer shown first on the page
    answer: z.string(),
    section: z.enum(['Use cases', 'Guides', 'Compare', 'Docs']),
    // a short label for navigation and llms.txt
    nav: z.string(),
    order: z.number(),
    updated: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
    published: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
    related: z.array(z.string()).default([]),
    faq: z.array(z.object({ q: z.string(), a: z.string() })).default([]),
    // a version badge, such as "v1alpha1"
    badge: z.string().optional(),
  }),
});

export const collections = { pages };
