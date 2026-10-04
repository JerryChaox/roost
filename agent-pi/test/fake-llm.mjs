// Fake model server for the tests (copied from spikes/pi-durable/fake-llm.mjs; port 0 picks a free port and the
// first stdout line reports it). Speaks just enough of the Anthropic Messages and OpenAI Responses
// streaming APIs for pi-ai's parsers. Never forwards anything anywhere.
//
//   node fake-llm.mjs <port> [logfile]
//
// Directives in the newest user text decide the answer:
//   [tool NAME {json}]  -> one tool call NAME(json)
//   [slow N]            -> N text deltas, 500 ms apart
//   [hang]              -> start streaming, then never finish (kill the host during the request)
//   otherwise           -> "echo: <text>"
// When the newest message is a tool result, the answer is "tool said: <result>".
import { appendFileSync } from "node:fs";
import { createServer } from "node:http";

const port = Number(process.argv[2] ?? 18080);
const logfile = process.argv[3];
let counter = 0;
const hungOnce = new Set();

function log(record) {
	const line = JSON.stringify({ t: new Date().toISOString(), ...record });
	console.log(line);
	if (logfile) appendFileSync(logfile, `${line}\n`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function textOf(content) {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((b) => (typeof b === "string" ? b : (b.text ?? (typeof b.content === "string" ? b.content : ""))))
		.join(" ");
}

// Normalise both APIs to { last: "user" | "tool", text }.
function lastTurn(api, body) {
	if (api === "anthropic") {
		const last = body.messages.at(-1);
		const toolResult = Array.isArray(last.content) && last.content.find((b) => b.type === "tool_result");
		if (toolResult) return { last: "tool", text: textOf(toolResult.content) };
		return { last: "user", text: textOf(last.content) };
	}
	const input = Array.isArray(body.input) ? body.input : [{ role: "user", content: body.input }];
	const last = input.at(-1);
	if (last.type === "function_call_output") return { last: "tool", text: String(last.output) };
	return { last: "user", text: textOf(last.content) };
}

function decide(turn) {
	if (turn.last === "tool") return { kind: "text", text: `tool said: ${turn.text.slice(0, 120)}` };
	const tool = /\[tool (\S+) (\{.*?\})\]/s.exec(turn.text);
	if (tool) return { kind: "tool", name: tool[1], args: tool[2] };
	const slow = /\[slow (\d+)\]/.exec(turn.text);
	if (slow) return { kind: "slow", n: Number(slow[1]) };
	if (turn.text.includes("[hang]")) return { kind: "hang" };
	if (turn.text.includes("[hangonce]")) {
		if (!hungOnce.has(turn.text)) {
			hungOnce.add(turn.text);
			return { kind: "hang" };
		}
		return { kind: "text", text: `recovered answer to: ${turn.text.slice(0, 80)}` };
	}
	return { kind: "text", text: `echo: ${turn.text.slice(0, 120)}` };
}

function sse(res, event, data) {
	res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

async function anthropic(res, body, plan) {
	const id = `msg_fake_${++counter}`;
	res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
	sse(res, "message_start", {
		type: "message_start",
		message: {
			id,
			type: "message",
			role: "assistant",
			model: body.model,
			content: [],
			stop_reason: null,
			stop_sequence: null,
			usage: { input_tokens: 10, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
		},
	});
	if (plan.kind === "tool") {
		const toolId = `toolu_fake_${counter}`;
		sse(res, "content_block_start", {
			type: "content_block_start",
			index: 0,
			content_block: { type: "tool_use", id: toolId, name: plan.name, input: {} },
		});
		sse(res, "content_block_delta", {
			type: "content_block_delta",
			index: 0,
			delta: { type: "input_json_delta", partial_json: plan.args },
		});
		sse(res, "content_block_stop", { type: "content_block_stop", index: 0 });
		sse(res, "message_delta", {
			type: "message_delta",
			delta: { stop_reason: "tool_use", stop_sequence: null },
			usage: { output_tokens: 5 },
		});
		sse(res, "message_stop", { type: "message_stop" });
		return res.end();
	}
	sse(res, "content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } });
	const deltas =
		plan.kind === "slow"
			? Array.from({ length: plan.n }, (_, i) => `chunk${i} `)
			: plan.kind === "hang"
				? ["started... "]
				: [plan.text];
	for (const d of deltas) {
		sse(res, "content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: d } });
		if (plan.kind === "slow") await sleep(500);
	}
	if (plan.kind === "hang") {
		log({ api: "anthropic", note: "hanging mid-stream" });
		return; // never end
	}
	sse(res, "content_block_stop", { type: "content_block_stop", index: 0 });
	sse(res, "message_delta", {
		type: "message_delta",
		delta: { stop_reason: "end_turn", stop_sequence: null },
		usage: { output_tokens: 5 },
	});
	sse(res, "message_stop", { type: "message_stop" });
	res.end();
}

async function openai(res, body, plan) {
	const id = `resp_fake_${++counter}`;
	let seq = 0;
	const ev = (type, data) => sse(res, type, { type, sequence_number: seq++, ...data });
	const base = { id, object: "response", created_at: Math.floor(Date.now() / 1000), model: body.model };
	res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
	ev("response.created", { response: { ...base, status: "in_progress", output: [] } });
	const usage = {
		input_tokens: 10,
		output_tokens: 5,
		total_tokens: 15,
		input_tokens_details: { cached_tokens: 0 },
		output_tokens_details: { reasoning_tokens: 0 },
	};
	if (plan.kind === "tool") {
		const item = { type: "function_call", id: `fc_${counter}`, call_id: `call_${counter}`, name: plan.name, arguments: "" };
		ev("response.output_item.added", { output_index: 0, item: { ...item, status: "in_progress" } });
		ev("response.function_call_arguments.delta", { item_id: item.id, output_index: 0, delta: plan.args });
		ev("response.function_call_arguments.done", { item_id: item.id, output_index: 0, arguments: plan.args });
		const done = { ...item, arguments: plan.args, status: "completed" };
		ev("response.output_item.done", { output_index: 0, item: done });
		ev("response.completed", { response: { ...base, status: "completed", output: [done], usage } });
		return res.end();
	}
	const msgId = `msg_${counter}`;
	ev("response.output_item.added", {
		output_index: 0,
		item: { type: "message", id: msgId, role: "assistant", status: "in_progress", content: [] },
	});
	ev("response.content_part.added", {
		item_id: msgId,
		output_index: 0,
		content_index: 0,
		part: { type: "output_text", text: "", annotations: [] },
	});
	const deltas =
		plan.kind === "slow"
			? Array.from({ length: plan.n }, (_, i) => `chunk${i} `)
			: plan.kind === "hang"
				? ["started... "]
				: [plan.text];
	for (const d of deltas) {
		ev("response.output_text.delta", { item_id: msgId, output_index: 0, content_index: 0, delta: d });
		if (plan.kind === "slow") await sleep(500);
	}
	if (plan.kind === "hang") {
		log({ api: "openai", note: "hanging mid-stream" });
		return;
	}
	const text = deltas.join("");
	ev("response.output_text.done", { item_id: msgId, output_index: 0, content_index: 0, text });
	const done = {
		type: "message",
		id: msgId,
		role: "assistant",
		status: "completed",
		content: [{ type: "output_text", text, annotations: [] }],
	};
	ev("response.output_item.done", { output_index: 0, item: done });
	ev("response.completed", { response: { ...base, status: "completed", output: [done], usage } });
	res.end();
}

const server = createServer((req, res) => {
	let raw = "";
	req.on("data", (c) => {
		raw += c;
	});
	req.on("end", async () => {
		const path = new URL(req.url, "http://fake").pathname;
		const api = path.endsWith("/messages") ? "anthropic" : path.endsWith("/responses") ? "openai" : undefined;
		const auth = req.headers["x-api-key"] ?? req.headers.authorization;
		if (!api) {
			log({ api: "unknown", method: req.method, url: req.url, auth });
			res.writeHead(404).end();
			return;
		}
		let body;
		try {
			body = JSON.parse(raw);
		} catch {
			res.writeHead(400).end();
			return;
		}
		const turn = lastTurn(api, body);
		const plan = decide(turn);
		log({
			api,
			url: req.url,
			auth,
			model: body.model,
			stream: body.stream,
			tools: (body.tools ?? []).map((t) => t.name ?? t.function?.name),
			systemChars: JSON.stringify(body.system ?? body.instructions ?? "").length,
			last: turn.last,
			text: turn.text.slice(0, 80),
			plan: plan.kind,
			thinking: body.thinking ?? body.reasoning ?? null,
		});
		res.on("close", () => {
			if (!res.writableEnded) log({ api, note: "client closed the connection mid-stream" });
		});
		try {
			if (api === "anthropic") await anthropic(res, body, plan);
			else await openai(res, body, plan);
		} catch (error) {
			log({ api, error: String(error) });
		}
	});
});
server.listen(port, "127.0.0.1", () => log({ listening: server.address().port }));
