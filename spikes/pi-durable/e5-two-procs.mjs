// E5 (Q12): two processes open the same storage while a run is in flight.
import { spawn } from "node:child_process";
import { rmSync, writeFileSync } from "node:fs";

const db = "work/e5/two/agent.sqlite";
rmSync("work/e5/two", { recursive: true, force: true });
const env = { PATH: process.env.PATH, HOME: process.env.HOME, EXEC_LOG: "work/e5/two-exec.log" };
rmSync(env.EXEC_LOG, { force: true });

writeFileSync(
	"work/e5/two-host.ts",
	`import { LiveDoc } from "@earendil-works/pi-durable";
import { ANTHROPIC, allEntries, brief, ctx, openHost, out } from "../../lib.ts";
const [role, db] = process.argv.slice(2);
const { harness } = await openHost(db);
out(role + " OPENED", "");
const ins = await harness.inspect(ctx);
for (const t of ins.tasks) out(role + " task", { id: t.record.id, kind: t.record.kind, status: t.record.state.status, phase: t.record.state.checkpoint?.phase });
if (role === "A") {
  const root = await harness.root(ctx, { agent: { model: ANTHROPIC } });
  const s = await root.submit({ type: "input", content: 'x [tool slow_tool {"seconds":6}]', requestId: "two-1" }, ctx);
  const settled = await s.wait(ctx);
  out("A SETTLED", settled);
} else {
  harness.resume();
  await harness.waitForIdle(ctx);
  out("B IDLE", "");
}
const root = await harness.root(ctx);
for (const e of await allEntries(root)) console.log(role + "    " + brief(e));
await harness.close(ctx);
out(role + " CLOSED", "");
`,
);

const start = (role) => {
	const child = spawn(process.execPath, ["work/e5/two-host.ts", role, db], { env, stdio: ["ignore", "pipe", "pipe"] });
	const lines = [];
	let reports = 0;
	const onData = (d) => {
		for (const line of String(d).split("\n")) {
			if (!line.trim()) continue;
			if (line.includes("REPORT")) {
				if (reports++ < 3) console.log(`[${role}] ${line}`);
				continue;
			}
			console.log(`[${role}] ${line}`);
		}
		lines.push(String(d));
	};
	child.stdout.on("data", onData);
	child.stderr.on("data", onData);
	const exited = new Promise((r) => child.on("exit", (code) => r(code)));
	return { child, lines, exited, reports: () => reports };
};

const a = start("A");
while (!a.lines.join("").includes("TOOL_STARTED")) await new Promise((r) => setTimeout(r, 50));
await new Promise((r) => setTimeout(r, 1000));
console.log(">>> starting B on the same storage while A's tool runs");
const b = start("B");
console.log("B exit code", await b.exited);
await new Promise((r) => setTimeout(r, 8000)); // A's tool would have finished by now
console.log(`A still running after B closed + 8 s: ${a.child.exitCode === null}; A REPORT lines so far: ${a.reports()}`);
a.child.kill("SIGKILL");
console.log("A killed:", await a.exited);
