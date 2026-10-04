// Command roost-driver is roost's process in every sandbox: it gates requests
// with the execution grant's tokens, forwards conversation requests to the
// agent host, holds the lock on agent storage and supervises the agent host.
// See docs/specs/driver-protocol.md.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/JerryChaox/roost/internal/driver"
)

// version is set at build time with -ldflags "-X main.version=...". When it
// is empty the driver reports the module version Go stamps into the binary.
var version string

const usage = `Usage: roost-driver [flags]

roost-driver reads one execution grant, a JSON object, from standard input and
then closes it. Nothing secret is taken from arguments or the environment:

  {"protocol": 1, "start": "<grant id>", "driverToken": "...", "backupToken": "...",
   "models": {...}, "agent": {...}}

"models" and "agent" are passed to the agent host unchanged in initialize.
Without a grant (standard input ends first) the driver answers every request
with 503 not_ready.

Flags:
`

func main() {
	os.Exit(run())
}

func run() int {
	logger := log.New(os.Stderr, "roost-driver: ", log.LstdFlags|log.Lmsgprefix)

	fs := flag.NewFlagSet("roost-driver", flag.ContinueOnError)
	fs.Usage = func() {
		fmt.Fprint(fs.Output(), usage)
		fs.PrintDefaults()
	}
	listen := fs.String("listen", "0.0.0.0:7070", "TCP `address` the driver protocol is served on")
	storage := fs.String("storage", "/var/lib/roost/agent", "agent storage `directory`; the driver locks <directory>/lock")
	socket := fs.String("socket", "/run/roost/agent.sock", "Unix socket `path` the agent host serves the conversation interface on")
	hostCmd := fs.String("host-cmd", "", "argv of the agent host as a JSON array of strings (required),\ne.g. '[\"node\", \"/opt/roost/agent-pi/main.js\"]'")
	hostUser := fs.String("host-user", "", "`user` to run the agent host as; ignored unless the driver runs as root\n(empty: the driver's own user)")
	if err := fs.Parse(os.Args[1:]); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(fs.Output(), "unexpected arguments: %q\n", fs.Args())
		fs.Usage()
		return 2
	}
	var argv []string
	if err := json.Unmarshal([]byte(*hostCmd), &argv); err != nil || len(argv) == 0 || argv[0] == "" {
		fmt.Fprintln(fs.Output(), "--host-cmd must be a non-empty JSON array of strings")
		fs.Usage()
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	err := driver.Run(ctx, driver.Config{
		Listen:   *listen,
		Storage:  *storage,
		Socket:   *socket,
		HostCmd:  argv,
		HostUser: *hostUser,
		Version:  version,
		Stdin:    os.Stdin,
		Log:      logger,
	})
	if err != nil {
		logger.Print(err)
		return 1
	}
	return 0
}
