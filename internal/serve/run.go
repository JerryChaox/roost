package serve

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/JerryChaox/roost/core/config"
	"github.com/JerryChaox/roost/core/driverapi"
	"github.com/JerryChaox/roost/core/provider/e2b"
	"github.com/JerryChaox/roost/core/reconcile"
	"github.com/JerryChaox/roost/core/sandbox"
	"github.com/JerryChaox/roost/core/secrets"
	"github.com/JerryChaox/roost/core/store"
	"github.com/JerryChaox/roost/core/workspace"
)

// The serve lock's lease and how often it is renewed (contracts §12).
const (
	LockLease = 15 * time.Second
	LockRenew = 5 * time.Second
)

// Options are roost serve's command-line settings.
type Options struct {
	ConfigPath string
	SandboxDir string
	Log        *log.Logger
}

// Run is roost serve: it loads the config, takes the serve lock (refusing to
// start while another live process holds it), and serves the API and runs
// the reconciler until ctx ends or the lock is lost.
func Run(ctx context.Context, o Options) error {
	logger := o.Log
	if logger == nil {
		logger = log.New(os.Stderr, "roost serve: ", log.LstdFlags|log.Lmsgprefix)
	}
	cfg, err := config.Load(o.ConfigPath)
	if err != nil {
		return err
	}
	prov, err := e2b.New(cfg.Provider.APIURL, cfg.Provider.APIKey)
	if err != nil {
		return err
	}
	tmpl, err := sandbox.Template(o.SandboxDir, prov.DefaultUser())
	if err != nil {
		return err
	}
	st, err := store.Open(cfg.Server.Database)
	if err != nil {
		return err
	}
	defer st.Close()

	holder := lockHolder()
	if err := st.AcquireLock(ctx, holder, LockLease); err != nil {
		return fmt.Errorf("roost serve cannot start: %w", err)
	}
	logger.Printf("holding the serve lock as %s", holder)
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	go keepLock(ctx, cancel, st, holder, logger)
	defer func() {
		rctx, rcancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer rcancel()
		_ = st.ReleaseLock(rctx, holder)
	}()

	models := secrets.NewE2B(cfg.Models.Providers)
	current := workspace.RuntimeConfigOf(cfg)
	rec := &reconcile.Reconciler{
		Store:    st,
		Provider: prov,
		Models:   models,
		Driver:   driverapi.NewClient(),
		Agent:    current.Agent, // the current config, from the next driver start
		Template: tmpl,
		Settings: reconcile.DefaultSettings,
		Log:      logger,
	}
	ws := &workspace.Service{
		Store:         st,
		Provider:      prov,
		RuntimeConfig: current, // recorded at creation, never rewritten
		Changed:       func(t, n string) { rec.Kick(ctx, t, n) },
	}
	keys := map[string]string{}
	for name, t := range cfg.Tenants {
		for _, k := range t.APIKeys {
			keys[k] = name
		}
	}
	api := &Server{Workspaces: ws, Store: st, Keys: keys, Log: logger}

	ln, err := net.Listen("tcp", cfg.Server.Listen)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	srv := &http.Server{Handler: api.Handler(), ReadHeaderTimeout: 10 * time.Second, ErrorLog: logger}
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ln) }()
	logger.Printf("listening on %s; template %s", ln.Addr(), tmpl.Name)

	go rec.Run(ctx)

	select {
	case <-ctx.Done():
	case err := <-serveErr:
		cancel(err)
	}
	sctx, scancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer scancel()
	_ = srv.Shutdown(sctx)
	if cause := context.Cause(ctx); cause != nil && !errors.Is(cause, context.Canceled) {
		return cause
	}
	return nil
}

// keepLock renews the lease every LockRenew and cancels ctx with
// store.ErrLockLost when it cannot: when another process holds the lock, or
// when renewing keeps failing until the lease is over.
func keepLock(ctx context.Context, cancel context.CancelCauseFunc, st *store.Store, holder string, logger *log.Logger) {
	t := time.NewTicker(LockRenew)
	defer t.Stop()
	renewed := time.Now()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		err := st.RenewLock(ctx, holder, LockLease)
		switch {
		case err == nil:
			renewed = time.Now()
		case errors.Is(err, store.ErrLockLost):
			logger.Printf("lost the serve lock; exiting")
			cancel(store.ErrLockLost)
			return
		default:
			logger.Printf("renew the serve lock: %v", err)
			if time.Since(renewed) >= LockLease {
				cancel(store.ErrLockLost)
				return
			}
		}
	}
}

func lockHolder() string {
	host, _ := os.Hostname()
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%s/%d/%s", host, os.Getpid(), hex.EncodeToString(b))
}
