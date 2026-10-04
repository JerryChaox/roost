// Package reconcile is roost serve's reconciler (contracts §12). It is
// level-triggered: each pass compares a workspace's rows with what the
// provider and the driver report and takes the next idempotent step, so a
// crash only means the step runs again. No table records work to be done.
package reconcile

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sort"
	"sync"
	"time"

	"github.com/JerryChaox/roost/core/driverapi"
	"github.com/JerryChaox/roost/core/grant"
	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/sandbox"
	"github.com/JerryChaox/roost/core/secrets"
	"github.com/JerryChaox/roost/core/store"
)

// Driver is what the reconciler asks a grant's driver.
type Driver interface {
	// Ready is nil when the driver's health answers 200 and its state names
	// the grant start.
	Ready(ctx context.Context, t driverapi.Target, start string) error
	// RunInProgress reports whether any conversation has a run going.
	RunInProgress(ctx context.Context, t driverapi.Target) (bool, error)
}

// ModelAccess mints and revokes grants' model credentials and gives the
// header injection a new sandbox is created with.
type ModelAccess interface {
	secrets.SecretProvider
	HeaderRules() (map[string]map[string]string, error)
}

// Settings are the reconciler's timings and thresholds.
type Settings struct {
	Tick           time.Duration // how often every workspace is reconciled
	StartGrace     time.Duration // a starting driver is not lost before this
	HealthFailures int           // consecutive failed checks that make a driver lost
	CreateFailures int           // consecutive failed builds or creates that fail provisioning
	SandboxTimeout time.Duration // a sandbox pauses this long after creation or extension
	ExtendBelow    time.Duration // extend a busy sandbox when less than this is left
}

// DefaultSettings are the values M1 runs with.
var DefaultSettings = Settings{
	Tick:           2 * time.Second,
	StartGrace:     60 * time.Second,
	HealthFailures: 3,
	CreateFailures: 5,
	SandboxTimeout: time.Hour,
	ExtendBelow:    15 * time.Minute,
}

// Reconciler drives workspaces from provisioning to active and keeps them
// there.
type Reconciler struct {
	Store    *store.Store
	Provider provider.Provider
	Models   ModelAccess
	Driver   Driver
	// Agent is the agent section of every grant issued: the current
	// roost.yaml's agent settings, which apply from the next driver start
	// (contracts §13). A workspace's runtime_config keeps what it was
	// created with and is not consulted here.
	Agent    grant.Agent
	Template provider.TemplateSpec
	Settings Settings
	Log      *log.Logger
	Now      func() time.Time

	mu       sync.Mutex
	running  map[store.WorkspaceKey]bool // a pass is under way
	again    map[store.WorkspaceKey]bool // kicked during a pass: run once more
	counts   map[store.WorkspaceKey]*counts
	noRevoke map[string]bool // grants whose model key cannot be revoked (logged once)

	templateMu    sync.Mutex
	templateReady bool
}

type counts struct {
	createFailures int
	healthFailures int
}

func (r *Reconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Reconciler) logf(format string, args ...any) {
	if r.Log != nil {
		r.Log.Printf(format, args...)
	}
}

func (r *Reconciler) init() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.running == nil {
		r.running = map[store.WorkspaceKey]bool{}
		r.again = map[store.WorkspaceKey]bool{}
		r.counts = map[store.WorkspaceKey]*counts{}
		r.noRevoke = map[string]bool{}
	}
}

// Run reconciles every provisioning or active workspace on every tick, and
// revokes ended grants' model keys, until ctx ends.
func (r *Reconciler) Run(ctx context.Context) {
	r.init()
	t := time.NewTicker(r.Settings.Tick)
	defer t.Stop()
	for {
		keys, err := r.Store.Reconcilable(ctx)
		if err != nil && ctx.Err() == nil {
			r.logf("reconcile: list workspaces: %v", err)
		}
		for _, k := range keys {
			r.Kick(ctx, k.Tenant, k.Name)
		}
		r.RevokeEnded(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Kick reconciles one workspace now, in the background. At most one pass
// per workspace runs at a time; a kick during a pass runs one more after it.
func (r *Reconciler) Kick(ctx context.Context, tenant, name string) {
	r.init()
	k := store.WorkspaceKey{Tenant: tenant, Name: name}
	r.mu.Lock()
	if r.running[k] {
		r.again[k] = true
		r.mu.Unlock()
		return
	}
	r.running[k] = true
	r.mu.Unlock()
	go func() {
		for {
			if err := r.Reconcile(ctx, k); err != nil && ctx.Err() == nil {
				r.logf("reconcile %s/%s: %v", k.Tenant, k.Name, err)
			}
			r.mu.Lock()
			if !r.again[k] || ctx.Err() != nil {
				delete(r.running, k)
				delete(r.again, k)
				r.mu.Unlock()
				return
			}
			delete(r.again, k)
			r.mu.Unlock()
		}
	}()
}

func (r *Reconciler) countsOf(k store.WorkspaceKey) *counts {
	r.mu.Lock()
	defer r.mu.Unlock()
	c := r.counts[k]
	if c == nil {
		c = &counts{}
		r.counts[k] = c
	}
	return c
}

// Reconcile takes the next step for one workspace. Callers must not run two
// passes over the same workspace at once (Kick guarantees it).
func (r *Reconciler) Reconcile(ctx context.Context, k store.WorkspaceKey) error {
	r.init()
	w, err := r.Store.GetWorkspace(ctx, k.Tenant, k.Name)
	if err != nil {
		return err
	}
	if w.Phase != store.Provisioning && w.Phase != store.Active {
		return nil
	}
	sbs, err := r.Provider.List(ctx, map[string]string{provider.LabelTenant: k.Tenant, provider.LabelWorkspace: k.Name})
	if err != nil {
		return err
	}
	var live *store.Grant
	if g, err := r.Store.LiveGrant(ctx, k.Tenant, k.Name); err == nil {
		live = &g
	} else if !errors.Is(err, store.ErrNotFound) {
		return err
	}
	if w.Phase == store.Provisioning {
		return r.provision(ctx, w, sbs, live)
	}
	return r.keepActive(ctx, w, sbs, live)
}

func (r *Reconciler) provision(ctx context.Context, w store.Workspace, sbs []provider.Sandbox, live *store.Grant) error {
	k := store.WorkspaceKey{Tenant: w.Tenant, Name: w.Name}
	if live != nil && find(sbs, live.SandboxID) == nil {
		// The listing may lag; ask for the live grant's sandbox itself.
		if sb, err := r.Provider.Get(ctx, live.SandboxID); err == nil {
			sbs = append(sbs, sb)
		} else if !errors.Is(err, provider.ErrNotFound) {
			return err
		}
	}
	if len(sbs) == 0 {
		if err := r.create(ctx, w); err != nil {
			c := r.countsOf(k)
			c.createFailures++
			if c.createFailures >= r.Settings.CreateFailures {
				c.createFailures = 0
				r.logf("workspace %s/%s failed: %v", w.Tenant, w.Name, err)
				return r.Store.SetPhase(ctx, w.Tenant, w.Name, store.Provisioning, store.Failed, map[string]string{"error": err.Error()})
			}
			return err
		}
		r.countsOf(k).createFailures = 0
		return nil
	}
	keep := pick(sbs, live)
	r.removeOthers(ctx, w, sbs, keep.ID)
	if live == nil || live.SandboxID != keep.ID {
		return r.startDriver(ctx, w, keep.ID, live)
	}
	t := r.target(*live)
	if err := r.Driver.Ready(ctx, t, live.ID); err != nil {
		return r.driverDown(ctx, w, *live, err)
	}
	r.countsOf(k).healthFailures = 0
	if err := r.Store.SetPhase(ctx, w.Tenant, w.Name, store.Provisioning, store.Active, nil); err != nil {
		return err
	}
	r.logf("workspace %s/%s active on sandbox %s (%s)", w.Tenant, w.Name, keep.ID, live.ID)
	return nil
}

func (r *Reconciler) keepActive(ctx context.Context, w store.Workspace, sbs []provider.Sandbox, live *store.Grant) error {
	k := store.WorkspaceKey{Tenant: w.Tenant, Name: w.Name}
	if live == nil {
		// Issuing a grant always ends the previous one in the same
		// transaction, so an active workspace has one; start one if not.
		if len(sbs) == 0 {
			return r.Store.SetPhase(ctx, w.Tenant, w.Name, store.Active, store.Failed, map[string]string{"error": "sandbox lost"})
		}
		keep := pick(sbs, nil)
		r.removeOthers(ctx, w, sbs, keep.ID)
		return r.startDriver(ctx, w, keep.ID, nil)
	}
	sb := find(sbs, live.SandboxID)
	if sb == nil {
		// Confirm with the provider before calling it lost: a listing can
		// miss a sandbox in transition.
		got, err := r.Provider.Get(ctx, live.SandboxID)
		switch {
		case errors.Is(err, provider.ErrNotFound):
			r.logf("workspace %s/%s failed: sandbox %s lost", w.Tenant, w.Name, live.SandboxID)
			return r.Store.SetPhase(ctx, w.Tenant, w.Name, store.Active, store.Failed, map[string]string{"error": "sandbox lost"})
		case err != nil:
			return err
		}
		sb = &got
	}
	r.removeOthers(ctx, w, sbs, sb.ID)
	if sb.State != provider.Running {
		// A request to a paused sandbox's endpoint would wake it: leave it.
		return nil
	}
	t := r.target(*live)
	if err := r.Driver.Ready(ctx, t, live.ID); err != nil {
		return r.driverDown(ctx, w, *live, err)
	}
	r.countsOf(k).healthFailures = 0
	busy, err := r.Driver.RunInProgress(ctx, t)
	if err != nil {
		return fmt.Errorf("runs in progress: %w", err)
	}
	if busy && sb.EndAt.Sub(r.now()) < r.Settings.ExtendBelow {
		if err := r.Provider.SetTimeout(ctx, sb.ID, r.Settings.SandboxTimeout); err != nil {
			return err
		}
		r.logf("workspace %s/%s: a run is in progress; sandbox %s timeout extended", w.Tenant, w.Name, sb.ID)
	}
	return nil
}

// driverDown counts a failed check of the live grant's driver. A driver
// still starting gets StartGrace; after HealthFailures failures in a row it
// is lost, and a new grant starts a new driver on the same sandbox.
func (r *Reconciler) driverDown(ctx context.Context, w store.Workspace, live store.Grant, cause error) error {
	if r.now().Sub(live.IssuedAt) < r.Settings.StartGrace {
		return nil
	}
	c := r.countsOf(store.WorkspaceKey{Tenant: w.Tenant, Name: w.Name})
	c.healthFailures++
	if c.healthFailures < r.Settings.HealthFailures {
		return nil
	}
	c.healthFailures = 0
	r.logf("workspace %s/%s: driver of %s lost (%v); starting a new one", w.Tenant, w.Name, live.ID, cause)
	return r.startDriver(ctx, w, live.SandboxID, &live)
}

// startDriver issues a new grant on the sandbox, ending prev (if any) with
// driver_lost in the same transaction, and starts its driver.
func (r *Reconciler) startDriver(ctx context.Context, w store.Workspace, sandboxID string, prev *store.Grant) error {
	id := grant.NewID()
	cred, err := r.Models.Issue(ctx, secrets.GrantRef{ID: id, Tenant: w.Tenant, Workspace: w.Name})
	if err != nil {
		return fmt.Errorf("model credential: %w", err)
	}
	dt, bt := grant.Tokens()
	prevID := ""
	if prev != nil {
		prevID = prev.ID
	}
	g, err := r.Store.IssueGrant(ctx, store.Grant{
		ID: id, Tenant: w.Tenant, Workspace: w.Name, SandboxID: sandboxID,
		DriverToken: dt, BackupToken: bt, ModelKeyRef: cred.Ref,
	}, prevID, "driver_lost")
	if err != nil {
		return fmt.Errorf("issue grant: %w", err)
	}
	r.countsOf(store.WorkspaceKey{Tenant: w.Tenant, Name: w.Name}).healthFailures = 0
	r.logf("workspace %s/%s: issued %s on sandbox %s", w.Tenant, w.Name, g.ID, sandboxID)
	if err := sandbox.StartDriver(ctx, r.Provider, sandboxID, grant.JSON(g.ID, dt, bt, cred, r.Agent)); err != nil {
		// The grant stays: its driver counts as lost after StartGrace, and
		// a new grant is issued then.
		return fmt.Errorf("%s: %w", g.ID, err)
	}
	return nil
}

func (r *Reconciler) create(ctx context.Context, w store.Workspace) error {
	if err := r.ensureTemplate(ctx); err != nil {
		return err
	}
	rules, err := r.Models.HeaderRules()
	if err != nil {
		return err
	}
	id, err := r.Provider.Create(ctx, provider.CreateSpec{
		Template:    r.Template.Name,
		Labels:      map[string]string{provider.LabelTenant: w.Tenant, provider.LabelWorkspace: w.Name},
		Timeout:     r.Settings.SandboxTimeout,
		HeaderRules: rules,
	})
	if err != nil {
		return err
	}
	r.logf("workspace %s/%s: created sandbox %s", w.Tenant, w.Name, id)
	return nil
}

// ensureTemplate finds the template by name, building it when it has no
// ready build. One build at a time in this process.
func (r *Reconciler) ensureTemplate(ctx context.Context) error {
	r.templateMu.Lock()
	defer r.templateMu.Unlock()
	if r.templateReady {
		return nil
	}
	ok, err := r.Provider.FindTemplate(ctx, r.Template.Name)
	if err != nil {
		return err
	}
	if !ok {
		r.logf("building template %s", r.Template.Name)
		start := r.now()
		if err := r.Provider.BuildTemplate(ctx, r.Template); err != nil {
			return fmt.Errorf("build template %s: %w", r.Template.Name, err)
		}
		r.logf("template %s ready after %s", r.Template.Name, r.now().Sub(start).Round(time.Second))
	}
	r.templateReady = true
	return nil
}

// removeOthers kills every listed sandbox but keep (never the live grant's:
// callers pass it as keep).
func (r *Reconciler) removeOthers(ctx context.Context, w store.Workspace, sbs []provider.Sandbox, keep string) {
	for _, sb := range sbs {
		if sb.ID == keep {
			continue
		}
		if err := r.Provider.Kill(ctx, sb.ID); err != nil {
			r.logf("workspace %s/%s: remove extra sandbox %s: %v", w.Tenant, w.Name, sb.ID, err)
			continue
		}
		r.logf("workspace %s/%s: removed extra sandbox %s", w.Tenant, w.Name, sb.ID)
	}
}

func (r *Reconciler) target(g store.Grant) driverapi.Target {
	return driverapi.Target{URL: r.Provider.Endpoint(g.SandboxID, sandbox.DriverPort), Token: g.DriverToken}
}

// RevokeEnded revokes the model key of every ended grant whose key is not
// revoked yet. A model access that cannot revoke is final: logged once, the
// grant's key_revoked_at stays NULL, and it is not retried.
func (r *Reconciler) RevokeEnded(ctx context.Context) {
	r.init()
	gs, err := r.Store.UnrevokedGrants(ctx)
	if err != nil {
		if ctx.Err() == nil {
			r.logf("revoke: %v", err)
		}
		return
	}
	for _, g := range gs {
		r.mu.Lock()
		skip := r.noRevoke[g.ID]
		r.mu.Unlock()
		if skip {
			continue
		}
		err := r.Models.Revoke(ctx, g.ModelKeyRef)
		switch {
		case err == nil:
			if err := r.Store.MarkKeyRevoked(ctx, g.ID); err != nil {
				r.logf("revoke %s: %v", g.ID, err)
			}
		case errors.Is(err, secrets.ErrUnsupported):
			r.mu.Lock()
			r.noRevoke[g.ID] = true
			r.mu.Unlock()
			r.logf("grant %s ended; its model key cannot be revoked by this model access (%v)", g.ID, err)
		default:
			r.logf("revoke %s: %v (retrying)", g.ID, err)
		}
	}
}

func find(sbs []provider.Sandbox, id string) *provider.Sandbox {
	for i := range sbs {
		if sbs[i].ID == id {
			return &sbs[i]
		}
	}
	return nil
}

// pick chooses the sandbox to keep: the live grant's if it is listed (it must
// never be removed), otherwise the newest.
func pick(sbs []provider.Sandbox, live *store.Grant) provider.Sandbox {
	if live != nil {
		if sb := find(sbs, live.SandboxID); sb != nil {
			return *sb
		}
	}
	s := append([]provider.Sandbox(nil), sbs...)
	sort.Slice(s, func(i, j int) bool {
		if !s[i].StartedAt.Equal(s[j].StartedAt) {
			return s[i].StartedAt.After(s[j].StartedAt)
		}
		return s[i].ID > s[j].ID
	})
	return s[0]
}
