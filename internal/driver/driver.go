//go:build unix

// Package driver implements roost-driver, roost's process in every sandbox
// (docs/specs/driver-protocol.md). It gates every request with the execution
// grant's tokens, forwards the conversation interface to the agent host over
// a Unix socket without parsing it, holds the lock on agent storage, and
// supervises the agent host over its stdio control channel.
package driver

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	rpcTimeout        = 5 * time.Second
	initializeTimeout = 60 * time.Second
	shutdownWait      = 10 * time.Second
	backoffStart      = 200 * time.Millisecond
	backoffMax        = 5 * time.Second
	// A host that ran at least this long restarts after backoffStart again.
	stableRun = 10 * time.Second
)

// Config is the driver's non-secret configuration, baked into the template.
// Everything secret arrives in the grant on Stdin.
type Config struct {
	Listen   string   // TCP address of the driver protocol
	Storage  string   // agent storage directory; the lock is <Storage>/lock
	Socket   string   // Unix socket the agent host serves the conversation interface on
	HostCmd  []string // argv of the agent host
	HostUser string   // user to run the agent host as, when the driver is root
	Version  string   // build version for the fingerprint; empty uses the module version

	Stdin   io.ReadCloser // where the grant is read from; closed after reading
	Log     *log.Logger   // driver log
	HostLog *log.Logger   // the agent host's stderr, line by line
	Capture Capture       // receives snapshot notifications; nil in M1
}

// Run serves the driver protocol until ctx ends, then stops the agent host
// and returns. It returns an error for a bad grant, a storage lock held by
// someone else, or a listener that fails.
func Run(ctx context.Context, cfg Config) error {
	if len(cfg.HostCmd) == 0 {
		return errors.New("no agent host command")
	}
	logger := cfg.Log
	if logger == nil {
		logger = log.New(os.Stderr, "roost-driver: ", log.LstdFlags|log.Lmsgprefix)
	}
	hostLog := cfg.HostLog
	if hostLog == nil {
		hostLog = log.New(os.Stderr, "agent-host: ", log.LstdFlags|log.Lmsgprefix)
	}
	capture := cfg.Capture
	if capture == nil {
		capture = noCapture{}
	}

	fp, err := selfFingerprint(cfg.Version)
	if err != nil {
		return err
	}
	cred, env, err := hostIdentity(cfg.HostUser, logger)
	if err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	ln, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	transport := newHostTransport(cfg.Socket)
	srv := &server{log: logger, fp: fp, proxy: newHostProxy(transport, logger)}
	// No write timeout: conversation streams stay open as long as they run.
	httpSrv := &http.Server{Handler: srv, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 2 * time.Minute, ErrorLog: logger}
	serveErr := make(chan error, 1)
	go func() { serveErr <- httpSrv.Serve(ln) }()
	defer httpSrv.Close()
	logger.Printf("driver %s (sha256 %s) listening on %s", fp.Version, fp.SHA256, ln.Addr())

	grant, err := readGrant(ctx, cfg.Stdin)
	if err != nil {
		return err
	}
	if ctx.Err() != nil {
		return nil
	}

	if err := ensureDir(cfg.Storage, cred); err != nil {
		return fmt.Errorf("agent storage: %w", err)
	}
	lock, err := lockStorage(cfg.Storage)
	if err != nil {
		return err
	}
	defer lock.Close()

	if grant == nil {
		logger.Printf("no execution grant on stdin; answering 503 not_ready")
		select {
		case <-ctx.Done():
			return nil
		case err := <-serveErr:
			return fmt.Errorf("serve: %w", err)
		}
	}

	if err := ensureDir(filepath.Dir(cfg.Socket), cred); err != nil {
		return fmt.Errorf("socket directory: %w", err)
	}
	host := &supervisor{
		log:     logger,
		hostLog: hostLog,
		argv:    cfg.HostCmd,
		env:     env,
		cred:    cred,
		socket:  cfg.Socket,
		init: initializeParams{
			Protocol: 1,
			Storage:  cfg.Storage,
			Socket:   cfg.Socket,
			Models:   grant.Models,
			Agent:    grant.Agent,
		},
		capture: capture,
		// Keep-alive connections to a host that is gone would fail the next
		// request that reuses them.
		onDown: transport.CloseIdleConnections,
	}
	srv.active.Store(&active{start: grant.Start, gate: newGate(grant.DriverToken, grant.BackupToken), host: host})
	logger.Printf("execution grant %s", grant.Start)

	hostDone := make(chan struct{})
	go func() {
		host.run(ctx)
		close(hostDone)
	}()
	select {
	case <-hostDone:
		return nil
	case err := <-serveErr:
		cancel()
		<-hostDone
		return fmt.Errorf("serve: %w", err)
	}
}

// readGrant reads the grant from r and closes r. It gives up when ctx ends.
func readGrant(ctx context.Context, r io.ReadCloser) (*Grant, error) {
	if r == nil {
		return nil, nil
	}
	type result struct {
		g   *Grant
		err error
	}
	ch := make(chan result, 1)
	go func() {
		g, err := ReadGrant(r)
		r.Close()
		ch <- result{g, err}
	}()
	select {
	case res := <-ch:
		return res.g, res.err
	case <-ctx.Done():
		r.Close()
		return nil, nil
	}
}

// hostIdentity returns the credential and environment the agent host runs
// with. Only a root driver can switch users; otherwise the host runs as the
// driver's user.
func hostIdentity(name string, logger *log.Logger) (*syscall.Credential, []string, error) {
	env := os.Environ()
	if name == "" {
		return nil, env, nil
	}
	if os.Geteuid() != 0 {
		logger.Printf("not running as root: the agent host runs as the current user, not %q", name)
		return nil, env, nil
	}
	u, err := user.Lookup(name)
	if err != nil {
		return nil, nil, fmt.Errorf("host user: %w", err)
	}
	uid, err := strconv.ParseUint(u.Uid, 10, 32)
	if err != nil {
		return nil, nil, fmt.Errorf("host user %s: uid %q: %w", name, u.Uid, err)
	}
	gid, err := strconv.ParseUint(u.Gid, 10, 32)
	if err != nil {
		return nil, nil, fmt.Errorf("host user %s: gid %q: %w", name, u.Gid, err)
	}
	gids, err := u.GroupIds()
	if err != nil {
		return nil, nil, fmt.Errorf("host user %s: groups: %w", name, err)
	}
	groups := make([]uint32, 0, len(gids))
	for _, g := range gids {
		n, err := strconv.ParseUint(g, 10, 32)
		if err != nil {
			return nil, nil, fmt.Errorf("host user %s: group %q: %w", name, g, err)
		}
		groups = append(groups, uint32(n))
	}
	env = setEnv(env, "HOME", u.HomeDir)
	env = setEnv(env, "USER", u.Username)
	env = setEnv(env, "LOGNAME", u.Username)
	return &syscall.Credential{Uid: uint32(uid), Gid: uint32(gid), Groups: groups}, env, nil
}

func setEnv(env []string, key, value string) []string {
	out := env[:0:0]
	for _, kv := range env {
		if !strings.HasPrefix(kv, key+"=") {
			out = append(out, kv)
		}
	}
	return append(out, key+"="+value)
}

// ensureDir creates dir if it is missing, owned by the agent host's user when
// the host runs as another user. An existing directory is left as it is.
func ensureDir(dir string, cred *syscall.Credential) error {
	if _, err := os.Stat(dir); err == nil {
		return nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dir), 0o755); err != nil {
		return err
	}
	if err := os.Mkdir(dir, 0o700); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	if cred != nil {
		return os.Chown(dir, int(cred.Uid), int(cred.Gid))
	}
	return nil
}
