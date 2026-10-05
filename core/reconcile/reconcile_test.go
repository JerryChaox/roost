package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JerryChaox/roost/core/driverapi"
	"github.com/JerryChaox/roost/core/grant"
	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/secrets"
	"github.com/JerryChaox/roost/core/store"
)

// fakeE2B is an in-memory provider. A sandbox in `hidden` exists but is not
// listed yet (E2B's listing is eventually consistent). Spawn reads the grant
// from stdin, as the driver does; the driver then counts as running with it.
type fakeE2B struct {
	mu      sync.Mutex
	seq     int
	now     time.Time
	boxes   map[string]*provider.Sandbox
	hidden  map[string]bool
	drivers map[string]string // sandbox → start of the grant its driver holds
	agents  []string          // the agent model of every grant a driver was started with
	killed  []string
	calls   []string
}

func newFake() *fakeE2B {
	return &fakeE2B{now: time.Unix(1_800_000_000, 0), boxes: map[string]*provider.Sandbox{}, hidden: map[string]bool{}, drivers: map[string]string{}}
}

func (f *fakeE2B) add(id string, labels map[string]string, state provider.State, hidden bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	f.boxes[id] = &provider.Sandbox{ID: id, State: state, Labels: labels, StartedAt: f.now.Add(time.Duration(f.seq) * time.Second), EndAt: f.now.Add(time.Hour)}
	f.hidden[id] = hidden
}

func (f *fakeE2B) record(c string) { f.calls = append(f.calls, c) }

func (f *fakeE2B) Create(_ context.Context, s provider.CreateSpec) (string, error) {
	f.mu.Lock()
	id := fmt.Sprintf("sbx-%d", f.seq+1)
	f.record("create " + id)
	f.mu.Unlock()
	f.add(id, s.Labels, provider.Running, false)
	return id, nil
}

func (f *fakeE2B) Get(_ context.Context, id string) (provider.Sandbox, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if b, ok := f.boxes[id]; ok {
		return *b, nil
	}
	return provider.Sandbox{}, provider.ErrNotFound
}

func (f *fakeE2B) List(_ context.Context, labels map[string]string) ([]provider.Sandbox, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []provider.Sandbox
	for id, b := range f.boxes {
		if f.hidden[id] {
			continue
		}
		match := true
		for k, v := range labels {
			match = match && b.Labels[k] == v
		}
		if match {
			out = append(out, *b)
		}
	}
	return out, nil
}

func (f *fakeE2B) Kill(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("kill " + id)
	delete(f.boxes, id)
	delete(f.drivers, id)
	f.killed = append(f.killed, id)
	return nil
}

func (f *fakeE2B) Exec(_ context.Context, id string, _ provider.Process) (provider.ExecResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("exec " + id)
	if f.boxes[id] != nil && f.boxes[id].State != provider.Running {
		f.record("WOKE " + id)
	}
	delete(f.drivers, id)
	return provider.ExecResult{}, nil
}

func (f *fakeE2B) Spawn(_ context.Context, id string, p provider.Process) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("spawn " + id)
	var g struct {
		Start string `json:"start"`
		Agent struct {
			Model string `json:"model"`
		} `json:"agent"`
	}
	if err := json.Unmarshal(p.Stdin, &g); err != nil {
		return 0, err
	}
	f.drivers[id] = g.Start
	f.agents = append(f.agents, g.Agent.Model)
	return 1, nil
}

func (f *fakeE2B) Connect(context.Context, string, time.Duration) error { return errors.New("unused") }
func (f *fakeE2B) Pause(context.Context, string) error                  { return errors.New("unused") }
func (f *fakeE2B) Reboot(context.Context, string, time.Duration) error  { return errors.New("unused") }
func (f *fakeE2B) SetTimeout(_ context.Context, id string, _ time.Duration) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("timeout " + id)
	return nil
}
func (f *fakeE2B) DefaultUser() string                                        { return "user" }
func (f *fakeE2B) Endpoint(id string, port int) string                        { return fmt.Sprintf("fake://%d-%s", port, id) }
func (f *fakeE2B) FindTemplate(context.Context, string) (bool, error)         { return true, nil }
func (f *fakeE2B) BuildTemplate(context.Context, provider.TemplateSpec) error { return nil }

// Ready is the fake driver: it answers for the grant its sandbox's driver
// was started with. Calling a paused sandbox's driver would wake it.
func (f *fakeE2B) Ready(_ context.Context, t driverapi.Target, start string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	id := t.URL[strings.Index(t.URL, "-")+1:]
	f.record("driver " + id)
	if b := f.boxes[id]; b == nil {
		return errors.New("no sandbox")
	} else if b.State != provider.Running {
		f.record("WOKE " + id)
	}
	if f.drivers[id] != start {
		return errors.New("not ready")
	}
	return nil
}

func (f *fakeE2B) RunInProgress(context.Context, driverapi.Target) (bool, error) { return false, nil }

func (f *fakeE2B) count(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, prefix) {
			n++
		}
	}
	return n
}

type fakeModels struct{}

func (fakeModels) Issue(context.Context, secrets.GrantRef) (secrets.Credential, error) {
	return secrets.Credential{BaseURLs: map[string]string{"anthropic": "https://example.test"}, Value: "placeholder"}, nil
}
func (fakeModels) Revoke(context.Context, string) error { return secrets.ErrUnsupported }
func (fakeModels) HeaderRules() (map[string]map[string]string, error) {
	return map[string]map[string]string{}, nil
}

func setup(t *testing.T) (*Reconciler, *fakeE2B, store.WorkspaceKey) {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "roost.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	k := store.WorkspaceKey{Tenant: "acme", Name: "ws-1"}
	rc := `{"agent":{"model":"anthropic/m","thinking":"low","systemPrompt":{"base":"pi","append":null}},"models":{"baseUrls":{}}}`
	if _, _, err := st.CreateWorkspace(context.Background(), k.Tenant, k.Name, "u", json.RawMessage(rc), store.Audit{Caller: "t", Tenant: k.Tenant, Action: "create", Target: k.Name}); err != nil {
		t.Fatal(err)
	}
	f := newFake()
	r := &Reconciler{Store: st, Provider: f, Models: fakeModels{}, Driver: f, Template: provider.TemplateSpec{Name: "roost-x"}, Settings: DefaultSettings,
		Agent: grant.Agent{Model: "anthropic/current", Thinking: "low", SystemPrompt: grant.SystemPrompt{Base: "pi"}}}
	return r, f, k
}

func labels(k store.WorkspaceKey) map[string]string {
	return map[string]string{provider.LabelTenant: k.Tenant, provider.LabelWorkspace: k.Name}
}

func phase(t *testing.T, r *Reconciler, k store.WorkspaceKey) string {
	t.Helper()
	w, err := r.Store.GetWorkspace(context.Background(), k.Tenant, k.Name)
	if err != nil {
		t.Fatal(err)
	}
	return w.Phase
}

// Scenario 19: a control plane killed after creating a sandbox and before
// issuing its grant. The new process's first listing does not show that
// sandbox yet, so it creates another; once both are listed, exactly one
// remains by the time the workspace is active.
func TestScenario19OneSandboxOnceActive(t *testing.T) {
	ctx := context.Background()
	r, f, k := setup(t)
	f.add("sbx-orphan", labels(k), provider.Running, true) // created by the killed process

	for i := 0; i < 10 && phase(t, r, k) != store.Active; i++ {
		if i == 1 {
			f.mu.Lock()
			f.hidden["sbx-orphan"] = false // the listing catches up
			f.mu.Unlock()
		}
		if err := r.Reconcile(ctx, k); err != nil {
			t.Fatal(err)
		}
	}
	if phase(t, r, k) != store.Active {
		t.Fatalf("not active: %v", f.calls)
	}
	left, _ := f.List(ctx, labels(k))
	if len(left) != 1 {
		t.Fatalf("%d sandboxes remain once active, want 1: %v", len(left), f.calls)
	}
	g, err := r.Store.LiveGrant(ctx, k.Tenant, k.Name)
	if err != nil || g.SandboxID != left[0].ID {
		t.Fatalf("live grant %v on %s, remaining sandbox %s", err, g.SandboxID, left[0].ID)
	}
}

// activate brings the workspace to active on one sandbox.
func activate(t *testing.T, r *Reconciler, f *fakeE2B, k store.WorkspaceKey) store.Grant {
	t.Helper()
	ctx := context.Background()
	for i := 0; i < 5 && phase(t, r, k) != store.Active; i++ {
		if err := r.Reconcile(ctx, k); err != nil {
			t.Fatal(err)
		}
	}
	if phase(t, r, k) != store.Active {
		t.Fatalf("not active: %v", f.calls)
	}
	g, err := r.Store.LiveGrant(ctx, k.Tenant, k.Name)
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func TestActiveRemovesOrphans(t *testing.T) {
	ctx := context.Background()
	r, f, k := setup(t)
	g := activate(t, r, f, k)
	f.add("sbx-stray", labels(k), provider.Running, false)
	f.add("sbx-other-ws", map[string]string{provider.LabelTenant: k.Tenant, provider.LabelWorkspace: "ws-2"}, provider.Running, false)

	if err := r.Reconcile(ctx, k); err != nil {
		t.Fatal(err)
	}
	if strings.Join(f.killed, ",") != "sbx-stray" {
		t.Fatalf("killed %v, want only sbx-stray", f.killed)
	}
	if _, err := f.Get(ctx, g.SandboxID); err != nil {
		t.Fatalf("the live grant's sandbox was removed: %v", err)
	}
	if now, _ := r.Store.LiveGrant(ctx, k.Tenant, k.Name); now.ID != g.ID {
		t.Fatalf("live grant changed from %s to %s", g.ID, now.ID)
	}
}

// The reconciler never calls the driver of a paused sandbox, nor execs in
// it, however long its driver has been silent: either would wake it.
func TestPausedSandboxIsLeftAlone(t *testing.T) {
	ctx := context.Background()
	r, f, k := setup(t)
	g := activate(t, r, f, k)
	f.mu.Lock()
	f.boxes[g.SandboxID].State = provider.Paused
	delete(f.drivers, g.SandboxID) // a paused sandbox's driver does not answer
	f.calls = nil
	f.mu.Unlock()
	r.Now = func() time.Time { return time.Now().Add(time.Hour) } // long past any start grace

	for range 10 {
		if err := r.Reconcile(ctx, k); err != nil {
			t.Fatal(err)
		}
	}
	if n := f.count("driver ") + f.count("exec ") + f.count("spawn ") + f.count("WOKE "); n != 0 {
		t.Fatalf("touched the paused sandbox: %v", f.calls)
	}
	if now, _ := r.Store.LiveGrant(ctx, k.Tenant, k.Name); now.ID != g.ID {
		t.Fatalf("issued a new grant for a paused sandbox: %s", now.ID)
	}
	if phase(t, r, k) != store.Active {
		t.Fatal("a paused sandbox made the workspace leave active")
	}
}

// A grant carries the agent settings of the current roost.yaml, not the
// runtime_config the workspace was created with (contracts §13: they apply
// from the next driver start).
func TestGrantTakesCurrentAgentSettings(t *testing.T) {
	r, f, k := setup(t) // runtime_config says anthropic/m; the current config anthropic/current
	activate(t, r, f, k)
	if len(f.agents) != 1 || f.agents[0] != "anthropic/current" {
		t.Fatalf("drivers started with agent models %v, want [anthropic/current]", f.agents)
	}
}
