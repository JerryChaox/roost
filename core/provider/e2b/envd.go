package e2b

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"

	"connectrpc.com/connect"

	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/provider/e2b/internal/envd/process"
	"github.com/JerryChaox/roost/core/provider/e2b/internal/envd/process/processconnect"
)

// maxExecOutput bounds what Exec keeps of a process's stdout and stderr.
const maxExecOutput = 1 << 20

// envd returns a client for the sandbox's envd process API, and a function
// that sets envd's auth headers on a request: the access token, and the user
// as HTTP Basic auth with an empty password.
func (p *Provider) envd(ctx context.Context, id, user string) (processconnect.ProcessClient, func(http.Header), error) {
	token, err := p.envdToken(ctx, id)
	if err != nil {
		return nil, nil, err
	}
	if user == "" {
		user = defaultUser
	}
	base := fmt.Sprintf("https://%d-%s.%s", envdPort, id, p.domain)
	c := processconnect.NewProcessClient(p.stream, base, connect.WithProtoJSON())
	auth := "Basic " + base64.StdEncoding.EncodeToString([]byte(user+":"))
	return c, func(h http.Header) {
		h.Set("X-Access-Token", token)
		h.Set("Authorization", auth)
	}, nil
}

func startRequest(pr provider.Process, stdin bool) (*connect.Request[process.StartRequest], error) {
	if len(pr.Argv) == 0 || pr.Argv[0] == "" {
		return nil, errors.New("e2b exec: empty argv")
	}
	req := connect.NewRequest(&process.StartRequest{
		Process: &process.ProcessConfig{Cmd: pr.Argv[0], Args: pr.Argv[1:], Envs: pr.Env},
		Stdin:   &stdin,
	})
	// envd sends keepalives on long streams; without them an idle stream
	// would be cut by proxies in between.
	req.Header().Set("Keepalive-Ping-Interval", "50")
	return req, nil
}

func (p *Provider) Exec(ctx context.Context, id string, pr provider.Process) (provider.ExecResult, error) {
	c, auth, err := p.envd(ctx, id, pr.User)
	if err != nil {
		return provider.ExecResult{}, err
	}
	req, err := startRequest(pr, false)
	if err != nil {
		return provider.ExecResult{}, err
	}
	auth(req.Header())
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.Start(ctx, req)
	if err != nil {
		return provider.ExecResult{}, fmt.Errorf("e2b exec %s: %w", id, err)
	}
	defer stream.Close()
	var stdout, stderr bytes.Buffer
	for stream.Receive() {
		ev := stream.Msg().GetEvent()
		if d := ev.GetData(); d != nil {
			appendLimited(&stdout, d.GetStdout())
			appendLimited(&stderr, d.GetStderr())
		}
		if e := ev.GetEnd(); e != nil {
			res := provider.ExecResult{ExitCode: int(e.GetExitCode()), Stdout: stdout.Bytes(), Stderr: stderr.Bytes()}
			if !e.GetExited() && e.GetError() != "" {
				return res, fmt.Errorf("e2b exec %s: %s", id, e.GetError())
			}
			return res, nil
		}
	}
	if err := stream.Err(); err != nil {
		return provider.ExecResult{}, fmt.Errorf("e2b exec %s: %w", id, err)
	}
	return provider.ExecResult{}, fmt.Errorf("e2b exec %s: stream ended without the process's end", id)
}

func appendLimited(b *bytes.Buffer, p []byte) {
	if room := maxExecOutput - b.Len(); room > 0 {
		if len(p) > room {
			p = p[:room]
		}
		b.Write(p)
	}
}

// Spawn starts the process with standard input enabled, waits for envd's
// start event, writes p.Stdin through SendInput (addressed by pid) and then
// closes the Start stream. envd keeps the process running after its Start
// stream closes, so it outlives this call.
func (p *Provider) Spawn(ctx context.Context, id string, pr provider.Process) (int, error) {
	c, auth, err := p.envd(ctx, id, pr.User)
	if err != nil {
		return 0, err
	}
	req, err := startRequest(pr, true)
	if err != nil {
		return 0, err
	}
	auth(req.Header())
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel() // closes the Start stream; the process keeps running
	stream, err := c.Start(streamCtx, req)
	if err != nil {
		return 0, fmt.Errorf("e2b spawn %s: %w", id, err)
	}
	defer stream.Close()
	var pid uint32
	for pid == 0 && stream.Receive() {
		ev := stream.Msg().GetEvent()
		if s := ev.GetStart(); s != nil {
			pid = s.GetPid()
		}
		if e := ev.GetEnd(); e != nil {
			return 0, fmt.Errorf("e2b spawn %s: process ended at once: exit %d %s", id, e.GetExitCode(), e.GetError())
		}
	}
	if pid == 0 {
		if err := stream.Err(); err != nil {
			return 0, fmt.Errorf("e2b spawn %s: %w", id, err)
		}
		return 0, fmt.Errorf("e2b spawn %s: no start event", id)
	}
	if len(pr.Stdin) > 0 {
		in := connect.NewRequest(&process.SendInputRequest{
			Process: &process.ProcessSelector{Selector: &process.ProcessSelector_Pid{Pid: pid}},
			Input:   &process.ProcessInput{Input: &process.ProcessInput_Stdin{Stdin: pr.Stdin}},
		})
		auth(in.Header())
		if _, err := c.SendInput(ctx, in); err != nil {
			return int(pid), fmt.Errorf("e2b spawn %s: send input to pid %d: %w", id, pid, err)
		}
	}
	return int(pid), nil
}
