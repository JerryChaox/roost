import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import { home, roadmap } from '../data/home';
import { plain } from '../lib/text';
import { REPO_URL } from '../../site.config.mjs';

// llms.txt (https://llmstxt.org): an H1, a blockquote summary, a few plain
// paragraphs, then H2 sections of links with one-line notes.
export const GET: APIRoute = async ({ site }) => {
  const url = (p: string) => new URL(p, site).href;
  const pages = (await getCollection('pages')).sort((a, b) => a.data.order - b.data.order);
  const section = (name: string) =>
    pages
      .filter((p) => p.data.section === name)
      .map((p) => `- [${p.data.h1}](${url(`/${p.id}/`)}): ${p.data.description}`);

  const body = [
    '# roost',
    '',
    `> ${plain(home.answer)}`,
    '',
    plain(home.layer),
    '',
    'Status: v1alpha1. Milestone M1 works today and is verified end to end on E2B Cloud: one long-lived E2B sandbox per workspace, Pi Durable as the agent, exactly one agent per workspace (execution grants; older tokens rejected), each message id admitted once (a repeat returns 200 duplicate), runs that continue after the agent host crashes mid-tool-call, a new grant on the same sandbox after a lost driver, idle sandboxes that pause and wake on the next request, an HTTP conversation API (create by key, send with queue or steer, read entries, SSE events, interrupt, reset), model credentials through a SecretProvider, SQLite, one Go binary plus a TypeScript agent host. License: Apache-2.0.',
    '',
    `Not available yet (roadmap): ${roadmap.map((r) => `${plain(r.text)} (${r.tag})`).join('; ')}.`,
    '',
    `Full text of every page: ${url('/llms-full.txt')}`,
    '',
    '## Start here',
    '',
    `- [roost home](${url('/')}): what roost is, the problems it solves, how it works, status and FAQ.`,
    ...section('Docs'),
    `- [Source code on GitHub](${REPO_URL}): Go control plane, sandbox driver and the TypeScript Pi Durable agent host.`,
    '',
    '## Use cases',
    '',
    ...section('Use cases'),
    '',
    '## Guides',
    '',
    ...section('Guides'),
    ...section('Compare'),
    '',
    '## Optional',
    '',
    `- [RFC 0001: a durable agent runtime built on Pi Durable](${REPO_URL}/blob/main/docs/rfcs/0001-durable-agent-runtime.md): the design, including parts that are still on the roadmap.`,
    `- [Contracts (v1alpha1)](${REPO_URL}/blob/main/docs/specs/contracts.md): the HTTP APIs, invariants and conformance scenarios.`,
    `- [Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable): the agent loop roost runs.`,
    '',
  ].join('\n');
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
