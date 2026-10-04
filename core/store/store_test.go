package store

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func open(t *testing.T, path string) *Store {
	t.Helper()
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

// Two concurrent issues that saw the same live grant: exactly one wins, and
// the workspace ends with exactly one live grant.
func TestConcurrentIssueOneWins(t *testing.T) {
	ctx := context.Background()
	s := open(t, filepath.Join(t.TempDir(), "roost.db"))
	for ws, prev := range map[string]string{"first": "", "next": "g_1"} {
		if _, _, err := s.CreateWorkspace(ctx, "acme", ws, "u", []byte(`{}`), Audit{Caller: "test", Tenant: "acme", Action: "create", Target: ws}); err != nil {
			t.Fatal(err)
		}
		if prev != "" {
			if _, err := s.IssueGrant(ctx, Grant{ID: prev, Tenant: "acme", Workspace: ws, SandboxID: "s", DriverToken: "d1", BackupToken: "b1"}, "", ""); err != nil {
				t.Fatal(err)
			}
		}
		var wg sync.WaitGroup
		errs := make([]error, 2)
		for i := range 2 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				id := "g_" + ws + string(rune('a'+i))
				_, errs[i] = s.IssueGrant(ctx, Grant{ID: id, Tenant: "acme", Workspace: ws, SandboxID: "s", DriverToken: "d" + id, BackupToken: "b" + id}, prev, "driver_lost")
			}()
		}
		wg.Wait()
		won, lost := 0, 0
		for _, err := range errs {
			switch {
			case err == nil:
				won++
			case errors.Is(err, ErrConflict):
				lost++
			default:
				t.Fatalf("prev %q: %v", prev, err)
			}
		}
		if won != 1 || lost != 1 {
			t.Fatalf("prev %q: %d won, %d lost; want 1 and 1", prev, won, lost)
		}
		var live int
		if err := s.db.QueryRow(`SELECT count(*) FROM grants WHERE workspace = ? AND ended_at IS NULL`, ws).Scan(&live); err != nil {
			t.Fatal(err)
		}
		if live != 1 {
			t.Fatalf("prev %q: %d live grants", prev, live)
		}
	}
}

// Scenario 7: a second roost serve cannot act while the first holds the
// lock; it can once the first's lease has expired, and the first then finds
// its lock lost.
func TestServeLock(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "roost.db")
	now := time.Date(2026, 10, 5, 0, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	a, b := open(t, path), open(t, path) // two processes, one database
	a.Now, b.Now = clock, clock
	const lease = 15 * time.Second

	if err := a.AcquireLock(ctx, "A", lease); err != nil {
		t.Fatal(err)
	}
	if err := b.AcquireLock(ctx, "B", lease); !errors.Is(err, ErrLockHeld) {
		t.Fatalf("second process: want ErrLockHeld, got %v", err)
	}
	now = now.Add(10 * time.Second)
	if err := a.RenewLock(ctx, "A", lease); err != nil {
		t.Fatal(err)
	}
	now = now.Add(10 * time.Second) // 5s left on A's renewed lease
	if err := b.AcquireLock(ctx, "B", lease); !errors.Is(err, ErrLockHeld) {
		t.Fatalf("second process after a renewal: want ErrLockHeld, got %v", err)
	}
	now = now.Add(6 * time.Second) // A stopped renewing: its lease is over
	if err := b.AcquireLock(ctx, "B", lease); err != nil {
		t.Fatalf("takeover after expiry: %v", err)
	}
	if err := a.RenewLock(ctx, "A", lease); !errors.Is(err, ErrLockLost) {
		t.Fatalf("first process after takeover: want ErrLockLost, got %v", err)
	}
}
