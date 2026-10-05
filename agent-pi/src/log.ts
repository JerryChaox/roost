// Logs go to stderr only: stdout carries the control channel and nothing else.
export type Logger = { info(message: string): void; warn(message: string): void; error(message: string): void };

export function createLogger(): Logger {
	const write = (level: string, message: string) => {
		process.stderr.write(`${new Date().toISOString()} ${level} ${message}\n`);
	};
	return {
		info: (message) => write("info", message),
		warn: (message) => write("warn", message),
		error: (message) => write("error", message),
	};
}

/** Route console output of libraries to stderr, so nothing but control messages reaches stdout. */
export function divertConsole(): void {
	const toStderr = (...args: unknown[]) => console.error(...args);
	console.log = toStderr;
	console.info = toStderr;
	console.debug = toStderr;
	console.warn = toStderr;
}
