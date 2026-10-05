// Command roost is roost's one binary. This version has one subcommand:
// roost serve, the control plane.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/JerryChaox/roost/internal/serve"
)

const usage = `Usage: roost <command> [flags]

Commands:
  serve   run the control plane: the HTTP API and the reconcilers
`

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return 2
	}
	switch args[0] {
	case "serve":
		return cmdServe(args[1:])
	case "-h", "--help", "help":
		fmt.Print(usage)
		return 0
	}
	fmt.Fprintf(os.Stderr, "roost: unknown command %q\n\n%s", args[0], usage)
	return 2
}

func cmdServe(args []string) int {
	fs := flag.NewFlagSet("roost serve", flag.ContinueOnError)
	configPath := fs.String("config", "roost.yaml", "`path` of roost.yaml")
	sandboxDir := fs.String("sandbox-dir", "dist/sandbox", "`directory` staged by scripts/build-sandbox.sh: the driver and the agent host for the template")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(os.Stderr, "roost serve: unexpected arguments %q\n", fs.Args())
		return 2
	}
	logger := log.New(os.Stderr, "roost serve: ", log.LstdFlags|log.Lmsgprefix)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := serve.Run(ctx, serve.Options{ConfigPath: *configPath, SandboxDir: *sandboxDir, Log: logger}); err != nil {
		logger.Print(err)
		return 1
	}
	return 0
}
