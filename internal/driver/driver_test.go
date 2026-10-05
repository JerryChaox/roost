//go:build unix

package driver_test

// These tests run the real roost-driver binary against testdata/fakehost, so
// stdin handling, the storage lock, signals and the Unix-socket proxy are
// exercised as they run in a sandbox.

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

var driverBin, hostBin string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "roost-driver-bin")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	driverBin = filepath.Join(dir, "roost-driver")
	hostBin = filepath.Join(dir, "fakehost")
	for _, b := range []struct{ out, pkg string }{
		{driverBin, "github.com/JerryChaox/roost/cmd/roost-driver"},
		{hostBin, "./testdata/fakehost"},
	} {
		cmd := exec.Command("go", "build", "-o", b.out, b.pkg)
		cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
		if err := cmd.Run(); err != nil {
			fmt.Fprintf(os.Stderr, "build %s: %v\n", b.pkg, err)
			os.RemoveAll(dir)
			os.Exit(1)
		}
	}
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

const (
	grantModels = `{"baseUrls":{"anthropic":"http://gateway.test"},"credential":"model-key-1"}`
	grantAgent  = `{"model":"test-model","thinking":"high"}`
)

type grant struct{ start, driver, backup string }

func newGrant() grant {
	return grant{start: "g-" + rand.Text(), driver: "dt-" + rand.Text(), backup: "bt-" + rand.Text()}
}

func (g grant) stdin() []byte {
	return fmt.Appendf(nil, `{"protocol":1,"start":%q,"driverToken":%q,"backupToken":%q,"models":%s,"agent":%s}`,
		g.start, g.driver, g.backup, grantModels, grantAgent)
}

type logBuffer struct {
	mu sync.Mutex
	b  strings.Builder
}

func (l *logBuffer) add(line string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.b.WriteString(line)
	l.b.WriteByte('\n')
}

func (l *logBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

type driverOpts struct {
	stdin   []byte // nil: no grant
	storage string // empty: a fresh directory
	env     []string
}

type driverProc struct {
	t       *testing.T
	cmd     *exec.Cmd
	base    string // empty if the driver exited before listening
	storage string
	logs    *logBuffer
	exited  chan struct{}
}

// shortTempDir keeps Unix socket paths under macOS's 104-byte limit, which
// t.TempDir() can exceed.
func shortTempDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "rd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func startDriver(t *testing.T, o driverOpts) *driverProc {
	t.Helper()
	storage := o.storage
	if storage == "" {
		storage = filepath.Join(shortTempDir(t), "agent")
	}
	hostCmd, _ := json.Marshal([]string{hostBin})
	cmd := exec.Command(driverBin,
		"--listen", "127.0.0.1:0",
		"--storage", storage,
		"--socket", filepath.Join(shortTempDir(t), "agent.sock"),
		"--host-cmd", string(hostCmd))
	cmd.Stdin = bytes.NewReader(o.stdin)
	cmd.Env = append(os.Environ(), o.env...)
	stderr, err := cmd.StderrPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	d := &driverProc{t: t, cmd: cmd, storage: storage, logs: &logBuffer{}, exited: make(chan struct{})}
	addr := make(chan string, 1)
	go func() {
		sc := bufio.NewScanner(stderr)
		for sc.Scan() {
			line := sc.Text()
			d.logs.add(line)
			if _, a, ok := strings.Cut(line, "listening on "); ok {
				select {
				case addr <- a:
				default:
				}
			}
		}
		_ = cmd.Wait()
		close(d.exited)
	}()
	t.Cleanup(d.stop)
	select {
	case a := <-addr:
		d.base = "http://" + a
	case <-d.exited:
	case <-time.After(10 * time.Second):
		t.Fatal("driver did not start listening")
	}
	return d
}

func (d *driverProc) stop() {
	select {
	case <-d.exited:
	default:
		_ = d.cmd.Process.Signal(syscall.SIGTERM)
		select {
		case <-d.exited:
		case <-time.After(15 * time.Second):
			_ = d.cmd.Process.Kill()
			<-d.exited
			d.t.Error("driver did not stop within 15s of SIGTERM")
		}
	}
	if d.t.Failed() {
		d.t.Logf("driver log:\n%s", d.logs)
	}
}

func (d *driverProc) running() bool {
	select {
	case <-d.exited:
		return false
	default:
		return true
	}
}

type reqOpt func(*http.Request)

func header(k, v string) reqOpt { return func(r *http.Request) { r.Header.Set(k, v) } }

func noProtocol(r *http.Request) { r.Header.Del("Roost-Protocol") }

func (d *driverProc) request(ctx context.Context, method, path, token, body string, opts ...reqOpt) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, d.base+path, strings.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Roost-Protocol", "1")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for _, o := range opts {
		o(req)
	}
	return http.DefaultClient.Do(req)
}

func (d *driverProc) call(t *testing.T, method, path, token, body string, opts ...reqOpt) (int, []byte) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	resp, err := d.request(ctx, method, path, token, body, opts...)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("%s %s: body: %v", method, path, err)
	}
	return resp.StatusCode, b
}

func (d *driverProc) waitHealth(t *testing.T, token string, want int) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		status, _ := d.call(t, "GET", "/v1/health", token, "")
		if status == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("GET /v1/health never answered %d (last %d)", want, status)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

type hostInfo struct {
	Pid         int             `json:"pid"`
	Init        json.RawMessage `json:"init"`
	Disconnects int64           `json:"disconnects"`
}

func (d *driverProc) hostInfo(t *testing.T, token string) hostInfo {
	t.Helper()
	status, body := d.call(t, "GET", "/v1/conversations/_info", token, "")
	if status != http.StatusOK {
		t.Fatalf("GET /v1/conversations/_info: %d %s", status, body)
	}
	var info hostInfo
	if err := json.Unmarshal(body, &info); err != nil {
		t.Fatal(err)
	}
	return info
}

func startReady(t *testing.T) (*driverProc, grant) {
	t.Helper()
	g := newGrant()
	d := startDriver(t, driverOpts{stdin: g.stdin()})
	if d.base == "" {
		t.Fatalf("driver exited:\n%s", d.logs)
	}
	d.waitHealth(t, g.driver, http.StatusOK)
	return d, g
}

func errorCode(b []byte) string {
	var e struct {
		Error string `json:"error"`
	}
	_ = json.Unmarshal(b, &e)
	return e.Error
}

func jsonEqual(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatalf("%s: %v", a, err)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatalf("%s: %v", b, err)
	}
	return reflect.DeepEqual(x, y)
}

func TestTokenGate(t *testing.T) {
	d, g := startReady(t)
	cases := []struct {
		name, method, path, token, body string
		opts                            []reqOpt
		status                          int
		code                            string
	}{
		{name: "no token", method: "GET", path: "/v1/health", status: 401, code: "unauthorized"},
		{name: "wrong token", method: "GET", path: "/v1/health", token: "dt-wrong", status: 401, code: "unauthorized"},
		{name: "no protocol header", method: "GET", path: "/v1/health", token: g.driver, opts: []reqOpt{noProtocol}, status: 400, code: "unsupported_protocol"},
		{name: "other protocol", method: "GET", path: "/v1/health", token: g.driver, opts: []reqOpt{header("Roost-Protocol", "2")}, status: 400, code: "unsupported_protocol"},
		{name: "driver token, health", method: "GET", path: "/v1/health", token: g.driver, status: 200},
		{name: "driver token, conversations", method: "GET", path: "/v1/conversations/_info", token: g.driver, status: 200},
		{name: "driver token, backup", method: "GET", path: "/v1/backup/stream", token: g.driver, status: 422, code: "unsupported"},
		{name: "driver token, snapshot", method: "POST", path: "/v1/snapshot", token: g.driver, status: 422, code: "unsupported"},
		{name: "backup token, state", method: "GET", path: "/v1/state", token: g.backup, status: 200},
		{name: "backup token, backup stream", method: "GET", path: "/v1/backup/stream?after=0", token: g.backup, status: 422, code: "unsupported"},
		{name: "backup token, backup content", method: "GET", path: "/v1/backup/content", token: g.backup, status: 422, code: "unsupported"},
		{name: "backup token, restore", method: "PUT", path: "/v1/restore", token: g.backup, status: 422, code: "unsupported"},
		{name: "backup token, health", method: "GET", path: "/v1/health", token: g.backup, status: 401, code: "unauthorized"},
		{name: "backup token, conversations", method: "GET", path: "/v1/conversations/_info", token: g.backup, status: 401, code: "unauthorized"},
		{name: "backup token, drain", method: "POST", path: "/v1/drain", token: g.backup, body: `{"phase":"open"}`, status: 401, code: "unauthorized"},
		{name: "backup token, rotate", method: "POST", path: "/v1/grant/rotate", token: g.backup, body: `{"driverToken":"dt-x"}`, status: 401, code: "unauthorized"},
		{name: "backup token, snapshot", method: "POST", path: "/v1/snapshot", token: g.backup, status: 401, code: "unauthorized"},
	}
	for _, c := range cases {
		status, body := d.call(t, c.method, c.path, c.token, c.body, c.opts...)
		if status != c.status || (c.code != "" && errorCode(body) != c.code) {
			t.Errorf("%s: %s %s = %d %s, want %d %s", c.name, c.method, c.path, status, body, c.status, c.code)
		}
	}
	// The rejected rotate attempts changed nothing.
	if status, _ := d.call(t, "GET", "/v1/health", g.driver, ""); status != 200 {
		t.Errorf("driver token after the gate checks: %d", status)
	}
}

func TestRotate(t *testing.T) {
	d, g := startReady(t)
	next := "dt-" + rand.Text()

	// Requests with the old token race the rotate; any sent after the rotate
	// answered must be rejected.
	type result struct {
		sent   time.Time
		status int
	}
	var (
		mu      sync.Mutex
		results []result
		wg      sync.WaitGroup
	)
	stop := make(chan struct{})
	for range 4 {
		wg.Go(func() {
			for {
				select {
				case <-stop:
					return
				default:
				}
				sent := time.Now()
				resp, err := d.request(context.Background(), "GET", "/v1/health", g.driver, "")
				if err != nil {
					continue
				}
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				mu.Lock()
				results = append(results, result{sent, resp.StatusCode})
				mu.Unlock()
			}
		})
	}
	time.Sleep(100 * time.Millisecond)
	status, body := d.call(t, "POST", "/v1/grant/rotate", g.driver, `{"driverToken":"`+next+`"}`)
	rotated := time.Now()
	time.Sleep(100 * time.Millisecond)
	close(stop)
	wg.Wait()
	if status != http.StatusNoContent {
		t.Fatalf("rotate: %d %s", status, body)
	}

	var before, after int
	for _, r := range results {
		switch {
		case r.sent.After(rotated):
			after++
			if r.status != http.StatusUnauthorized {
				t.Errorf("old token sent %s after the rotate answered: %d", r.sent.Sub(rotated), r.status)
			}
		case r.status == http.StatusOK:
			before++
		}
	}
	if before == 0 || after == 0 {
		t.Fatalf("the race was not exercised: %d accepted before, %d sent after", before, after)
	}

	checks := []struct {
		name, method, path, token, body string
		status                          int
	}{
		{"old token, health", "GET", "/v1/health", g.driver, "", 401},
		{"old token, conversations", "GET", "/v1/conversations/_info", g.driver, "", 401},
		{"old token, rotate", "POST", "/v1/grant/rotate", g.driver, `{"driverToken":"dt-y"}`, 401},
		{"new token, health", "GET", "/v1/health", next, "", 200},
		{"new token, conversations", "GET", "/v1/conversations/_info", next, "", 200},
		{"backup token, state", "GET", "/v1/state", g.backup, "", 200},
	}
	for _, c := range checks {
		if status, body := d.call(t, c.method, c.path, c.token, c.body); status != c.status {
			t.Errorf("%s: %d %s, want %d", c.name, status, body, c.status)
		}
	}
}

func TestProxyStreamsEventsAsEmitted(t *testing.T) {
	d, g := startReady(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	resp, err := d.request(ctx, "GET", "/v1/conversations/c1/stream", g.driver, "")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 || resp.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("stream: %d %q", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	var ids []string
	var at []time.Time
	sc := bufio.NewScanner(resp.Body)
	for sc.Scan() {
		if id, ok := strings.CutPrefix(sc.Text(), "id: "); ok {
			ids = append(ids, id)
			at = append(at, time.Now())
		}
	}
	if want := []string{"1", "2", "3", "4"}; !reflect.DeepEqual(ids, want) {
		t.Fatalf("event ids %v, want %v", ids, want)
	}
	// The host emits an event every 300ms. Buffered delivery would bring
	// them in together.
	for i := 1; i < len(at); i++ {
		if gap := at[i].Sub(at[i-1]); gap < 150*time.Millisecond {
			t.Errorf("event %s arrived %s after event %s; want them as emitted, ~300ms apart", ids[i], gap, ids[i-1])
		}
	}
}

func TestProxyForwardsRequestWithoutAuthorization(t *testing.T) {
	d, g := startReady(t)
	const uri = "/v1/conversations/echo/a%2Fb/messages?after=c%2B1&bad=%zz"
	const body = `{"requestId":"m-1","content":"hi"}`
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	resp, err := d.request(ctx, "POST", uri, g.driver, body, header("X-Custom", "kept"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated || resp.Header.Get("X-Fake-Host") != "yes" {
		t.Fatalf("echo: %d, X-Fake-Host %q; want the host's status and headers", resp.StatusCode, resp.Header.Get("X-Fake-Host"))
	}
	var echo struct {
		Method string              `json:"method"`
		URI    string              `json:"uri"`
		Header map[string][]string `json:"header"`
		Body   string              `json:"body"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&echo); err != nil {
		t.Fatal(err)
	}
	if v, ok := echo.Header["Authorization"]; ok {
		t.Errorf("the host received Authorization %q", v)
	}
	if got := echo.Header["X-Custom"]; len(got) != 1 || got[0] != "kept" {
		t.Errorf("X-Custom = %q, want it forwarded", got)
	}
	if echo.Method != "POST" || echo.URI != uri || echo.Body != body {
		t.Errorf("host saw %s %s %q; want POST %s %q", echo.Method, echo.URI, echo.Body, uri, body)
	}
}

func TestProxyClientDisconnectReachesHost(t *testing.T) {
	d, g := startReady(t)
	ctx, cancel := context.WithCancel(context.Background())
	resp, err := d.request(ctx, "GET", "/v1/conversations/hang", g.driver, "")
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if resp.StatusCode != 200 {
		t.Fatalf("hang: %d", resp.StatusCode)
	}
	cancel()
	resp.Body.Close()

	deadline := time.Now().Add(5 * time.Second)
	for d.hostInfo(t, g.driver).Disconnects < 1 {
		if time.Now().After(deadline) {
			t.Fatal("the host never saw the caller go away")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestSecondDriverCannotLockStorage(t *testing.T) {
	first, g := startReady(t)
	second := startDriver(t, driverOpts{stdin: newGrant().stdin(), storage: first.storage})
	select {
	case <-second.exited:
	case <-time.After(10 * time.Second):
		t.Fatal("a second driver on the same storage is still running")
	}
	if code := second.cmd.ProcessState.ExitCode(); code == 0 {
		t.Errorf("second driver exited with 0")
	}
	if !strings.Contains(second.logs.String(), "lock") {
		t.Errorf("second driver did not say why it exited:\n%s", second.logs)
	}
	if status, body := first.call(t, "GET", "/v1/health", g.driver, ""); status != 200 {
		t.Errorf("first driver after the second exited: %d %s", status, body)
	}
}

func TestHostRestart(t *testing.T) {
	g := newGrant()
	// A slow initialize keeps the host down long enough to observe it.
	d := startDriver(t, driverOpts{stdin: g.stdin(), env: []string{"FAKEHOST_INIT_DELAY=400ms"}})
	d.waitHealth(t, g.driver, http.StatusOK)
	before := d.hostInfo(t, g.driver)

	if err := syscall.Kill(before.Pid, syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	d.waitHealth(t, g.driver, http.StatusServiceUnavailable)
	if status, body := d.call(t, "GET", "/v1/conversations/_info", g.driver, ""); status != 503 || errorCode(body) != "not_ready" {
		t.Errorf("conversation request while the host is down: %d %s, want 503 not_ready", status, body)
	}

	d.waitHealth(t, g.driver, http.StatusOK)
	after := d.hostInfo(t, g.driver)
	if after.Pid == before.Pid {
		t.Fatalf("host pid %d did not change", after.Pid)
	}
	if !jsonEqual(t, after.Init, before.Init) {
		t.Errorf("initialize after restart %s, want the same as before %s", after.Init, before.Init)
	}
	var init struct {
		Models json.RawMessage `json:"models"`
		Agent  json.RawMessage `json:"agent"`
	}
	if err := json.Unmarshal(after.Init, &init); err != nil {
		t.Fatal(err)
	}
	if !jsonEqual(t, init.Models, []byte(grantModels)) || !jsonEqual(t, init.Agent, []byte(grantAgent)) {
		t.Errorf("initialize models %s agent %s, want the grant's unchanged", init.Models, init.Agent)
	}
	if !d.running() {
		t.Fatal("the driver exited")
	}
}

func TestNoGrant(t *testing.T) {
	d := startDriver(t, driverOpts{})
	if d.base == "" {
		t.Fatalf("driver exited:\n%s", d.logs)
	}
	routes := []struct{ method, path string }{
		{"GET", "/v1/health"},
		{"GET", "/v1/state"},
		{"POST", "/v1/drain"},
		{"POST", "/v1/grant/rotate"},
		{"GET", "/v1/conversations"},
		{"POST", "/v1/conversations/c1/messages"},
		{"GET", "/v1/backup/stream"},
		{"PUT", "/v1/restore"},
		{"POST", "/v1/snapshot"},
		{"GET", "/v1/unknown"},
	}
	for _, r := range routes {
		for _, opts := range [][]reqOpt{nil, {noProtocol}} {
			for _, token := range []string{"", "dt-any"} {
				status, body := d.call(t, r.method, r.path, token, "", opts...)
				if status != 503 || errorCode(body) != "not_ready" {
					t.Errorf("%s %s (token %q, %d opts) = %d %s, want 503 not_ready", r.method, r.path, token, len(opts), status, body)
				}
			}
		}
	}
	if !d.running() {
		t.Fatal("the driver exited")
	}
}
