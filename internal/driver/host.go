//go:build unix

package driver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log"
	"os"
	"os/exec"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// HostInfo is what the agent host reports in its initialize result.
type HostInfo struct {
	Agent        string   `json:"agent"`
	Version      string   `json:"version"`
	Durability   string   `json:"durability"`
	Capabilities []string `json:"capabilities"`
}

// initializeParams is the initialize request (driver-protocol §5). M1 never
// fills a sandbox from a snapshot, so restoredFrom is always omitted.
type initializeParams struct {
	Protocol int             `json:"protocol"`
	Storage  string          `json:"storage"`
	Socket   string          `json:"socket"`
	Models   json.RawMessage `json:"models,omitempty"`
	Agent    json.RawMessage `json:"agent,omitempty"`
}

// readyHost is an initialized agent host the driver can talk to.
type readyHost struct {
	rpc  *rpcConn
	info HostInfo
}

// supervisor runs one agent host at a time, restarting it with backoff until
// its context ends.
type supervisor struct {
	log     *log.Logger
	hostLog *log.Logger
	argv    []string
	env     []string
	cred    *syscall.Credential
	socket  string
	init    initializeParams
	capture Capture
	onDown  func() // called each time the host stops being ready

	ready atomic.Pointer[readyHost]
}

// current returns the initialized host, or nil while it is starting,
// restarting or stopping.
func (s *supervisor) current() *readyHost { return s.ready.Load() }

func (s *supervisor) run(ctx context.Context) {
	delay := backoffStart
	for {
		started := time.Now()
		err := s.runOnce(ctx)
		if ctx.Err() != nil {
			return
		}
		if time.Since(started) >= stableRun {
			delay = backoffStart
		}
		s.log.Printf("agent host stopped: %v; restarting in %s", err, delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay = min(delay*2, backoffMax)
	}
}

// runOnce starts the host, initializes it and returns when it is gone.
func (s *supervisor) runOnce(ctx context.Context) error {
	removeStaleSocket(s.socket)
	p, err := s.spawn()
	if err != nil {
		return fmt.Errorf("start: %w", err)
	}
	defer p.cleanup()
	s.log.Printf("agent host started (pid %d)", p.pid)

	initCtx, cancel := context.WithTimeout(ctx, initializeTimeout)
	res, err := p.rpc.call(initCtx, "initialize", s.init)
	cancel()
	if err != nil {
		if ctx.Err() != nil {
			s.stop(p)
			return ctx.Err()
		}
		return err
	}
	var info HostInfo
	if err := json.Unmarshal(res, &info); err != nil {
		return fmt.Errorf("initialize: result: %w", err)
	}
	if info.Capabilities == nil {
		info.Capabilities = []string{}
	}
	s.ready.Store(&readyHost{rpc: p.rpc, info: info})
	s.log.Printf("agent host ready: %s %s", info.Agent, info.Version)

	select {
	case <-p.exited:
		s.down()
		return p.exitErr()
	case <-p.rpcClosed:
		// A host without its control channel cannot be managed.
		s.down()
		return errors.New("control channel closed")
	case <-ctx.Done():
		s.down()
		s.stop(p)
		return ctx.Err()
	}
}

func (s *supervisor) down() {
	s.ready.Store(nil)
	if s.onDown != nil {
		s.onDown()
	}
}

// stop asks the host to shut down, waits up to shutdownWait for it to exit,
// then kills it.
func (s *supervisor) stop(p *hostProc) {
	deadline := time.Now().Add(shutdownWait)
	callCtx, cancel := context.WithTimeout(context.Background(), min(rpcTimeout, shutdownWait))
	_, err := p.rpc.call(callCtx, "shutdown", nil)
	cancel()
	if err != nil {
		s.log.Printf("agent host shutdown: %v", err)
	}
	select {
	case <-p.exited:
		s.log.Printf("agent host stopped")
	case <-time.After(time.Until(deadline)):
		s.log.Printf("agent host did not stop within %s; killing it", shutdownWait)
	}
}

// hostProc is one running agent host.
type hostProc struct {
	pid       int
	rpc       *rpcConn
	exited    chan struct{} // closed once the process has been reaped
	waitErr   error
	rpcClosed chan struct{} // closed when the host's stdout ends

	stdin   *os.File
	stdout  *os.File
	stderr  *os.File
	readers sync.WaitGroup
}

func (p *hostProc) exitErr() error {
	if p.waitErr != nil {
		return p.waitErr
	}
	return errors.New("exited")
}

// cleanup kills whatever is left of the host's process group, reaps the host
// and releases the pipes.
func (p *hostProc) cleanup() {
	_ = syscall.Kill(-p.pid, syscall.SIGKILL)
	<-p.exited
	p.rpc.close(errors.New("agent host exited"))
	p.stdin.Close()
	// Something that escaped the process group may still hold the host's
	// stdout or stderr open; do not wait for it for long.
	done := make(chan struct{})
	go func() { p.readers.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
	}
	p.stdout.Close()
	p.stderr.Close()
	<-done
}

func (s *supervisor) spawn() (*hostProc, error) {
	inR, inW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	outR, outW, err := os.Pipe()
	if err != nil {
		inR.Close()
		inW.Close()
		return nil, err
	}
	errR, errW, err := os.Pipe()
	if err != nil {
		inR.Close()
		inW.Close()
		outR.Close()
		outW.Close()
		return nil, err
	}

	cmd := exec.Command(s.argv[0], s.argv[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = inR, outW, errW
	cmd.Env = s.env
	cmd.SysProcAttr = hostSysProcAttr(s.cred)

	p := &hostProc{
		exited:    make(chan struct{}),
		rpcClosed: make(chan struct{}),
		stdin:     inW,
		stdout:    outR,
		stderr:    errR,
	}
	started := make(chan error, 1)
	go func() {
		// On Linux the parent-death signal follows the thread that started
		// the child, so that thread must outlive it.
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		if err := cmd.Start(); err != nil {
			started <- err
			return
		}
		started <- nil
		p.waitErr = cmd.Wait()
		close(p.exited)
	}()
	err = <-started
	// The child holds its own copies of these ends.
	inR.Close()
	outW.Close()
	errW.Close()
	if err != nil {
		inW.Close()
		outR.Close()
		errR.Close()
		return nil, err
	}
	p.pid = cmd.Process.Pid

	p.rpc = newRPCConn(inW, s.log, s.notification)
	p.readers.Add(2)
	go func() {
		defer p.readers.Done()
		p.rpc.readLoop(outR)
		close(p.rpcClosed)
	}()
	go func() {
		defer p.readers.Done()
		_ = readLines(errR, 64<<10, func(line []byte, tooLong bool) {
			if tooLong {
				s.hostLog.Printf("%s [truncated]", line)
			} else {
				s.hostLog.Printf("%s", line)
			}
		})
	}()
	go func() {
		<-p.exited
		p.rpc.close(errors.New("agent host exited"))
	}()
	return p, nil
}

// notification handles a notification from the host.
func (s *supervisor) notification(method string, params json.RawMessage) {
	switch method {
	case "snapshot":
		var n SnapshotNotice
		if err := json.Unmarshal(params, &n); err != nil {
			s.log.Printf("control channel: skipped a malformed snapshot notification: %v", err)
			return
		}
		s.capture.HostSnapshot(n)
	default:
		s.log.Printf("control channel: skipped unknown notification %q", method)
	}
}

// removeStaleSocket removes a socket left by an earlier host, so a new host
// can listen and nothing reaches the old one.
func removeStaleSocket(path string) {
	fi, err := os.Lstat(path)
	if err != nil || fi.Mode()&fs.ModeSocket == 0 {
		return
	}
	_ = os.Remove(path)
}
