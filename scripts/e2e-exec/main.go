// Command e2e-exec runs one command in a sandbox through roost's E2B provider
// (its Exec), for scripts/e2e-m1.sh. The E2B API key is read from
// E2B_API_KEY and never printed.
//
//	e2e-exec <sandbox-id> <user> <argv...>
//
// It prints the command's stdout and stderr and exits with its exit code.
package main

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/provider/e2b"
)

func main() {
	if len(os.Args) < 4 {
		fmt.Fprintln(os.Stderr, "usage: e2e-exec <sandbox-id> <user> <argv...>")
		os.Exit(2)
	}
	apiURL := os.Getenv("E2B_API_URL")
	if apiURL == "" {
		apiURL = "https://api.e2b.app"
	}
	key := os.Getenv("E2B_API_KEY")
	if key == "" {
		fmt.Fprintln(os.Stderr, "E2B_API_KEY is not set")
		os.Exit(2)
	}
	p, err := e2b.New(apiURL, key)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	res, err := p.Exec(ctx, os.Args[1], provider.Process{User: os.Args[2], Argv: os.Args[3:]})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Stdout.Write(res.Stdout)
	os.Stderr.Write(res.Stderr)
	os.Exit(res.ExitCode)
}
