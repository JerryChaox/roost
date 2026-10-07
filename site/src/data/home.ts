// The home page's content, kept as data so the page, its JSON-LD and
// llms-full.txt all read the same words. Strings may use the inline Markdown
// of src/lib/text.ts.

export const home = {
  title: 'roost: open-source infrastructure for AI employees',
  description:
    'Give every customer a durable AI agent with its own long-lived computer and transcripts. Survives crashes, never runs twice. Built on Pi Durable and E2B.',
  h1: 'Open-source infrastructure for AI employees: one durable agent per customer.',
  sub: 'Each customer or user gets an agent with its own long-lived computer and transcripts. It survives crashes and never runs twice. roost is built on Pi Durable and E2B, and is open source under Apache-2.0.',
  answer:
    'roost is an open-source runtime that gives every customer or user their own AI agent. Each agent lives in one long-lived E2B sandbox with its own files and transcripts, runs Pi Durable so a crash resumes from the last step, and is guaranteed to be the only copy running. Your backend talks to it over a small HTTP API.',
  layer:
    'roost is not a sandbox provider and not an agent loop. It is the layer between them: E2B provides the sandboxes, Pi Durable is the agent, and roost gives each workspace one sandbox, keeps exactly one agent running in it, and routes your messages to it.',
};

export const problems = [
  {
    quote: 'My agent crashed at hour three and started over.',
    answer: 'It continues from its last step.',
    how: 'Pi Durable commits every model call and tool call before it is shown. When the agent process dies, roost restarts it in the same sandbox and the run picks up where it stopped.',
    link: { href: '/guides/ai-agent-crash-recovery/', text: 'What happens when an agent crashes' },
  },
  {
    quote: 'The same message got two replies.',
    answer: 'Each message is admitted once.',
    how: 'Every message carries an id you choose, such as the chat message id. The agent records it with the transcript, and sending the same id again returns `200 duplicate` instead of starting another run.',
    link: { href: '/guides/ai-agent-duplicate-replies/', text: 'Why agents reply twice' },
  },
  {
    quote: 'Two copies of the agent wrote the same files.',
    answer: 'Exactly one agent per workspace.',
    how: 'roost issues one execution grant per workspace. A new grant ends the old one in the same database transaction, and the sandbox rejects every request that carries an older token.',
    link: { href: '/use-cases/ai-employee-per-customer/', text: 'One agent per customer' },
  },
];

export const steps = [
  {
    title: 'Create a workspace for each customer',
    body: '`PUT /v1/workspaces/user-42`. roost creates one long-lived E2B sandbox for it and starts Pi Durable inside. The workspace is the agent’s computer: its files and its storage.',
  },
  {
    title: 'Open a conversation per thread',
    body: 'Create a conversation with a `key` you already have, such as a chat thread id. The same `key` always returns the same conversation, so you store no extra ids.',
  },
  {
    title: 'Send messages with your own ids',
    body: 'Each message has an `id`, so retries are safe. A message sent while the agent is busy waits for the next run (`queue`) or joins the running one (`steer`).',
  },
  {
    title: 'Stream the answer; roost handles failure',
    body: 'Read the transcript or follow live events over SSE. If the agent process or its driver dies, the run continues. An idle sandbox pauses and wakes on the next request.',
  },
];

export const curl = `# a workspace per user, a conversation per thread
curl -X PUT localhost:7070/v1/workspaces/user-42 -H "Authorization: Bearer $ROOST_KEY"
# ready once GET /v1/workspaces/user-42 shows "phase": "active"
curl -X POST localhost:7070/v1/workspaces/user-42/conversations -H "Authorization: Bearer $ROOST_KEY" \\
  -d '{"key": "slack:C123:1712.0000"}'
# {"conversation": "c_01J9Z..."}

# send a message (its id makes retries safe), then stream the answer
curl -X POST localhost:7070/v1/workspaces/user-42/conversations/c_01J9Z.../messages \\
  -H "Authorization: Bearer $ROOST_KEY" -d '{"id": "m-1", "text": "Fix the failing test"}'
curl -N -H "Authorization: Bearer $ROOST_KEY" localhost:7070/v1/workspaces/user-42/conversations/c_01J9Z.../events`;

export const worksToday = [
  'One long-lived sandbox per workspace on E2B Cloud',
  'Pi Durable as the agent, with any model it supports',
  'Exactly one agent per workspace: execution grants, older tokens rejected',
  'Each message id admitted once; a repeat returns `200 duplicate`',
  'The agent host crashes mid-tool-call: the run continues and completes',
  'A lost driver: a new grant on the same sandbox, conversations and transcripts kept',
  'Idle sandboxes pause and wake on the next request',
  'Conversation API over HTTP: create by `key`, send with `queue` or `steer`, read entries, SSE events, interrupt, reset',
  'Model credentials through a `SecretProvider`',
  'SQLite; one Go binary plus a TypeScript agent host',
];

export const roadmap = [
  { tag: 'M2', text: 'Continuous backups with kopia' },
  { tag: 'M2', text: 'Restore a workspace to any step' },
  { tag: 'M2', text: 'Fork a snapshot into N workspaces' },
  { tag: 'M3', text: 'Self-hosting on your own KVM machines with E2B Embed' },
  { tag: 'M3', text: 'Per-grant model keys from a LiteLLM gateway' },
  { tag: 'M3', text: 'One-command deployments' },
  { tag: 'Planned', text: 'The `roost` CLI and the operator API' },
  { tag: 'Planned', text: 'Postgres' },
];

export const faq = [
  {
    q: 'What is roost?',
    a: 'roost is open-source infrastructure for running one durable AI agent per customer or user. Each agent gets a long-lived computer (an E2B sandbox) with its own files and transcripts, survives process crashes, and is never running twice. Your backend talks to it over HTTP.',
  },
  {
    q: 'Is roost an agent framework or a sandbox?',
    a: 'Neither. The agent loop is Pi Durable and the sandboxes come from E2B. roost is the layer between them: it gives each workspace one sandbox, keeps exactly one agent running in it, and routes your messages to it.',
  },
  {
    q: 'Which agent and models does roost run?',
    a: 'roost runs Pi Durable, with Pi’s prompts, tools and skills, and any model Pi supports, set with `agent.model` in `roost.yaml`. It does not run other agents such as OpenClaw, Hermes, Claude Code or Codex.',
  },
  {
    q: 'Does “never runs twice” mean exactly-once?',
    a: 'It means one live agent per workspace and each message admitted once. It does not mean exactly-once side effects: a tool that was running when a process died comes back as interrupted unless it is safe to replay, and anything it already did in the outside world is not undone.',
  },
  {
    q: 'Can I self-host roost?',
    a: 'You run `roost serve`, one Go binary with SQLite, on your own machine or server. Today the sandboxes run on E2B Cloud, so you need an E2B account. Running sandboxes on your own KVM machines with E2B Embed is on the roadmap (M3).',
  },
  {
    q: 'Is roost ready for production?',
    a: 'Not yet. The API is v1alpha1 and may change. Milestone M1 is verified end to end on E2B Cloud, but backups are not built yet (M2): if a sandbox itself is lost, its files and transcripts are lost with it.',
  },
  {
    q: 'What does roost cost?',
    a: 'roost is free and open source under the Apache-2.0 license. You pay for the E2B sandboxes and the model calls your agents make.',
  },
];

/** The home page as Markdown, for llms-full.txt. */
export function homeMarkdown(): string {
  const lines: string[] = [];
  lines.push(`# ${home.h1}`, '', home.answer, '', home.layer, '');
  lines.push('## Three problems roost solves', '');
  for (const p of problems) lines.push(`- **“${p.quote}”** ${p.answer} ${p.how}`);
  lines.push('', '## How it works', '');
  steps.forEach((s, i) => lines.push(`${i + 1}. **${s.title}.** ${s.body}`));
  lines.push('', '```bash', curl, '```', '');
  lines.push('## Status', '', '### Works today (milestone M1, verified end to end on E2B Cloud)', '');
  for (const w of worksToday) lines.push(`- ${w}`);
  lines.push('', '### On the roadmap (not available yet)', '');
  for (const r of roadmap) lines.push(`- ${r.text} (${r.tag})`);
  lines.push('', '## FAQ', '');
  for (const f of faq) lines.push(`### ${f.q}`, '', f.a, '');
  return lines.join('\n');
}
