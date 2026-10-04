// Test harness: the fake model server, the built host as a child process (we play the driver), and HTTP/SSE over its
// Unix socket. Tests run against dist/ (npm test builds first).
import { type ChildProcess, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const MAIN = join(ROOT, "dist", "main.js");

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function until<T>(what: string, probe: () => Promise<T | undefined | false>, timeoutMs = 20_000): Promise<T> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const value = await probe();
		if (value !== undefined && value !== false) return value;
		await sleep(50);
	}
	throw new Error(`timed out waiting for ${what}`);
}

/** The fake model server; `requests` holds its request log (model, tools, thinking, ...). */
export type Fake = { url: string; requests: any[]; stop(): void };

export async function startFake(): Promise<Fake> {
	const child = spawn(process.execPath, [join(ROOT, "test", "fake-llm.mjs"), "0"], { stdio: ["ignore", "pipe", "inherit"] });
	const lines = createInterface({ input: child.stdout! });
	const port = await new Promise<number>((resolve) => lines.once("line", (line) => resolve(JSON.parse(line).listening)));
	const requests: any[] = [];
	lines.on("line", (line) => {
		const record = JSON.parse(line);
		if (record.model !== undefined) requests.push(record);
	});
	return { url: `http://127.0.0.1:${port}`, requests, stop: () => child.kill("SIGKILL") };
}

/** A temporary directory with a short path (Unix socket paths are limited to ~104 bytes). */
export function tempDir(t: { after(fn: () => void): void }): { dir: string; storage: string; socket: string; workdir: string } {
	const dir = mkdtempSync(join(tmpdir(), "rap-"));
	t.after(() => rmSync(dir, { recursive: true, force: true }));
	const workdir = join(dir, "ws");
	mkdirSync(workdir);
	return { dir, storage: join(dir, "agent"), socket: join(dir, "a.sock"), workdir };
}

export function initParams(fake: Fake, paths: { storage: string; socket: string }, agent: Record<string, unknown> = {}) {
	return {
		protocol: 1,
		storage: paths.storage,
		socket: paths.socket,
		models: { baseUrls: { anthropic: fake.url }, credential: "placeholder-key" },
		agent: { model: "anthropic/claude-haiku-4-5", thinking: "off", systemPrompt: { base: "pi", append: null }, ...agent },
	};
}

type RpcMessage = { id?: number; method?: string; params?: any; result?: any; error?: { code: number; message: string } };

export class HostProcess {
	readonly child: ChildProcess;
	readonly socket: string;
	readonly notifications: RpcMessage[] = [];
	readonly exited: Promise<number | null>;
	stderr = "";
	#nextId = 1;
	#pending = new Map<number, (message: RpcMessage) => void>();

	constructor(workdir: string, socket: string) {
		this.socket = socket;
		this.child = spawn(process.execPath, [MAIN, "--workdir", workdir], { stdio: ["pipe", "pipe", "pipe"] });
		this.child.stderr!.on("data", (chunk) => {
			this.stderr += chunk;
			if (process.env.HOST_LOG) process.stderr.write(chunk);
		});
		createInterface({ input: this.child.stdout! }).on("line", (line) => {
			const message = JSON.parse(line) as RpcMessage;
			if (message.id !== undefined && this.#pending.has(message.id)) {
				this.#pending.get(message.id)!(message);
				this.#pending.delete(message.id);
			} else this.notifications.push(message);
		});
		this.exited = new Promise((resolve) => this.child.on("exit", (code) => resolve(code)));
	}

	rpc(method: string, params?: unknown): Promise<RpcMessage> {
		const id = this.#nextId++;
		const reply = new Promise<RpcMessage>((resolve) => this.#pending.set(id, resolve));
		this.child.stdin!.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, ...(params === undefined ? {} : { params }) })}\n`);
		return reply;
	}

	async shutdown(): Promise<void> {
		if (this.child.exitCode !== null) return;
		await this.rpc("shutdown");
		await this.exited;
	}

	kill(): Promise<number | null> {
		this.child.kill("SIGKILL");
		return this.exited;
	}

	request(method: string, path: string, body?: unknown): Promise<{ status: number; body: any }> {
		return new Promise((resolve, reject) => {
			const req = http.request({ socketPath: this.socket, method, path, headers: { "content-type": "application/json" } }, (res) => {
				let text = "";
				res.on("data", (chunk) => (text += chunk));
				res.on("end", () => resolve({ status: res.statusCode!, body: text === "" ? null : JSON.parse(text) }));
			});
			req.on("error", reject);
			if (body !== undefined) req.write(JSON.stringify(body));
			req.end();
		});
	}

	/** Every entry of a conversation, oldest first. */
	async allEntries(conversation: string): Promise<any[]> {
		const all: any[] = [];
		let after = "";
		for (;;) {
			const page = await this.request("GET", `/v1/conversations/${conversation}/entries?limit=500${after ? `&after=${after}` : ""}`);
			all.push(...page.body.entries);
			if (page.body.next === null) return all;
			after = page.body.next;
		}
	}

	/** Wait until the conversation has no run in progress and nothing queued. */
	async idle(conversation: string): Promise<void> {
		await until(`${conversation} idle`, async () => {
			const state = await this.rpc("state");
			return !state.result.runs.some((run: { conversation: string }) => run.conversation === conversation);
		});
	}
}

/** Start a host and initialize it; returns the host and the initialize reply. */
export async function startHost(
	paths: { workdir: string; socket: string },
	params: unknown,
): Promise<{ host: HostProcess; reply: RpcMessage }> {
	const host = new HostProcess(paths.workdir, paths.socket);
	const reply = await host.rpc("initialize", params);
	return { host, reply };
}

export type SseEvent = { event: string; id?: string; data: any; raw: string };

/** An SSE connection that collects events. */
export class SseClient {
	readonly events: SseEvent[] = [];
	readonly #req: http.ClientRequest;
	#buffer = "";

	constructor(socket: string, path: string, headers: Record<string, string> = {}) {
		this.#req = http.request({ socketPath: socket, method: "GET", path, headers }, (res) => {
			res.setEncoding("utf8");
			res.on("data", (chunk: string) => {
				this.#buffer += chunk;
				let end: number;
				while ((end = this.#buffer.indexOf("\n\n")) >= 0) {
					const block = this.#buffer.slice(0, end);
					this.#buffer = this.#buffer.slice(end + 2);
					this.#parse(block);
				}
			});
		});
		this.#req.on("error", () => {});
		this.#req.end();
	}

	#parse(block: string): void {
		const lines = block.split("\n").filter((line) => !line.startsWith(":"));
		if (lines.length === 0) return;
		let event = "message";
		let id: string | undefined;
		let data = "";
		for (const line of lines) {
			if (line.startsWith("event: ")) event = line.slice(7);
			else if (line.startsWith("id: ")) id = line.slice(4);
			else if (line.startsWith("data: ")) data += line.slice(6);
		}
		this.events.push({ event, ...(id === undefined ? {} : { id }), data: JSON.parse(data), raw: block });
	}

	close(): void {
		this.#req.destroy();
	}
}

export { existsSync, join };
