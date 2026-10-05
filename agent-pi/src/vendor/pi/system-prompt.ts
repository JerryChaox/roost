// Vendored from Pi (https://github.com/earendil-works/pi), commit 2003871, MIT License (see ./LICENSE).
// Sources:
//   packages/coding-agent/src/core/system-prompt.ts            (buildSystemPromptSections, buildRules, renderProjectContext)
//   packages/coding-agent/src/core/tools/{read,bash,edit,write}.ts (the *ToolSystemPromptContribution constants)
//   packages/coding-agent/src/experimental/durable/prompt.ts   (section keys and the per-request assembly)
// These are not exported from any npm package roost depends on. Changes from the source:
//   - the `docs` section is left out: it points at Pi's own README, docs and examples inside the coding-agent package,
//     which roost does not install;
//   - skills, custom prompts, forced prompts, appended prompts and custom sections are left out (not in roost M1).

export const TOOL_CONTRIBUTIONS: Readonly<Record<string, { snippet: string; guidelines: readonly string[] }>> = {
	read: {
		snippet: "Read file contents",
		guidelines: ["Use read to examine files instead of cat or sed."],
	},
	bash: {
		snippet: "Execute bash commands (ls, grep, find, etc.)",
		guidelines: ["You can inspect PI_* environment variables for current model and session details."],
	},
	edit: {
		snippet: "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
		guidelines: [
			"Use edit for precise changes (edits[].oldText must match exactly)",
			"When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
			"Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
			"Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
		],
	},
	write: {
		snippet: "Create or overwrite files",
		guidelines: ["Use write only for new files or complete rewrites."],
	},
};

/** Pi's section order (prompt.ts), without `docs` and `skills`. */
export const SECTION_KEYS = ["preamble", "tools", "rules", "project_context", "cwd"] as const;

export type ContextFile = { path: string; content: string };

function renderProjectContext(contextFiles: readonly ContextFile[]): string {
	return [
		"Project-specific instructions and guidelines:",
		...contextFiles.map(
			({ path, content }) => `<project_instructions path="${path}">\n${content}\n</project_instructions>`,
		),
	].join("\n\n");
}

function buildRules(selectedTools: readonly string[], toolGuidelines: Record<string, readonly string[]>): string {
	const rules: string[] = [];
	const seen = new Set<string>();
	const addRule = (rule: string): void => {
		const normalized = rule.trim();
		if (!normalized || seen.has(normalized)) return;
		seen.add(normalized);
		rules.push(normalized);
	};

	const hasBash = selectedTools.includes("bash");
	const hasPowerShell = selectedTools.includes("powershell");
	const hasGrep = selectedTools.includes("grep");
	const hasFind = selectedTools.includes("find");
	const hasLs = selectedTools.includes("ls");

	if ((hasBash || hasPowerShell) && !hasGrep && !hasFind && !hasLs) {
		if (hasBash && hasPowerShell) {
			addRule("Use bash or PowerShell for file operations like listing, searching, and finding files");
		} else if (hasPowerShell) {
			addRule("Use PowerShell for file operations like listing, searching, and finding files");
		} else {
			addRule("Use bash for file operations like ls, rg, find");
		}
	}

	for (const name of selectedTools) {
		for (const rule of toolGuidelines[name] ?? []) addRule(rule);
	}
	addRule("Be concise in your responses");
	addRule("Show file paths clearly when working with files");
	return rules.map((rule) => `- ${rule}`).join("\n");
}

/** Pi's ordered prompt sections for the request's tools and directory; every section but `preamble` is tagged. */
export function buildSystemPromptSections(input: {
	cwd: string;
	selectedTools: readonly string[];
	contextFiles: readonly ContextFile[];
}): Record<string, string> {
	const { cwd, selectedTools, contextFiles } = input;
	const toolSnippets: Record<string, string> = {};
	const toolGuidelines: Record<string, readonly string[]> = {};
	for (const name of selectedTools) {
		const contribution = TOOL_CONTRIBUTIONS[name];
		if (contribution === undefined) continue;
		toolSnippets[name] = contribution.snippet;
		toolGuidelines[name] = contribution.guidelines;
	}

	const promptSections: Record<string, string> = {};
	promptSections.preamble =
		"You are an expert coding assistant operating inside pi, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files.";
	const visibleTools = selectedTools.filter((name) => !!toolSnippets[name]);
	const tools =
		visibleTools.length > 0 ? visibleTools.map((name) => `- ${name}: ${toolSnippets[name]}`).join("\n") : "(none)";
	promptSections.tools = `${tools}\n\nIn addition to the tools above, you may have access to other custom tools depending on the project.`;
	promptSections.rules = buildRules(selectedTools, toolGuidelines);

	if (contextFiles.length > 0) promptSections.project_context = renderProjectContext(contextFiles);
	promptSections.cwd = cwd.replace(/\\/g, "/");

	const sections: Record<string, string> = { preamble: promptSections.preamble };
	for (const [name, content] of Object.entries(promptSections)) {
		if (name !== "preamble") sections[name] = `<${name}>\n${content}\n</${name}>`;
	}
	return sections;
}
