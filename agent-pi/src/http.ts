// The conversation interface (driver-protocol §4) over HTTP on a Unix socket.
import { lstatSync, mkdirSync, unlinkSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { dirname } from "node:path";
import { withAbortSignal } from "@earendil-works/chord/context";
import { type AgentEvent, watchEvents } from "@earendil-works/pi-durable";
import { ApiError, ctx, type Host, type WireEntry } from "./host.ts";
import type { Logger } from "./log.ts";
import type { ConvState } from "./runs.ts";

const MAX_BODY = 8 << 20;
const KEEPALIVE_MS = 15_000;
const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 500;

const isObject = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value);

const invalid = (detail: string) => new ApiError(400, "invalid_request", detail);

function sendJson(res: ServerResponse, status: number, body: unknown): void {
	const text = JSON.stringify(body);
	res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(text) });
	res.end(text);
}

async function readJson(req: IncomingMessage, required: boolean): Promise<Record<string, unknown>> {
	const chunks: Buffer[] = [];
	let size = 0;
	for await (const chunk of req) {
		size += (chunk as Buffer).length;
		if (size > MAX_BODY) throw invalid("request body too large");
		chunks.push(chunk as Buffer);
	}
	const text = Buffer.concat(chunks).toString("utf8").trim();
	if (text === "") {
		if (required) throw invalid("a JSON body is required");
		return {};
	}
	let body: unknown;
	try {
		body = JSON.parse(text);
	} catch {
		throw invalid("body is not valid JSON");
	}
	if (!isObject(body)) throw invalid("body must be a JSON object");
	return body;
}

function onlyKeys(body: Record<string, unknown>, allowed: readonly string[]): void {
	const extra = Object.keys(body).filter((key) => !allowed.includes(key));
	if (extra.length > 0) throw invalid(`unknown field(s) ${extra.join(", ")}`);
}

function optionalString(body: Record<string, unknown>, field: string): string | undefined {
	const value = body[field];
	if (value === undefined) return undefined;
	if (typeof value !== "string") throw invalid(`${field} must be a string`);
	return value;
}

function requiredString(body: Record<string, unknown>, field: string): string {
	const value = body[field];
	if (typeof value !== "string" || value.length === 0) throw invalid(`${field} must be a non-empty string`);
	return value;
}

function parseLimit(value: string | null): number {
	if (value === null) return DEFAULT_LIMIT;
	if (!/^[0-9]+$/.test(value)) throw invalid("limit must be a positive integer");
	const limit = Number(value);
	if (limit < 1 || limit > MAX_LIMIT) throw invalid(`limit must be between 1 and ${MAX_LIMIT}`);
	return limit;
}

function parseCursor(value: string | null | undefined, field: string): number {
	if (value === null || value === undefined || value === "") return 0;
	if (!/^[0-9]+$/.test(value)) throw invalid(`${field} must be an entry cursor`);
	return Number(value);
}

export function createConversationServer(host: Host, log: Logger): Server {
	const server = createServer((req, res) => {
		handle(host, log, req, res).catch((error: unknown) => {
			if (error instanceof ApiError) {
				if (!res.headersSent) sendJson(res, error.status, { error: error.code, detail: error.message });
				else res.end();
				return;
			}
			log.error(`${req.method} ${req.url}: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
			if (!res.headersSent) {
				const status = host.closing ? 503 : 500;
				sendJson(res, status, { error: host.closing ? "not_ready" : "internal", detail: String(error) });
			} else res.end();
		});
	});
	return server;
}

/** Remove a stale socket file (this process holds the storage lock) and listen. */
export function listen(server: Server, socket: string): Promise<void> {
	mkdirSync(dirname(socket), { recursive: true });
	try {
		lstatSync(socket);
		unlinkSync(socket);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	return new Promise((resolve, reject) => {
		server.once("error", reject);
		server.listen(socket, () => {
			server.off("error", reject);
			resolve();
		});
	});
}

async function handle(host: Host, log: Logger, req: IncomingMessage, res: ServerResponse): Promise<void> {
	const url = new URL(req.url ?? "/", "http://agent");
	const parts = url.pathname.split("/").filter((part) => part !== "");
	const method = req.method ?? "GET";
	if (parts[0] !== "v1" || parts[1] !== "conversations" || parts.length > 4) {
		throw new ApiError(404, "invalid_request", `no route ${method} ${url.pathname}`);
	}
	if (parts.length === 2) {
		if (method === "POST") return createConversation(host, req, res);
		if (method === "GET") return listConversations(host, url, res);
		throw invalid(`method ${method} not allowed on ${url.pathname}`);
	}
	let wire: string;
	try {
		wire = decodeURIComponent(parts[2]!);
	} catch {
		throw invalid("conversation id is not URL-encoded");
	}
	const conv = host.conversationId(wire);
	const action = parts[3];
	const route = `${method} ${action ?? ""}`;
	switch (route) {
		case "POST messages":
			return postMessage(host, conv, req, res);
		case "GET entries":
			return getEntries(host, conv, url, res);
		case "GET stream":
			return stream(host, log, conv, url, req, res);
		case "POST abort":
			await readJson(req, false);
			await host.abort(conv);
			return sendJson(res, 200, {});
		case "POST reset": {
			const body = await readJson(req, false);
			onlyKeys(body, ["note"]);
			await host.reset(conv, optionalString(body, "note"));
			return sendJson(res, 200, {});
		}
		case "POST notes": {
			const body = await readJson(req, true);
			onlyKeys(body, ["id", "data"]);
			const id = requiredString(body, "id");
			if (!isObject(body.data)) throw invalid("data must be a JSON object");
			await host.note(conv, id, body.data);
			return sendJson(res, 200, {});
		}
		default:
			throw new ApiError(404, "invalid_request", `no route ${method} ${url.pathname}`);
	}
}

async function createConversation(host: Host, req: IncomingMessage, res: ServerResponse): Promise<void> {
	const body = await readJson(req, false);
	onlyKeys(body, ["key", "settings"]);
	const key = optionalString(body, "key");
	if (key === "") throw invalid("key must not be empty");
	const settings = body.settings ?? {};
	if (!isObject(settings)) throw invalid("settings must be an object");
	onlyKeys(settings, ["model", "modelInfo", "instructions"]);
	const conversation = await host.createConversation(key, {
		...(optionalString(settings, "model") === undefined ? {} : { model: settings.model as string }),
		...(settings.modelInfo === undefined ? {} : { modelInfo: settings.modelInfo }),
		...(optionalString(settings, "instructions") === undefined ? {} : { instructions: settings.instructions as string }),
	});
	sendJson(res, 200, { conversation });
}

function listConversations(host: Host, url: URL, res: ServerResponse): void {
	const active = url.searchParams.get("active");
	if (active !== null && active !== "true" && active !== "false") throw invalid("active must be true or false");
	const key = url.searchParams.get("key");
	const cursor = url.searchParams.get("cursor");
	sendJson(
		res,
		200,
		host.listConversations({
			...(key === null ? {} : { key }),
			active: active === "true",
			...(cursor === null || cursor === "" ? {} : { cursor }),
			limit: parseLimit(url.searchParams.get("limit")),
		}),
	);
}

async function postMessage(host: Host, conv: ConvState, req: IncomingMessage, res: ServerResponse): Promise<void> {
	const body = await readJson(req, true);
	onlyKeys(body, ["requestId", "content", "whenBusy"]);
	const requestId = requiredString(body, "requestId");
	if (typeof body.content !== "string") throw invalid("content must be a string");
	const whenBusy = body.whenBusy ?? "follow_up";
	if (whenBusy !== "follow_up" && whenBusy !== "steer") throw invalid(`whenBusy must be "follow_up" or "steer"`);
	const result = await host.postMessage(conv, requestId, body.content, whenBusy);
	sendJson(res, result.code, { status: result.status });
}

async function getEntries(host: Host, conv: ConvState, url: URL, res: ServerResponse): Promise<void> {
	const after = parseCursor(url.searchParams.get("after"), "after");
	const limit = parseLimit(url.searchParams.get("limit"));
	const page = await host.entriesAfter(conv, after, limit);
	sendJson(res, 200, { entries: page.entries, next: page.more && page.scanned !== undefined ? String(page.scanned) : null });
}

/**
 * SSE: entries after the cursor from storage, then live. The Pi event watch is attached before storage is read, so
 * nothing committed in between is missed; entries are deduplicated by cursor. When the watch falls behind and
 * replaces its batches with a snapshot, storage is read again from the last cursor sent.
 */
async function stream(
	host: Host,
	log: Logger,
	conv: ConvState,
	url: URL,
	req: IncomingMessage,
	res: ServerResponse,
): Promise<void> {
	const header = req.headers["last-event-id"];
	const lastEventId = Array.isArray(header) ? header[0] : header;
	let last = lastEventId !== undefined && lastEventId !== "" ? parseCursor(lastEventId, "Last-Event-ID") : parseCursor(url.searchParams.get("after"), "after");

	const abort = new AbortController();
	const watch = await watchEvents(host.harness, conv.id as never, withAbortSignal(abort.signal, ctx));
	let closed = false;
	const close = () => {
		if (closed) return;
		closed = true;
		clearInterval(keepalive);
		abort.abort();
		void watch.stop();
	};
	res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
	res.flushHeaders();
	const keepalive = setInterval(() => {
		if (!closed) res.write(": keepalive\n\n");
	}, KEEPALIVE_MS);
	req.on("close", close);
	res.on("close", close);

	const write = async (text: string): Promise<void> => {
		if (closed) return;
		if (!res.write(text)) {
			await new Promise<void>((resolve) => {
				const done = () => {
					res.off("drain", done);
					res.off("close", done);
					resolve();
				};
				res.on("drain", done);
				res.on("close", done);
			});
		}
	};
	const sendEntry = async (entry: WireEntry) => write(`event: entry\nid: ${entry.cursor}\ndata: ${JSON.stringify(entry)}\n\n`);
	const sendRun = async (run: string, status: string) => write(`event: run\ndata: ${JSON.stringify({ run, status })}\n\n`);
	const sendLive = async (type: "text" | "thinking" | "tool_output", delta: string) => {
		const run = conv.current?.run;
		if (run === undefined || delta === "") return;
		await write(`event: live\ndata: ${JSON.stringify({ run, type, delta })}\n\n`);
	};
	const replay = async () => {
		while (!closed) {
			const page = await host.entriesAfter(conv, last, 200);
			for (const entry of page.entries) await sendEntry(entry);
			if (page.scanned !== undefined) last = Math.max(last, page.scanned);
			if (!page.more) return;
		}
	};
	let announced: string | null = conv.current?.run ?? null;

	try {
		await replay();
	} catch (error) {
		close();
		res.end();
		throw error;
	}
	const handleEvent = async (event: AgentEvent, next: AgentEvent | undefined) => {
		switch (event.type) {
			case "message_start": {
				// The first partial of a streamed answer; a stored entry's start is directly followed by its end.
				if (next?.type === "message_end" || event.message.role !== "assistant") return;
				for (const block of event.message.content) {
					if (block.type === "text") await sendLive("text", block.text);
					else if (block.type === "thinking") await sendLive("thinking", block.thinking);
				}
				return;
			}
			case "snapshot": {
				await replay();
				const now = conv.current?.run ?? null;
				if (announced !== null && announced !== now) {
					await sendRun(announced, host.tracker.statusOf(conv, announced) ?? "completed");
				}
				if (now !== null && now !== announced) await sendRun(now, "running");
				announced = now;
				return;
			}
			case "message_end":
			case "entry_appended": {
				if (event.entry.id <= last) return;
				last = event.entry.id;
				const wire = host.toWire(conv, event.entry);
				if (wire !== undefined) await sendEntry(wire);
				return;
			}
			case "message_update":
				for (const change of event.changes) {
					if (change.type === "text_delta") await sendLive("text", change.delta);
					else if (change.type === "thinking_delta") await sendLive("thinking", change.delta);
					else if (change.type === "text_start" && change.block.type === "text") await sendLive("text", change.block.text);
					else if (change.type === "thinking_start" && change.block.type === "thinking") {
						await sendLive("thinking", change.block.thinking);
					}
				}
				return;
			case "tool_execution_update": {
				const output = event.output;
				if (output === undefined) return;
				if ("set" in output) await sendLive("tool_output", output.set);
				else if (output.append !== undefined) await sendLive("tool_output", output.append);
				return;
			}
			case "run_start": {
				const run = host.tracker.runIdOf(event.inputs[0] as number);
				announced = run;
				await sendRun(run, "running");
				return;
			}
			case "run_end": {
				const run = host.tracker.runIdOf(event.inputs[0] as number);
				if (announced === run) announced = null;
				await sendRun(run, host.tracker.statusOf(conv, run) ?? "completed");
				return;
			}
			default:
				return;
		}
	};
	watch.start(async (events) => {
		for (const [index, event] of events.entries()) {
			if (closed) return;
			try {
				await handleEvent(event, events[index + 1]);
			} catch (error) {
				log.warn(`stream c_${conv.id}: ${String(error)}`);
				close();
				res.end();
				return;
			}
		}
	});
	await watch.closed;
	close();
	if (!res.writableEnded) res.end();
}
