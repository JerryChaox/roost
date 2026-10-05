// Package sandbox defines what runs in every workspace's sandbox: the one
// fixed template of this version, and how the control plane starts the
// driver in it (driver-protocol §2).
package sandbox

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/JerryChaox/roost/core/provider"
)

// Paths inside the sandbox.
const (
	DriverPort    = 7070
	DriverBin     = "/usr/local/bin/roost-driver"
	AgentDir      = "/opt/roost/agent-pi"
	AgentMain     = AgentDir + "/dist/main.js"
	AgentStorage  = "/var/lib/roost/agent"
	AgentSocket   = "/run/roost/agent.sock"
	Workdir       = "/workspace"
	DriverLogFile = "/var/log/roost/driver.log"
)

// Files the staged sandbox directory must hold (scripts/build-sandbox.sh).
var stagedFiles = []string{
	"roost-driver",
	"agent-pi/package.json",
	"agent-pi/package-lock.json",
	"agent-pi/dist/main.js",
}

// Template returns the template of this version, built from the staged
// directory dir, for sandboxes whose default user is user. Its name is
// roost- plus the SHA-256 of the template definition and every staged file,
// so a changed driver, agent host or definition is a new template.
func Template(dir, user string) (provider.TemplateSpec, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return provider.TemplateSpec{}, err
	}
	for _, f := range stagedFiles {
		if _, err := os.Stat(filepath.Join(abs, f)); err != nil {
			return provider.TemplateSpec{}, fmt.Errorf("sandbox directory %s: %w (run scripts/build-sandbox.sh)", dir, err)
		}
	}
	spec := provider.TemplateSpec{
		Image:    "node:24-bookworm-slim",
		CPU:      2,
		MemoryMB: 2048,
		Dir:      abs,
		Steps: []provider.Step{
			{User: "root", Run: "apt-get update && apt-get install -y --no-install-recommends ca-certificates procps && rm -rf /var/lib/apt/lists/*"},
			{User: "root", Copy: &provider.Copy{Src: "roost-driver", Dest: DriverBin}},
			{User: "root", Copy: &provider.Copy{Src: "agent-pi", Dest: AgentDir}},
			{User: "root", Run: strings.Join([]string{
				"chmod 0755 " + DriverBin,
				"cd " + AgentDir,
				"npm ci --omit=dev --no-audit --no-fund",
				"mkdir -p " + Workdir,
				"chown " + user + ": " + Workdir,
			}, " && ")},
		},
	}
	digest, err := digest(spec)
	if err != nil {
		return provider.TemplateSpec{}, err
	}
	spec.Name = "roost-" + digest
	return spec, nil
}

// digest hashes the definition (everything but the local directory's path)
// and, in path order, every file under the directory: its path, mode and
// content.
func digest(spec provider.TemplateSpec) (string, error) {
	h := sha256.New()
	def := spec
	def.Dir = ""
	b, err := json.Marshal(def)
	if err != nil {
		return "", err
	}
	h.Write(b)
	var paths []string
	err = filepath.WalkDir(spec.Dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		paths = append(paths, p)
		return nil
	})
	if err != nil {
		return "", err
	}
	sort.Strings(paths)
	for _, p := range paths {
		info, err := os.Lstat(p)
		if err != nil {
			return "", err
		}
		rel, _ := filepath.Rel(spec.Dir, p)
		fmt.Fprintf(h, "\x00%s\x00%o\x00", filepath.ToSlash(rel), info.Mode())
		switch {
		case info.Mode().IsRegular():
			f, err := os.Open(p)
			if err != nil {
				return "", err
			}
			_, err = io.Copy(h, f)
			f.Close()
			if err != nil {
				return "", err
			}
		case info.Mode()&fs.ModeSymlink != 0:
			t, err := os.Readlink(p)
			if err != nil {
				return "", err
			}
			h.Write([]byte(t))
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// agentPattern matches the agent host's command line in pgrep/pkill -f. The
// bracket keeps the pattern from matching the shell running the script.
const agentPattern = "[/]opt/roost/agent-pi/dist/main.js"

// stopScript stops every driver and agent host in the sandbox and confirms
// they are gone: TERM (the driver shuts its host down), KILL after 5s, and a
// non-zero exit if anything is left after 10s.
const stopScript = `pat='` + agentPattern + `'
pkill -TERM -x roost-driver; pkill -TERM -f "$pat"
i=0
while pgrep -x roost-driver >/dev/null || pgrep -f "$pat" >/dev/null; do
  i=$((i+1))
  if [ "$i" -eq 50 ]; then pkill -KILL -x roost-driver; pkill -KILL -f "$pat"; fi
  if [ "$i" -ge 100 ]; then echo "roost-driver or the agent host is still running" >&2; exit 1; fi
  sleep 0.1
done
exit 0`

// StartDriver starts the driver for a grant (driver-protocol §2): it stops
// every running driver and agent host and confirms they are gone, then
// starts roost-driver as root, detached, with grantJSON written to its
// standard input. Nothing secret is in its arguments or environment. The
// driver's output goes to DriverLogFile.
func StartDriver(ctx context.Context, p provider.Provider, sandboxID string, grantJSON []byte) error {
	res, err := p.Exec(ctx, sandboxID, provider.Process{Argv: []string{"/bin/sh", "-c", stopScript}, User: "root"})
	if err != nil {
		return fmt.Errorf("stop the running driver: %w", err)
	}
	if res.ExitCode != 0 {
		return fmt.Errorf("stop the running driver: exit %d: %s", res.ExitCode, strings.TrimSpace(string(res.Stderr)))
	}
	hostCmd, err := json.Marshal([]string{"node", "--use-system-ca", AgentMain, "--workdir", Workdir})
	if err != nil {
		return err
	}
	argv := []string{DriverBin,
		"--listen", fmt.Sprintf("0.0.0.0:%d", DriverPort),
		"--storage", AgentStorage,
		"--socket", AgentSocket,
		"--host-user", p.DefaultUser(),
		"--host-cmd", string(hostCmd),
	}
	quoted := make([]string, len(argv))
	for i, a := range argv {
		quoted[i] = shellQuote(a)
	}
	// The log is created private (umask in a subshell only: the directories
	// the driver creates must stay readable by the agent host's user).
	logFile := shellQuote(DriverLogFile)
	script := fmt.Sprintf("mkdir -p %s && (umask 077 && : >>%s) && exec %s >>%s 2>&1",
		shellQuote(filepath.Dir(DriverLogFile)), logFile, strings.Join(quoted, " "), logFile)
	if len(grantJSON) == 0 {
		return errors.New("start the driver: no grant")
	}
	if _, err := p.Spawn(ctx, sandboxID, provider.Process{Argv: []string{"/bin/sh", "-c", script}, User: "root", Stdin: grantJSON}); err != nil {
		return fmt.Errorf("start the driver: %w", err)
	}
	return nil
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }
