package serve

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JerryChaox/roost/core/driverapi"
	"github.com/JerryChaox/roost/core/store"
	"github.com/JerryChaox/roost/core/workspace"
)

// fixedRouter routes every active workspace to one fake driver.
type fixedRouter struct {
	store  *store.Store
	target driverapi.Target
}

func (f fixedRouter) Route(ctx context.Context, tenant, name string) (driverapi.Target, error) {
	w, err := f.store.GetWorkspace(ctx, tenant, name)
	if err != nil {
		return driverapi.Target{}, err
	}
	if w.Phase != store.Active {
		return driverapi.Target{}, workspace.ErrBusy
	}
	return f.target, nil
}

type harness struct {
	api    *httptest.Server
	store  *store.Store
	driver http.HandlerFunc // set by each test
	mu     sync.Mutex
}

const (
	keyAcme  = "acme-key"
	keyOther = "other-key"
)

func newHarness(t *testing.T) *harness {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "roost.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	h := &harness{store: st}
	drv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.mu.Lock()
		fn := h.driver
		h.mu.Unlock()
		if fn == nil {
			w.WriteHeader(http.StatusTeapot)
			return
		}
		fn(w, r)
	}))
	t.Cleanup(drv.Close)
	srv := &Server{
		Workspaces: &workspace.Service{Store: st},
		Store:      st,
		Router:     fixedRouter{store: st, target: driverapi.Target{URL: drv.URL, Token: "driver-token"}},
		Keys:       map[string]string{keyAcme: "acme", keyOther: "other"},
	}
	h.api = httptest.NewServer(srv.Handler())
	t.Cleanup(h.api.Close)
	return h
}

func (h *harness) setDriver(fn http.HandlerFunc) {
	h.mu.Lock()
	h.driver = fn
	h.mu.Unlock()
}

func (h *harness) do(t *testing.T, key, method, path, body string) (*http.Response, string) {
	t.Helper()
	req, _ := http.NewRequest(method, h.api.URL+path, strings.NewReader(body))
	if key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, string(b)
}

// activeWorkspace creates ws-1 for acme and marks it active.
func (h *harness) activeWorkspace(t *testing.T) {
	t.Helper()
	if resp, body := h.do(t, keyAcme, "PUT", "/v1/workspaces/ws-1", `{"owner":"u-1"}`); resp.StatusCode != 201 {
		t.Fatalf("PUT: %d %s", resp.StatusCode, body)
	}
	if err := h.store.SetPhase(context.Background(), "acme", "ws-1", store.Provisioning, store.Active, nil); err != nil {
		t.Fatal(err)
	}
}

// A tenant never reaches another tenant's workspace: every route answers
// 404 unknown_workspace (and the listing omits it), and the driver is never
// called. A missing or unknown key is 401.
func TestTenantIsolation(t *testing.T) {
	h := newHarness(t)
	h.activeWorkspace(t)
	called := false
	h.setDriver(func(w http.ResponseWriter, r *http.Request) { called = true; w.WriteHeader(200) })

	routes := []struct{ method, path, body string }{
		{"GET", "/v1/workspaces/ws-1", ""},
		{"POST", "/v1/workspaces/ws-1/conversations", `{"key":"k"}`},
		{"GET", "/v1/workspaces/ws-1/conversations", ""},
		{"POST", "/v1/workspaces/ws-1/conversations/c_1/messages", `{"id":"m-1","text":"hi"}`},
		{"GET", "/v1/workspaces/ws-1/conversations/c_1/entries", ""},
		{"GET", "/v1/workspaces/ws-1/conversations/c_1/events", ""},
		{"POST", "/v1/workspaces/ws-1/conversations/c_1/interrupt", ""},
		{"POST", "/v1/workspaces/ws-1/conversations/c_1/reset", `{}`},
	}
	for _, rt := range routes {
		resp, body := h.do(t, keyOther, rt.method, rt.path, rt.body)
		if resp.StatusCode != 404 || !strings.Contains(body, `"unknown_workspace"`) {
			t.Errorf("other tenant %s %s: %d %s", rt.method, rt.path, resp.StatusCode, body)
		}
		for _, key := range []string{"", "nope"} {
			resp, body := h.do(t, key, rt.method, rt.path, rt.body)
			if resp.StatusCode != 401 || strings.TrimSpace(body) != `{"error":"unauthorized"}` {
				t.Errorf("key %q %s %s: %d %s", key, rt.method, rt.path, resp.StatusCode, body)
			}
		}
	}
	if called {
		t.Error("the driver was called for another tenant")
	}
	// PUT by the other tenant creates its own workspace; it is not acme's.
	if resp, body := h.do(t, keyOther, "PUT", "/v1/workspaces/ws-1", `{"owner":"u-9"}`); resp.StatusCode != 201 {
		t.Errorf("other tenant PUT: %d %s", resp.StatusCode, body)
	}
	_, body := h.do(t, keyAcme, "GET", "/v1/workspaces", "")
	if !strings.Contains(body, `"u-1"`) || strings.Contains(body, `"u-9"`) {
		t.Errorf("acme's listing: %s", body)
	}
}

func TestMessagesMapping(t *testing.T) {
	h := newHarness(t)
	h.activeWorkspace(t)
	var got struct {
		path, auth, proto string
		body              map[string]any
	}
	reply := func(status int, body string) {
		h.setDriver(func(w http.ResponseWriter, r *http.Request) {
			got.path, got.auth, got.proto = r.URL.EscapedPath(), r.Header.Get("Authorization"), r.Header.Get("Roost-Protocol")
			got.body = nil
			_ = json.NewDecoder(r.Body).Decode(&got.body)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			io.WriteString(w, body)
		})
	}
	const path = "/v1/workspaces/ws-1/conversations/c%20x/messages"

	for _, tc := range []struct{ delivery, whenBusy string }{{"", "follow_up"}, {"queue", "follow_up"}, {"steer", "steer"}} {
		reply(202, `{"status":"queued"}`)
		req := `{"id":"m-1","text":"hello","delivery":"` + tc.delivery + `"}`
		if tc.delivery == "" {
			req = `{"id":"m-1","text":"hello"}`
		}
		resp, body := h.do(t, keyAcme, "POST", path, req)
		if resp.StatusCode != 202 || strings.TrimSpace(body) != `{"status":"queued"}` {
			t.Fatalf("delivery %q: %d %s", tc.delivery, resp.StatusCode, body)
		}
		want := map[string]any{"requestId": "m-1", "content": "hello", "whenBusy": tc.whenBusy}
		if len(got.body) != len(want) || got.body["requestId"] != want["requestId"] || got.body["content"] != want["content"] || got.body["whenBusy"] != want["whenBusy"] {
			t.Fatalf("delivery %q: driver got %v, want %v", tc.delivery, got.body, want)
		}
		if got.path != "/v1/conversations/c%20x/messages" || got.auth != "Bearer driver-token" || got.proto != "1" {
			t.Fatalf("driver request: path %q auth %q protocol %q", got.path, got.auth, got.proto)
		}
	}

	reply(200, `{"status":"duplicate"}`)
	if resp, body := h.do(t, keyAcme, "POST", path, `{"id":"m-1","text":"hello"}`); resp.StatusCode != 200 || strings.TrimSpace(body) != `{"status":"duplicate"}` {
		t.Fatalf("duplicate: %d %s", resp.StatusCode, body)
	}

	reply(202, `{}`)
	got.path = ""
	if resp, body := h.do(t, keyAcme, "POST", path, `{"id":"m-2","text":"x","delivery":"later"}`); resp.StatusCode != 400 || !strings.Contains(body, "invalid_request") || got.path != "" {
		t.Fatalf("unknown delivery: %d %s (driver path %q)", resp.StatusCode, body, got.path)
	}
	if resp, _ := h.do(t, keyAcme, "POST", "/v1/workspaces/ws-1/conversations/a%2Fb/messages", `{"id":"m","text":"x"}`); resp.StatusCode != 400 {
		t.Fatalf("conversation id with /: %d", resp.StatusCode)
	}

	reply(404, `{"error":"unknown_conversation","detail":"no such conversation"}`)
	if resp, body := h.do(t, keyAcme, "POST", path, `{"id":"m-3","text":"x"}`); resp.StatusCode != 404 || !strings.Contains(body, "unknown_conversation") {
		t.Fatalf("host error: %d %s", resp.StatusCode, body)
	}
	reply(502, `<html>bad gateway</html>`)
	if resp, body := h.do(t, keyAcme, "POST", path, `{"id":"m-3","text":"x"}`); resp.StatusCode != 503 || !strings.Contains(body, "not_ready") {
		t.Fatalf("unreachable driver: %d %s", resp.StatusCode, body)
	}
}

// Events pass through as the driver writes them, not when its response
// ends, and a client that goes away ends the request to the driver.
func TestEventsStreamThrough(t *testing.T) {
	h := newHarness(t)
	h.activeWorkspace(t)
	release := make(chan struct{})
	upstreamDone := make(chan struct{})
	var lastEventID, after string
	h.setDriver(func(w http.ResponseWriter, r *http.Request) {
		defer close(upstreamDone)
		lastEventID, after = r.Header.Get("Last-Event-ID"), r.URL.Query().Get("after")
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		io.WriteString(w, "event: entry\nid: 7\ndata: {\"cursor\":\"7\"}\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-release:
			io.WriteString(w, "event: run\ndata: {}\n\n")
			w.(http.Flusher).Flush()
		case <-r.Context().Done():
			return
		}
		<-r.Context().Done() // the stream stays open until the client leaves
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", h.api.URL+"/v1/workspaces/ws-1/conversations/c_1/events?after=3", nil)
	req.Header.Set("Authorization", "Bearer "+keyAcme)
	req.Header.Set("Last-Event-ID", "5")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); resp.StatusCode != 200 || !strings.HasPrefix(ct, "text/event-stream") {
		t.Fatalf("%d %s", resp.StatusCode, ct)
	}
	lines := make(chan string, 16)
	go func() {
		sc := bufio.NewScanner(resp.Body)
		for sc.Scan() {
			lines <- sc.Text()
		}
		close(lines)
	}()
	next := func(want string) {
		t.Helper()
		select {
		case l := <-lines:
			if l != want {
				t.Fatalf("got %q, want %q", l, want)
			}
		case <-time.After(2 * time.Second):
			t.Fatalf("%q did not arrive while the driver's stream was open", want)
		}
	}
	next("event: entry") // before the driver writes anything more
	next("id: 7")
	next(`data: {"cursor":"7"}`)
	next("")
	close(release)
	next("event: run")
	if lastEventID != "5" || after != "3" {
		t.Fatalf("driver got Last-Event-ID %q after %q", lastEventID, after)
	}

	cancel()
	select {
	case <-upstreamDone:
	case <-time.After(2 * time.Second):
		t.Fatal("the driver's stream stayed open after the client left")
	}
}
