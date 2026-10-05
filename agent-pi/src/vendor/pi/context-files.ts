// Vendored from Pi (https://github.com/earendil-works/pi), commit 2003871, MIT License (see ./LICENSE).
// Source: packages/coding-agent/src/core/resource-loader.ts (loadProjectContextFiles, loadContextFileFromDir).
// `loadProjectContextFiles` is exported only from @earendil-works/pi-coding-agent, which roost does not depend on.
// Changes from the source: no global agent directory (~/.pi/agent), no linked-worktree shadowing, and read failures
// are reported through the given logger instead of chalk on the console.
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { ContextFile } from "./system-prompt.ts";

const CANDIDATES = ["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"];

function stripBom(text: string): string {
	return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

function loadContextFileFromDir(dir: string, warn: (message: string) => void): ContextFile | null {
	for (const filename of CANDIDATES) {
		const filePath = join(dir, filename);
		if (existsSync(filePath)) {
			try {
				if (!statSync(filePath).isFile()) continue;
				return { path: filePath, content: stripBom(readFileSync(filePath, "utf-8")) };
			} catch (error) {
				warn(`could not read ${filePath}: ${String(error)}`);
			}
		}
	}
	return null;
}

/** The context file of `cwd` and of each of its ancestors, outermost first. */
export function loadProjectContextFiles(cwd: string, warn: (message: string) => void): ContextFile[] {
	const files: ContextFile[] = [];
	const seen = new Set<string>();
	let current = resolve(cwd);
	while (true) {
		const file = loadContextFileFromDir(current, warn);
		if (file && !seen.has(file.path)) {
			files.unshift(file);
			seen.add(file.path);
		}
		const parent = dirname(current);
		if (parent === current) break;
		current = parent;
	}
	return files;
}
