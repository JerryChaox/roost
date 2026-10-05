// E2 orchestrator: kill the host mid-run (model request / unsafe tool / safe tool), then reopen and observe.
import { spawn } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";

const env = { PATH: process.env.PATH, HOME: process.env.HOME, EXEC_LOG: "work/e2/exec.log" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function run(args, { until, label } = {}) {
	return new Promise((resolve) => {
		const child = spawn(process.execPath, ["e2-host.ts", ...args], { env, stdio: ["ignore", "pipe", "pipe"] });
		let buf = "";
		let fired = false;
		const onData = (d) => {
			buf += d;
			for (const line of String(d).split("\n")) if (line.trim()) console.log(`  [${label}] ${line}`);
			if (until && !fired && until(buf)) {
				fired = true;
				resolve({ child, buf });
			}
		};
		child.stdout.on("data", onData);
		child.stderr.on("data", onData);
		child.on("exit", (code, signal) => {
			console.log(`  [${label}] exit code=${code} signal=${signal}`);
			if (!fired) resolve({ child, buf, code, signal });
		});
	});
}

const fakeLogLength = () => readFileSync("work/fake.log", "utf8").length;
const fakeLogSince = (n) => readFileSync("work/fake.log", "utf8").slice(n);

async function scenario(name, text, trigger) {
	console.log(`\n=== ${name}`);
	const db = `work/e2/${name}/agent.sqlite`;
	rmSync(`work/e2/${name}`, { recursive: true, force: true });
	const mark = fakeLogLength();
	let triggered;
	if (trigger === "hang") {
		const started = run(["start", db, text], { label: "start", until: (b) => b.includes("SUBMITTED") });
		const { child } = await started;
		while (!fakeLogSince(mark).includes("hanging mid-stream")) await sleep(50);
		await sleep(700); // let a throttled partial commit (100 ms)
		triggered = child;
	} else {
		const { child } = await run(["start", db, text], { label: "start", until: (b) => b.includes("TOOL_STARTED") });
		await sleep(1200); // let some tool output commit
		triggered = child;
	}
	console.log("  >>> SIGKILL");
	triggered.kill("SIGKILL");
	await new Promise((r) => triggered.on("exit", r));
	const mark2 = fakeLogLength();
	await run(["peek", db], { label: "peek" });
	console.log(`  model requests during peek (no resume): ${fakeLogSince(mark2).split("\n").filter((l) => l.includes('"api"')).length}`);
	const mark3 = fakeLogLength();
	await run(["recover", db], { label: "recover" });
	console.log("  fake-llm requests during recover:");
	for (const l of fakeLogSince(mark3).split("\n").filter(Boolean)) console.log(`    ${l}`);
}

rmSync("work/e2/exec.log", { force: true });
await scenario("model-request", "please answer [hangonce]", "hang");
await scenario("unsafe-tool", 'run it [tool slow_tool {"seconds":4}]', "tool");
await scenario("safe-tool", 'run it safely [tool safe_tool {"seconds":4}]', "tool");
console.log("\n=== tool executions (EXEC_LOG)");
console.log(readFileSync("work/e2/exec.log", "utf8"));
