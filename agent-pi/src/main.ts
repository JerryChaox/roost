#!/usr/bin/env node
// roost-agent-pi: the agent host. JSON-RPC 2.0 control channel on stdin/stdout (driver-protocol §5), one message per
// line; the conversation interface over HTTP on a Unix socket (§4). stdout carries only control messages.
import { existsSync, unlinkSync } from "node:fs";
import type { Server } from "node:http";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { InvalidParams, parseInitialize } from "./config.ts";
import { Host, PI_DURABLE_VERSION } from "./host.ts";
import { createConversationServer, listen } from "./http.ts";
import { StorageLocked } from "./lock.ts";
import { createLogger, divertConsole } from "./log.ts";

divertConsole();
const log = createLogger();

function parseArgs(argv: readonly string[]): { workdir: string } {
	let workdir = "/workspace";
	for (let index = 0; index < argv.length; index++) {
		const arg = argv[index]!;
		if (arg === "--workdir" && argv[index + 1] !== undefined) workdir = argv[++index]!;
		else if (arg.startsWith("--workdir=")) workdir = arg.slice("--workdir=".length);
		else {
			log.error(`unknown argument ${JSON.stringify(arg)}; usage: roost-agent-pi [--workdir <dir>]`);
			process.exit(2);
		}
	}
	workdir = resolve(workdir);
	if (!existsSync(workdir)) log.warn(`workdir ${workdir} does not exist; tools run there`);
	return { workdir };
}

const { workdir } = parseArgs(process.argv.slice(2));

type Id = string | number | null;
const ERR = { parse: -32700, request: -32600, method: -32601, params: -32602, internal: -32603, server: -32000 };

function send(message: object): Promise<void> {
	return new Promise((resolve) => {
		process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`, () => resolve());
	});
}
const reply = (id: Id, result: unknown) => send({ id, result });
const fail = (id: Id, code: number, message: string) => send({ id, error: { code, message } });

let host: Host | undefined;
let server: Server | undefined;
let socketPath: string | undefined;
let initializing = false;
let stopping: Promise<void> | undefined;

function notify(method: string, params: unknown): void {
	void send({ method, params });
}

/** Stop admitting, close the harness (Pi aborts in-flight work; it resumes on the next start), release socket and lock. */
function stop(): Promise<void> {
	stopping ??= (async () => {
		if (host !== undefined) {
			host.draining = true;
			host.closing = true;
		}
		if (server !== undefined) {
			const closed = new Promise<void>((resolve) => server!.close(() => resolve()));
			server.closeAllConnections();
			await closed;
		}
		if (host !== undefined) await host.close();
		if (socketPath !== undefined) {
			try {
				unlinkSync(socketPath);
			} catch {}
		}
	})();
	return stopping;
}

async function initialize(id: Id, params: unknown): Promise<void> {
	if (host !== undefined || initializing) return fail(id, ERR.request, "already initialized");
	initializing = true;
	try {
		const config = parseInitialize(params);
		host = await Host.open(config, workdir, log, { snapshot: (notice) => notify("snapshot", notice) });
		host.resume();
		server = createConversationServer(host, log);
		await listen(server, config.socket);
		socketPath = config.socket;
		log.info(`listening on ${config.socket}; storage ${config.storage}; workdir ${workdir}`);
		await reply(id, {
			agent: "pi-durable",
			version: PI_DURABLE_VERSION,
			durability: "step",
			capabilities: ["steer", "reset_note"],
		});
	} catch (error) {
		const code = error instanceof InvalidParams ? ERR.params : error instanceof StorageLocked ? ERR.server : ERR.internal;
		const message = error instanceof Error ? error.message : String(error);
		log.error(`initialize failed: ${error instanceof Error ? (error.stack ?? message) : message}`);
		await fail(id, code, message);
		await stop().catch(() => {});
		process.exit(1);
	}
}

async function dispatch(message: Record<string, unknown>): Promise<void> {
	const id = (message.id ?? null) as Id;
	const isRequest = "id" in message;
	const method = message.method;
	if (typeof method !== "string") {
		if (isRequest) await fail(id, ERR.request, "method must be a string");
		return;
	}
	if (method === "initialize") return initialize(id, message.params);
	if (host === undefined) {
		if (isRequest) await fail(id, ERR.request, "not initialized");
		return;
	}
	switch (method) {
		case "quiesce":
			host.draining = true;
			return reply(id, { running: host.running() });
		case "resume":
			host.draining = false;
			return reply(id, { running: host.running() });
		case "state":
			return reply(id, host.state());
		case "shutdown":
			await stop();
			await reply(id, {});
			process.exit(0);
			return;
		default:
			if (isRequest) await fail(id, ERR.method, `unknown method ${method}`);
	}
}

const input = createInterface({ input: process.stdin, crlfDelay: Number.POSITIVE_INFINITY });
input.on("line", (line) => {
	if (line.trim() === "") return;
	let message: unknown;
	try {
		message = JSON.parse(line);
	} catch {
		void fail(null, ERR.parse, "parse error");
		return;
	}
	if (typeof message !== "object" || message === null || Array.isArray(message)) {
		void fail(null, ERR.request, "a message must be a JSON object");
		return;
	}
	dispatch(message as Record<string, unknown>).catch((error: unknown) => {
		log.error(`control: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
		const id = ((message as Record<string, unknown>).id ?? null) as Id;
		void fail(id, ERR.internal, String(error));
	});
});

// The driver owns this process: when the control channel closes, or on SIGTERM, stop cleanly.
const exitAfterStop = (reason: string) => {
	log.info(`stopping: ${reason}`);
	stop()
		.catch((error: unknown) => log.error(`stop: ${String(error)}`))
		.finally(() => process.exit(0));
};
input.on("close", () => exitAfterStop("control channel closed"));
process.on("SIGTERM", () => exitAfterStop("SIGTERM"));
process.on("SIGINT", () => exitAfterStop("SIGINT"));
