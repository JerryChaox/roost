// Command spike exercises the E2B Cloud REST API and envd through Go clients
// generated from E2B's own specs (internal/api, internal/envd), logging every
// HTTP exchange with secrets redacted. It is a verification spike, not
// production code: one subcommand per provider operation roost relies on.
//
// The E2B API key is read from E2B_API_KEY and never printed.
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"connectrpc.com/connect"

	"roost.spike/e2b/internal/api"
	"roost.spike/e2b/internal/envd/process"
	"roost.spike/e2b/internal/envd/process/processconnect"
)

const envdPort = 49983

var (
	apiURL   = getenv("E2B_API_URL", "https://api.e2b.app")
	domain   = getenv("E2B_DOMAIN", "e2b.app")
	stateDir = os.Getenv("SPIKE_STATE_DIR") // sandbox-scoped tokens, kept outside the repo
	quiet    = os.Getenv("SPIKE_QUIET") == "1"
	t0       = time.Now()
)

func getenv(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cmd, args := os.Args[1], os.Args[2:]
	ctx := context.Background()
	var err error
	switch cmd {
	case "list":
		err = cmdList(ctx, args)
	case "get":
		err = cmdGet(ctx, args)
	case "poll-list":
		err = cmdPollList(ctx, args)
	case "create":
		err = cmdCreate(ctx, args)
	case "pause":
		err = cmdPause(ctx, args)
	case "connect":
		err = cmdConnect(ctx, args)
	case "kill":
		err = cmdKill(ctx, args)
	case "network":
		err = cmdNetwork(ctx, args)
	case "timeout":
		err = cmdTimeout(ctx, args)
	case "exec":
		err = cmdExec(ctx, args)
	case "ps":
		err = cmdPs(ctx, args)
	case "signal":
		err = cmdSignal(ctx, args)
	case "http":
		err = cmdHTTP(ctx, args)
	case "build":
		err = cmdBuild(ctx, args)
	case "alias":
		err = cmdAlias(ctx, args)
	case "templates":
		err = cmdTemplates(ctx, args)
	case "template-delete":
		err = cmdTemplateDelete(ctx, args)
	case "raw":
		err = cmdRaw(ctx, args)
	case "digest":
		sum := sha256.Sum256([]byte(strings.Join(args, "\n")))
		fmt.Println("roost-" + hex.EncodeToString(sum[:]))
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "ERROR:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: spike <list|get|create|pause|connect|kill|network|timeout|exec|ps|signal|http|build|alias|templates|template-delete|raw|digest> ...")
	os.Exit(2)
}

// ---------------------------------------------------------------- logging

var redactHeaders = map[string]bool{
	"x-api-key":                true,
	"x-access-token":           true,
	"e2b-traffic-access-token": true,
}

// Also covers tokens echoed back by the in-sandbox test server: E2B's proxy
// forwards e2b-traffic-access-token to the sandbox service.
var redactJSONKeys = regexp.MustCompile(`(?i)("(?:envdAccessToken|trafficAccessToken|accessToken|password|value|e2b-traffic-access-token|x-access-token)"\s*:\s*)"([^"]*)"`)

func redactBody(b []byte) string {
	s := redactJSONKeys.ReplaceAllStringFunc(string(b), func(m string) string {
		sub := redactJSONKeys.FindStringSubmatch(m)
		return sub[1] + fmt.Sprintf(`"<redacted len=%d>"`, len(sub[2]))
	})
	if k := os.Getenv("E2B_API_KEY"); k != "" {
		s = strings.ReplaceAll(s, k, "<redacted-api-key>")
	}
	return s
}

func redactHeader(k, v string) string {
	lk := strings.ToLower(k)
	if redactHeaders[lk] {
		return fmt.Sprintf("<redacted len=%d>", len(v))
	}
	if lk == "authorization" {
		if strings.HasPrefix(v, "Basic ") {
			dec, _ := base64.StdEncoding.DecodeString(strings.TrimPrefix(v, "Basic "))
			return fmt.Sprintf("%s   (= base64(%q))", v, string(dec))
		}
		return fmt.Sprintf("<redacted len=%d>", len(v))
	}
	return v
}

func ts() string { return fmt.Sprintf("[+%7.3fs]", time.Since(t0).Seconds()) }

type logRT struct{ next http.RoundTripper }

func (l logRT) RoundTrip(req *http.Request) (*http.Response, error) {
	start := time.Now()
	var reqBody []byte
	if req.Body != nil {
		reqBody, _ = io.ReadAll(req.Body)
		req.Body = io.NopCloser(bytes.NewReader(reqBody))
	}
	if !quiet {
		fmt.Printf("%s >>> %s %s\n", ts(), req.Method, req.URL.String())
		keys := make([]string, 0, len(req.Header))
		for k := range req.Header {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			if k == "User-Agent" {
				continue
			}
			for _, v := range req.Header[k] {
				fmt.Printf("    %s: %s\n", k, redactHeader(k, v))
			}
		}
		if len(reqBody) > 0 {
			if bytes.HasPrefix(reqBody, []byte{0}) && len(reqBody) > 5 { // connect envelope
				fmt.Printf("    body (connect envelope, flags=%d): %s\n", reqBody[0], redactBody(reqBody[5:]))
			} else {
				fmt.Printf("    body: %s\n", redactBody(reqBody))
			}
		}
	}
	resp, err := l.next.RoundTrip(req)
	el := time.Since(start)
	if err != nil {
		fmt.Printf("%s <<< transport error after %s: %v\n", ts(), el.Round(time.Millisecond), err)
		return resp, err
	}
	fmt.Printf("%s <<< %s (%s) %s %s\n", ts(), resp.Status, el.Round(time.Millisecond), req.Method, req.URL.Path)
	if !quiet {
		for _, k := range []string{"Content-Type", "X-Next-Token", "X-Total-Running", "X-Ratelimit-Limit", "X-Ratelimit-Remaining", "Retry-After", "Connect-Content-Encoding"} {
			if v := resp.Header.Get(k); v != "" {
				fmt.Printf("    %s: %s\n", k, v)
			}
		}
	}
	ct := resp.Header.Get("Content-Type")
	streaming := strings.HasPrefix(ct, "application/connect+") || strings.HasPrefix(ct, "text/event-stream")
	if !streaming && resp.Body != nil {
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		resp.Body = io.NopCloser(bytes.NewReader(b))
		if len(b) > 0 && !quiet {
			shown := b
			if resp.Header.Get("Content-Encoding") == "gzip" {
				if zr, err := gzip.NewReader(bytes.NewReader(b)); err == nil {
					if d, err := io.ReadAll(zr); err == nil {
						shown = d
					}
				}
			}
			s := redactBody(shown)
			if len(s) > 6000 {
				s = s[:6000] + "...<truncated>"
			}
			fmt.Printf("    body: %s\n", s)
		}
	}
	return resp, nil
}

var httpClient = &http.Client{Transport: logRT{next: http.DefaultTransport}}

func apiClient() *api.ClientWithResponses {
	key := os.Getenv("E2B_API_KEY")
	if key == "" {
		fmt.Fprintln(os.Stderr, "E2B_API_KEY is not set")
		os.Exit(2)
	}
	c, err := api.NewClientWithResponses(apiURL, api.WithHTTPClient(httpClient),
		api.WithRequestEditorFn(func(_ context.Context, r *http.Request) error {
			r.Header.Set("X-API-Key", key)
			return nil
		}))
	if err != nil {
		panic(err)
	}
	return c
}

// ---------------------------------------------------------------- state

type sbxState struct {
	SandboxID          string  `json:"sandboxID"`
	EnvdAccessToken    string  `json:"envdAccessToken"`
	TrafficAccessToken *string `json:"trafficAccessToken"`
	Domain             *string `json:"domain"`
	EnvdVersion        string  `json:"envdVersion"`
}

func saveState(b []byte) {
	if stateDir == "" {
		return
	}
	var s sbxState
	if json.Unmarshal(b, &s) != nil || s.SandboxID == "" {
		return
	}
	old := loadState(s.SandboxID)
	if s.EnvdAccessToken == "" {
		s.EnvdAccessToken = old.EnvdAccessToken
	}
	if s.TrafficAccessToken == nil {
		s.TrafficAccessToken = old.TrafficAccessToken
	}
	_ = os.MkdirAll(stateDir, 0o700)
	out, _ := json.Marshal(s)
	_ = os.WriteFile(filepath.Join(stateDir, s.SandboxID+".json"), out, 0o600)
}

func loadState(id string) sbxState {
	var s sbxState
	if stateDir == "" {
		return s
	}
	b, err := os.ReadFile(filepath.Join(stateDir, id+".json"))
	if err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

// ---------------------------------------------------------------- REST

type kv map[string]string

func (m kv) String() string { return fmt.Sprint(map[string]string(m)) }
func (m kv) Set(s string) error {
	k, v, ok := strings.Cut(s, "=")
	if !ok {
		return fmt.Errorf("want k=v, got %q", s)
	}
	m[k] = v
	return nil
}

// encodeMetadata builds the `metadata` query value: "k=v&k2=v2" with each key
// and value URL-encoded (the spec's wording); the generated client then
// form-encodes the whole value once more as a query parameter.
func encodeMetadata(m kv) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(m[k]))
	}
	return strings.Join(parts, "&")
}

func cmdList(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("list", flag.ExitOnError)
	meta := kv{}
	fs.Var(meta, "meta", "metadata filter k=v (repeatable)")
	state := fs.String("state", "", "comma-separated states (running,paused)")
	limit := fs.Int("limit", 0, "page size")
	next := fs.String("next", "", "next token")
	all := fs.Bool("all", false, "follow X-Next-Token")
	fs.Parse(args)
	p := &api.GetV2SandboxesParams{}
	if len(meta) > 0 {
		m := encodeMetadata(meta)
		p.Metadata = &m
	}
	if *state != "" {
		var ss []api.SandboxState
		for _, s := range strings.Split(*state, ",") {
			ss = append(ss, api.SandboxState(s))
		}
		p.State = &ss
	}
	if *limit > 0 {
		l := api.PaginationLimit(*limit)
		p.Limit = &l
	}
	if *next != "" {
		p.NextToken = next
	}
	total := 0
	for page := 1; ; page++ {
		r, err := apiClient().GetV2SandboxesWithResponse(ctx, p)
		if err != nil {
			return err
		}
		if r.JSON200 == nil {
			return fmt.Errorf("list: %s", r.Status())
		}
		for _, s := range *r.JSON200 {
			md, _ := json.Marshal(s.Metadata)
			fmt.Printf("  page %d: %s state=%s template=%s startedAt=%s endAt=%s metadata=%s\n", page, s.SandboxID, s.State, s.TemplateID, s.StartedAt.Format(time.RFC3339), s.EndAt.Format(time.RFC3339), md)
		}
		total += len(*r.JSON200)
		nt := r.HTTPResponse.Header.Get("X-Next-Token")
		if !*all || nt == "" {
			break
		}
		p.NextToken = &nt
	}
	fmt.Printf("RESULT list count=%d\n", total)
	return nil
}

// cmdPollList polls GET /v2/sandboxes (filtered by metadata, both states)
// until sandbox `id` is listed in `want` state ("absent" = not listed), and
// reports how long that took — the list's eventual-consistency window.
func cmdPollList(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("poll-list", flag.ExitOnError)
	meta := kv{}
	fs.Var(meta, "meta", "metadata filter k=v")
	id := fs.String("id", "", "sandbox id")
	want := fs.String("want", "running", "running|paused|absent")
	every := fs.Duration("every", 250*time.Millisecond, "poll interval")
	fs.Parse(args)
	m := encodeMetadata(meta)
	ss := []api.SandboxState{"running", "paused"}
	p := &api.GetV2SandboxesParams{Metadata: &m, State: &ss}
	start := time.Now()
	prevQuiet := quiet
	quiet = true
	defer func() { quiet = prevQuiet }()
	for n := 1; ; n++ {
		r, err := apiClient().GetV2SandboxesWithResponse(ctx, p)
		if err != nil {
			return err
		}
		got := "absent"
		if r.JSON200 != nil {
			for _, s := range *r.JSON200 {
				if s.SandboxID == *id {
					got = string(s.State)
				}
			}
		}
		if got == *want {
			fmt.Printf("RESULT poll-list id=%s state=%s reached after %s (%d polls)\n", *id, got, time.Since(start).Round(time.Millisecond), n)
			return nil
		}
		if time.Since(start) > 3*time.Minute {
			fmt.Printf("RESULT poll-list id=%s still %s after %s\n", *id, got, time.Since(start).Round(time.Millisecond))
			return nil
		}
		time.Sleep(*every)
	}
}

func cmdGet(ctx context.Context, args []string) error {
	r, err := apiClient().GetSandboxesSandboxIDWithResponse(ctx, args[0])
	if err != nil {
		return err
	}
	if r.JSON200 != nil {
		saveState(r.Body)
		fmt.Printf("RESULT get state=%s endAt=%s\n", r.JSON200.State, r.JSON200.EndAt.Format(time.RFC3339))
	} else {
		fmt.Printf("RESULT get status=%d\n", r.StatusCode())
	}
	return nil
}

func cmdCreate(ctx context.Context, args []string) error {
	// args[0] is the raw JSON body: the exact bytes sent.
	body := args[0]
	start := time.Now()
	r, err := apiClient().PostV2SandboxesWithBodyWithResponse(ctx, "application/json", strings.NewReader(body))
	if err != nil {
		return err
	}
	el := time.Since(start)
	if r.JSON201 == nil {
		fmt.Printf("RESULT create status=%d elapsed=%s\n", r.StatusCode(), el.Round(time.Millisecond))
		return nil
	}
	saveState(r.Body)
	fmt.Printf("RESULT create sandboxID=%s status=%d elapsed=%s\n", r.JSON201.SandboxID, r.StatusCode(), el.Round(time.Millisecond))
	return nil
}

func cmdPause(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("pause", flag.ExitOnError)
	mem := fs.String("memory", "", "true|false (omit for default)")
	fs.Parse(args)
	body := api.SandboxPauseRequest{}
	if *mem != "" {
		b := *mem == "true"
		body.Memory = &b
	}
	start := time.Now()
	r, err := apiClient().PostSandboxesSandboxIDPauseWithResponse(ctx, fs.Arg(0), body)
	if err != nil {
		return err
	}
	fmt.Printf("RESULT pause status=%d elapsed=%s\n", r.StatusCode(), time.Since(start).Round(time.Millisecond))
	return nil
}

func cmdConnect(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("connect", flag.ExitOnError)
	mem := fs.String("memory", "", "true|false (omit for default)")
	timeout := fs.Int("timeout", 0, "timeout seconds")
	fs.Parse(args)
	body := api.ConnectSandboxV2{}
	if *mem != "" {
		b := *mem == "true"
		body.Memory = &b
	}
	if *timeout > 0 {
		t := int32(*timeout)
		body.Timeout = &t
	}
	start := time.Now()
	r, err := apiClient().PostV2SandboxesSandboxIDConnectWithResponse(ctx, fs.Arg(0), body)
	if err != nil {
		return err
	}
	saveState(r.Body)
	fmt.Printf("RESULT connect status=%d elapsed=%s\n", r.StatusCode(), time.Since(start).Round(time.Millisecond))
	return nil
}

func cmdKill(ctx context.Context, args []string) error {
	for _, id := range args {
		start := time.Now()
		r, err := apiClient().DeleteSandboxesSandboxIDWithResponse(ctx, id)
		if err != nil {
			return err
		}
		fmt.Printf("RESULT kill %s status=%d elapsed=%s\n", id, r.StatusCode(), time.Since(start).Round(time.Millisecond))
		if stateDir != "" && r.StatusCode() == 204 {
			_ = os.Remove(filepath.Join(stateDir, id+".json"))
		}
	}
	return nil
}

func cmdNetwork(ctx context.Context, args []string) error {
	start := time.Now()
	r, err := apiClient().PutSandboxesSandboxIDNetworkWithBodyWithResponse(ctx, args[0], "application/json", strings.NewReader(args[1]))
	if err != nil {
		return err
	}
	fmt.Printf("RESULT network status=%d elapsed=%s\n", r.StatusCode(), time.Since(start).Round(time.Millisecond))
	return nil
}

func cmdTimeout(ctx context.Context, args []string) error {
	var t int32
	fmt.Sscan(args[1], &t)
	r, err := apiClient().PostSandboxesSandboxIDTimeoutWithResponse(ctx, args[0], api.SandboxTimeoutRequest{Timeout: t})
	if err != nil {
		return err
	}
	fmt.Printf("RESULT timeout status=%d\n", r.StatusCode())
	return nil
}

// raw sends METHOD PATH [BODY] to the REST API with the generated client's
// transport (for endpoints where we want the exact bytes, e.g. invalid input).
func cmdRaw(ctx context.Context, args []string) error {
	var body io.Reader
	if len(args) > 2 {
		body = strings.NewReader(args[2])
	}
	req, _ := http.NewRequestWithContext(ctx, args[0], apiURL+args[1], body)
	req.Header.Set("X-API-Key", os.Getenv("E2B_API_KEY"))
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	fmt.Printf("RESULT raw status=%d\n", resp.StatusCode)
	return nil
}

// ---------------------------------------------------------------- templates

func cmdBuild(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("build", flag.ExitOnError)
	name := fs.String("name", "", "template name (alias)")
	cpu := fs.Int("cpu", 2, "cpu count")
	mem := fs.Int("mem", 512, "memory MB")
	startBody := fs.String("start", "", "raw JSON TemplateBuildStartV2 body")
	fs.Parse(args)
	c := apiClient()
	t0b := time.Now()
	cc, mm := api.CPUCount(*cpu), api.MemoryMB(*mem)
	r, err := c.PostV3TemplatesWithResponse(ctx, api.TemplateBuildRequestV3{Name: name, CpuCount: &cc, MemoryMB: &mm})
	if err != nil {
		return err
	}
	if r.JSON202 == nil {
		return fmt.Errorf("request build: %s", r.Status())
	}
	tid, bid := r.JSON202.TemplateID, r.JSON202.BuildID
	fmt.Printf("RESULT build-requested templateID=%s buildID=%s\n", tid, bid)
	if *startBody == "" {
		return nil
	}
	r2, err := c.PostV2TemplatesTemplateIDBuildsBuildIDWithBodyWithResponse(ctx, tid, bid, "application/json", strings.NewReader(*startBody))
	if err != nil {
		return err
	}
	if r2.StatusCode() >= 300 {
		return fmt.Errorf("start build: %s", r2.Status())
	}
	// poll status quietly; print only transitions and log lines
	prevQuiet := quiet
	quiet = true
	defer func() { quiet = prevQuiet }()
	last := ""
	offset := 0
	for {
		logsOffset := int32(offset)
		st, err := c.GetTemplatesTemplateIDBuildsBuildIDStatusWithResponse(ctx, tid, bid, &api.GetTemplatesTemplateIDBuildsBuildIDStatusParams{LogsOffset: &logsOffset})
		if err != nil {
			return err
		}
		if st.JSON200 == nil {
			return fmt.Errorf("status: %s", st.Status())
		}
		for _, e := range st.JSON200.LogEntries {
			fmt.Printf("    build-log %s [%s] %s\n", e.Timestamp.Format("15:04:05"), e.Level, strings.TrimSpace(e.Message))
		}
		offset += len(st.JSON200.LogEntries)
		s := string(st.JSON200.Status)
		if s != last {
			fmt.Printf("%s build status=%s (t=%s)\n", ts(), s, time.Since(t0b).Round(time.Second))
			last = s
		}
		if s == "ready" || s == "error" {
			if st.JSON200.Reason != nil {
				fmt.Printf("    reason: %s\n", st.JSON200.Reason.Message)
			}
			fmt.Printf("RESULT build status=%s templateID=%s buildID=%s total=%s\n", s, tid, bid, time.Since(t0b).Round(time.Second))
			return nil
		}
		time.Sleep(3 * time.Second)
	}
}

func cmdAlias(ctx context.Context, args []string) error {
	start := time.Now()
	r, err := apiClient().GetTemplatesAliasesAliasWithResponse(ctx, args[0])
	if err != nil {
		return err
	}
	fmt.Printf("RESULT alias status=%d elapsed=%s\n", r.StatusCode(), time.Since(start).Round(time.Millisecond))
	return nil
}

func cmdTemplates(ctx context.Context, _ []string) error {
	r, err := apiClient().GetTemplatesWithResponse(ctx, &api.GetTemplatesParams{})
	if err != nil {
		return err
	}
	if r.JSON200 != nil {
		for _, t := range *r.JSON200 {
			fmt.Printf("  template %s names=%v status=%s cpu=%d mem=%d\n", t.TemplateID, t.Names, t.BuildStatus, t.CpuCount, t.MemoryMB)
		}
		fmt.Printf("RESULT templates count=%d\n", len(*r.JSON200))
	}
	return nil
}

func cmdTemplateDelete(ctx context.Context, args []string) error {
	r, err := apiClient().DeleteTemplatesTemplateIDWithResponse(ctx, args[0])
	if err != nil {
		return err
	}
	fmt.Printf("RESULT template-delete status=%d\n", r.StatusCode())
	return nil
}

// ---------------------------------------------------------------- envd

func sandboxDomain(s sbxState) string {
	if s.Domain != nil && *s.Domain != "" {
		return *s.Domain
	}
	return domain
}

func envdClient(id string) (processconnect.ProcessClient, sbxState) {
	s := loadState(id)
	if s.EnvdAccessToken == "" {
		// GET /sandboxes/{id} returns envdAccessToken without resuming.
		r, err := apiClient().GetSandboxesSandboxIDWithResponse(context.Background(), id)
		if err == nil && r.JSON200 != nil {
			saveState(r.Body)
			s = loadState(id)
			if s.EnvdAccessToken == "" && r.JSON200.EnvdAccessToken != nil {
				s.EnvdAccessToken = *r.JSON200.EnvdAccessToken
			}
		}
	}
	base := fmt.Sprintf("https://%d-%s.%s", envdPort, id, sandboxDomain(s))
	return processconnect.NewProcessClient(httpClient, base, connect.WithProtoJSON()), s
}

func envdHeaders(h http.Header, s sbxState, user string, stream bool) {
	h.Set("X-Access-Token", s.EnvdAccessToken)
	if user != "" {
		h.Set("Authorization", "Basic "+base64.StdEncoding.EncodeToString([]byte(user+":")))
	}
	if stream {
		h.Set("Keepalive-Ping-Interval", "50")
	}
}

type strList []string

func (l *strList) String() string     { return strings.Join(*l, ",") }
func (l *strList) Set(s string) error { *l = append(*l, s); return nil }

func cmdExec(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("exec", flag.ExitOnError)
	user := fs.String("user", "", "user (Basic auth username)")
	env := kv{}
	fs.Var(env, "env", "env k=v (repeatable)")
	tag := fs.String("tag", "", "process tag")
	cwd := fs.String("cwd", "", "cwd")
	bg := fs.Bool("bg", false, "return as soon as the process has started (close the stream)")
	fs.Parse(args)
	argv := fs.Args()
	if len(argv) == 0 {
		return errors.New("exec: need argv")
	}
	c, s := envdClient(argv[0])
	argv = argv[1:]
	req := connect.NewRequest(&process.StartRequest{
		Process: &process.ProcessConfig{Cmd: argv[0], Args: argv[1:], Envs: env},
	})
	if *tag != "" {
		req.Msg.Tag = tag
	}
	if *cwd != "" {
		req.Msg.Process.Cwd = cwd
	}
	envdHeaders(req.Header(), s, *user, true)
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	start := time.Now()
	stream, err := c.Start(cctx, req)
	if err != nil {
		return err
	}
	defer stream.Close()
	for stream.Receive() {
		ev := stream.Msg().GetEvent()
		rel := time.Since(start).Seconds()
		switch {
		case ev.GetStart() != nil:
			fmt.Printf("  [%6.3fs] start pid=%d\n", rel, ev.GetStart().GetPid())
			if *bg {
				fmt.Printf("RESULT exec bg-started pid=%d (closing stream)\n", ev.GetStart().GetPid())
				cancel()
				return nil
			}
		case ev.GetData() != nil:
			d := ev.GetData()
			if len(d.GetStdout()) > 0 {
				for _, line := range strings.Split(strings.TrimRight(string(d.GetStdout()), "\n"), "\n") {
					fmt.Printf("  [%6.3fs] stdout| %s\n", rel, line)
				}
			}
			if len(d.GetStderr()) > 0 {
				for _, line := range strings.Split(strings.TrimRight(string(d.GetStderr()), "\n"), "\n") {
					fmt.Printf("  [%6.3fs] stderr| %s\n", rel, line)
				}
			}
		case ev.GetEnd() != nil:
			e := ev.GetEnd()
			fmt.Printf("  [%6.3fs] end exit=%d exited=%v status=%q err=%q\n", rel, e.GetExitCode(), e.GetExited(), e.GetStatus(), e.GetError())
			fmt.Printf("RESULT exec exit=%d elapsed=%s\n", e.GetExitCode(), time.Since(start).Round(time.Millisecond))
		case ev.GetKeepalive() != nil:
			fmt.Printf("  [%6.3fs] keepalive\n", rel)
		}
	}
	if err := stream.Err(); err != nil {
		return fmt.Errorf("stream: %w", err)
	}
	return nil
}

func cmdPs(ctx context.Context, args []string) error {
	c, s := envdClient(args[0])
	req := connect.NewRequest(&process.ListRequest{})
	envdHeaders(req.Header(), s, "", false)
	r, err := c.List(ctx, req)
	if err != nil {
		return err
	}
	for _, p := range r.Msg.GetProcesses() {
		envs := []string{}
		for k, v := range p.GetConfig().GetEnvs() {
			envs = append(envs, fmt.Sprintf("%s=<len %d>", k, len(v)))
		}
		sort.Strings(envs)
		fmt.Printf("  pid=%d tag=%q cmd=%q args=%q envs(returned by List, values elided)=%v\n", p.GetPid(), p.GetTag(), p.GetConfig().GetCmd(), p.GetConfig().GetArgs(), envs)
	}
	fmt.Printf("RESULT ps count=%d\n", len(r.Msg.GetProcesses()))
	return nil
}

func cmdSignal(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("signal", flag.ExitOnError)
	tag := fs.String("tag", "", "tag")
	pid := fs.Int("pid", 0, "pid")
	sig := fs.String("sig", "KILL", "KILL|TERM")
	fs.Parse(args)
	c, s := envdClient(fs.Arg(0))
	sel := &process.ProcessSelector{}
	if *tag != "" {
		sel.Selector = &process.ProcessSelector_Tag{Tag: *tag}
	} else {
		sel.Selector = &process.ProcessSelector_Pid{Pid: uint32(*pid)}
	}
	sg := process.Signal_SIGNAL_SIGKILL
	if *sig == "TERM" {
		sg = process.Signal_SIGNAL_SIGTERM
	}
	req := connect.NewRequest(&process.SendSignalRequest{Process: sel, Signal: sg})
	envdHeaders(req.Header(), s, "", false)
	_, err := c.SendSignal(ctx, req)
	if err != nil {
		fmt.Printf("RESULT signal error=%v\n", err)
		return nil
	}
	fmt.Printf("RESULT signal ok\n")
	return nil
}

// ---------------------------------------------------------------- ports

func cmdHTTP(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("http", flag.ExitOnError)
	token := fs.Bool("traffic-token", false, "send e2b-traffic-access-token from saved state")
	sse := fs.Bool("sse", false, "stream the body line by line with receive times")
	hdr := kv{}
	fs.Var(hdr, "H", "extra header k=v")
	maxTime := fs.Duration("max", 20*time.Minute, "overall deadline")
	fs.Parse(args)
	id, port, path := fs.Arg(0), fs.Arg(1), fs.Arg(2)
	s := loadState(id)
	u := fmt.Sprintf("https://%s-%s.%s%s", port, id, sandboxDomain(s), path)
	cctx, cancel := context.WithTimeout(ctx, *maxTime)
	defer cancel()
	req, _ := http.NewRequestWithContext(cctx, http.MethodGet, u, nil)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	if *token {
		if s.TrafficAccessToken == nil {
			return errors.New("no traffic token in state")
		}
		req.Header.Set("e2b-traffic-access-token", *s.TrafficAccessToken)
	}
	start := time.Now()
	resp, err := httpClient.Do(req)
	if err != nil {
		fmt.Printf("RESULT http error after %s: %v\n", time.Since(start).Round(time.Millisecond), err)
		return nil
	}
	defer resp.Body.Close()
	if *sse {
		br := bufio.NewReader(resp.Body)
		for {
			line, err := br.ReadString('\n')
			if line != "" {
				fmt.Printf("  [recv +%7.3fs @%d] %s", time.Since(start).Seconds(), time.Now().UnixMilli(), line)
			}
			if err != nil {
				fmt.Printf("RESULT http-sse status=%d ended after %s: %v\n", resp.StatusCode, time.Since(start).Round(time.Millisecond), err)
				return nil
			}
		}
	}
	fmt.Printf("RESULT http status=%d elapsed=%s\n", resp.StatusCode, time.Since(start).Round(time.Millisecond))
	return nil
}
