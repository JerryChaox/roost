// Package store is roost's database (contracts §11) on SQLite: workspaces,
// execution grants, the audit log and the serve lock.
package store

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schema string

// TimeFormat is how every timestamp is stored: RFC 3339, UTC, fixed width.
const TimeFormat = "2006-01-02T15:04:05.000Z"

var (
	ErrNotFound        = errors.New("not found")
	ErrWorkspaceExists = errors.New("workspace exists with another owner")
	ErrConflict        = errors.New("conflict: the row changed")
	ErrLockHeld        = errors.New("the serve lock is held by another live process")
	ErrLockLost        = errors.New("the serve lock was lost")
)

// Phases (contracts §4).
const (
	Provisioning = "provisioning"
	Active       = "active"
	Restoring    = "restoring"
	Failed       = "failed"
)

// Store is roost's database.
type Store struct {
	db  *sql.DB
	Now func() time.Time // the clock; tests replace it
}

// Open opens (creating if needed) the SQLite database at path and applies
// the schema.
func Open(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	q := url.Values{}
	q.Add("_pragma", "busy_timeout(10000)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "synchronous(FULL)")
	q.Set("_txlock", "immediate") // writers serialize at BEGIN, never deadlock on upgrade
	db, err := sql.Open("sqlite", "file:"+path+"?"+q.Encode())
	if err != nil {
		return nil, fmt.Errorf("store: %w", err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: schema: %w", err)
	}
	return &Store{db: db, Now: time.Now}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) now() string { return fmtTime(s.Now()) }

func fmtTime(t time.Time) string { return t.UTC().Format(TimeFormat) }

func parseTime(v string) time.Time {
	t, _ := time.Parse(TimeFormat, v)
	return t
}

func (s *Store) tx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}

// ---------------------------------------------------------------- lock

// AcquireLock takes the serve lock for holder, unless another holder's lease
// is unexpired.
func (s *Store) AcquireLock(ctx context.Context, holder string, lease time.Duration) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		var cur, exp string
		err := tx.QueryRowContext(ctx, `SELECT holder, expires_at FROM serve_lock WHERE id = 1`).Scan(&cur, &exp)
		now := s.Now()
		switch {
		case errors.Is(err, sql.ErrNoRows):
		case err != nil:
			return err
		case cur != holder && parseTime(exp).After(now):
			return fmt.Errorf("%w (holder %s, lease until %s)", ErrLockHeld, cur, exp)
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO serve_lock (id, holder, expires_at) VALUES (1, ?, ?)
			ON CONFLICT (id) DO UPDATE SET holder = excluded.holder, expires_at = excluded.expires_at`,
			holder, fmtTime(now.Add(lease)))
		return err
	})
}

// RenewLock extends holder's lease. It fails with ErrLockLost if the lease
// has expired or another process holds the lock.
func (s *Store) RenewLock(ctx context.Context, holder string, lease time.Duration) error {
	now := s.Now()
	res, err := s.db.ExecContext(ctx, `UPDATE serve_lock SET expires_at = ? WHERE id = 1 AND holder = ? AND expires_at > ?`,
		fmtTime(now.Add(lease)), holder, fmtTime(now))
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return ErrLockLost
	}
	return nil
}

// ReleaseLock gives the lock up, if holder still holds it.
func (s *Store) ReleaseLock(ctx context.Context, holder string) error {
	_, err := s.db.ExecContext(ctx, `DELETE FROM serve_lock WHERE id = 1 AND holder = ?`, holder)
	return err
}

// ---------------------------------------------------------------- workspaces

// Workspace is one row of workspaces.
type Workspace struct {
	Tenant        string
	Name          string
	Owner         string
	ForkedFrom    *string
	CreatedAt     time.Time
	RuntimeConfig json.RawMessage
	Phase         string
	PhaseSince    time.Time
	PhaseDetail   json.RawMessage // nil in provisioning and active
}

// Audit is one audit entry.
type Audit struct {
	Caller  string
	Tenant  string
	Action  string
	Target  string
	Reason  string
	Request any // marshalled to JSON; never a secret
	Outcome string
}

const wsColumns = `tenant, name, owner, forked_from, created_at, runtime_config, phase, phase_since, phase_detail`

type scanner interface{ Scan(...any) error }

func scanWorkspace(r scanner) (Workspace, error) {
	var w Workspace
	var forked, detail sql.NullString
	var created, since, rc string
	if err := r.Scan(&w.Tenant, &w.Name, &w.Owner, &forked, &created, &rc, &w.Phase, &since, &detail); err != nil {
		return w, err
	}
	if forked.Valid {
		w.ForkedFrom = &forked.String
	}
	if detail.Valid {
		w.PhaseDetail = json.RawMessage(detail.String)
	}
	w.CreatedAt, w.PhaseSince, w.RuntimeConfig = parseTime(created), parseTime(since), json.RawMessage(rc)
	return w, nil
}

// CreateWorkspace inserts a workspace in provisioning, or returns the
// existing one if it has the same owner (created is false). An existing
// workspace with another owner is ErrWorkspaceExists. A creation is audited
// in the same transaction.
func (s *Store) CreateWorkspace(ctx context.Context, tenant, name, owner string, runtimeConfig json.RawMessage, audit Audit) (w Workspace, created bool, err error) {
	err = s.tx(ctx, func(tx *sql.Tx) error {
		cur, err := scanWorkspace(tx.QueryRowContext(ctx, `SELECT `+wsColumns+` FROM workspaces WHERE tenant = ? AND name = ?`, tenant, name))
		switch {
		case err == nil:
			if cur.Owner != owner {
				return ErrWorkspaceExists
			}
			w = cur
			return nil
		case !errors.Is(err, sql.ErrNoRows):
			return err
		}
		now := s.now()
		if _, err := tx.ExecContext(ctx, `INSERT INTO workspaces (`+wsColumns+`) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL)`,
			tenant, name, owner, now, string(runtimeConfig), Provisioning, now); err != nil {
			return err
		}
		if err := appendAudit(ctx, tx, now, audit); err != nil {
			return err
		}
		w = Workspace{Tenant: tenant, Name: name, Owner: owner, CreatedAt: parseTime(now), RuntimeConfig: runtimeConfig, Phase: Provisioning, PhaseSince: parseTime(now)}
		created = true
		return nil
	})
	return w, created, err
}

// GetWorkspace returns a workspace of the tenant, or ErrNotFound.
func (s *Store) GetWorkspace(ctx context.Context, tenant, name string) (Workspace, error) {
	w, err := scanWorkspace(s.db.QueryRowContext(ctx, `SELECT `+wsColumns+` FROM workspaces WHERE tenant = ? AND name = ?`, tenant, name))
	if errors.Is(err, sql.ErrNoRows) {
		return w, ErrNotFound
	}
	return w, err
}

// Filter narrows ListWorkspaces; empty fields match everything.
type Filter struct {
	Owner      string
	Phase      string
	ForkedFrom string
}

// ListWorkspaces returns up to limit workspaces of the tenant ordered by
// name, after the name `after`, and whether more follow.
func (s *Store) ListWorkspaces(ctx context.Context, tenant string, f Filter, after string, limit int) ([]Workspace, bool, error) {
	where := []string{"tenant = ?", "name > ?"}
	args := []any{tenant, after}
	if f.Owner != "" {
		where, args = append(where, "owner = ?"), append(args, f.Owner)
	}
	if f.Phase != "" {
		where, args = append(where, "phase = ?"), append(args, f.Phase)
	}
	if f.ForkedFrom != "" {
		where, args = append(where, "forked_from = ?"), append(args, f.ForkedFrom)
	}
	args = append(args, limit+1)
	rows, err := s.db.QueryContext(ctx, `SELECT `+wsColumns+` FROM workspaces WHERE `+strings.Join(where, " AND ")+` ORDER BY name LIMIT ?`, args...)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	var out []Workspace
	for rows.Next() {
		w, err := scanWorkspace(rows)
		if err != nil {
			return nil, false, err
		}
		out = append(out, w)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	more := len(out) > limit
	if more {
		out = out[:limit]
	}
	return out, more, nil
}

// WorkspaceKey names a workspace.
type WorkspaceKey struct{ Tenant, Name string }

// Reconcilable returns every workspace the serve reconciler acts on: those
// provisioning or active.
func (s *Store) Reconcilable(ctx context.Context) ([]WorkspaceKey, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT tenant, name FROM workspaces WHERE phase IN (?, ?) ORDER BY tenant, name`, Provisioning, Active)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []WorkspaceKey
	for rows.Next() {
		var k WorkspaceKey
		if err := rows.Scan(&k.Tenant, &k.Name); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, rows.Err()
}

// SetPhase moves a workspace from one phase to another by compare-and-set:
// ErrConflict if it is no longer in `from`. detail must be nil for
// provisioning and active.
func (s *Store) SetPhase(ctx context.Context, tenant, name, from, to string, detail any) error {
	var d sql.NullString
	if detail != nil {
		b, err := json.Marshal(detail)
		if err != nil {
			return err
		}
		d = sql.NullString{String: string(b), Valid: true}
	}
	res, err := s.db.ExecContext(ctx, `UPDATE workspaces SET phase = ?, phase_since = ?, phase_detail = ? WHERE tenant = ? AND name = ? AND phase = ?`,
		to, s.now(), d, tenant, name, from)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return ErrConflict
	}
	return nil
}

// ---------------------------------------------------------------- grants

// Grant is one row of grants.
type Grant struct {
	ID           string // the grant's start
	Tenant       string
	Workspace    string
	SandboxID    string
	DriverToken  string
	BackupToken  string
	ModelKeyRef  string // empty: none
	IssuedAt     time.Time
	EndedAt      *time.Time
	EndReason    string
	KeyRevokedAt *time.Time
}

// String keeps the tokens out of any log line that formats a grant.
func (g Grant) String() string { return "grant " + g.ID }

// GoString does the same for %#v.
func (g Grant) GoString() string { return g.String() }

const grantColumns = `id, tenant, workspace, sandbox_id, driver_token, backup_token, model_key_ref, issued_at, ended_at, end_reason, key_revoked_at`

func scanGrant(r scanner) (Grant, error) {
	var g Grant
	var ref, ended, reason, revoked sql.NullString
	var issued string
	if err := r.Scan(&g.ID, &g.Tenant, &g.Workspace, &g.SandboxID, &g.DriverToken, &g.BackupToken, &ref, &issued, &ended, &reason, &revoked); err != nil {
		return g, err
	}
	g.ModelKeyRef, g.EndReason, g.IssuedAt = ref.String, reason.String, parseTime(issued)
	if ended.Valid {
		t := parseTime(ended.String)
		g.EndedAt = &t
	}
	if revoked.Valid {
		t := parseTime(revoked.String)
		g.KeyRevokedAt = &t
	}
	return g, nil
}

// IssueGrant inserts g as the workspace's live grant, ending the previous
// live grant with endReason in the same transaction. It is a compare-and-set:
// prev is the id of the live grant the caller saw ("" for none), and if the
// live grant is another one by now, nothing changes and it returns
// ErrConflict. Of two concurrent issues seeing the same live grant, one wins.
func (s *Store) IssueGrant(ctx context.Context, g Grant, prev, endReason string) (Grant, error) {
	err := s.tx(ctx, func(tx *sql.Tx) error {
		var cur string
		err := tx.QueryRowContext(ctx, `SELECT id FROM grants WHERE tenant = ? AND workspace = ? AND ended_at IS NULL`, g.Tenant, g.Workspace).Scan(&cur)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if cur != prev {
			return ErrConflict
		}
		now := s.now()
		if cur != "" {
			if _, err := tx.ExecContext(ctx, `UPDATE grants SET ended_at = ?, end_reason = ? WHERE id = ?`, now, endReason, cur); err != nil {
				return err
			}
		}
		var ref sql.NullString
		if g.ModelKeyRef != "" {
			ref = sql.NullString{String: g.ModelKeyRef, Valid: true}
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO grants (id, tenant, workspace, sandbox_id, driver_token, backup_token, model_key_ref, issued_at)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?)`, g.ID, g.Tenant, g.Workspace, g.SandboxID, g.DriverToken, g.BackupToken, ref, now); err != nil {
			if strings.Contains(err.Error(), "UNIQUE") {
				return ErrConflict
			}
			return err
		}
		g.IssuedAt = parseTime(now)
		return nil
	})
	return g, err
}

// LiveGrant returns the workspace's live grant, or ErrNotFound.
func (s *Store) LiveGrant(ctx context.Context, tenant, workspace string) (Grant, error) {
	g, err := scanGrant(s.db.QueryRowContext(ctx, `SELECT `+grantColumns+` FROM grants WHERE tenant = ? AND workspace = ? AND ended_at IS NULL`, tenant, workspace))
	if errors.Is(err, sql.ErrNoRows) {
		return g, ErrNotFound
	}
	return g, err
}

// UnrevokedGrants returns the ended grants whose model key is not revoked.
func (s *Store) UnrevokedGrants(ctx context.Context) ([]Grant, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+grantColumns+` FROM grants WHERE ended_at IS NOT NULL AND key_revoked_at IS NULL ORDER BY ended_at`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Grant
	for rows.Next() {
		g, err := scanGrant(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// MarkKeyRevoked records that the grant's model key is revoked.
func (s *Store) MarkKeyRevoked(ctx context.Context, id string) error {
	_, err := s.db.ExecContext(ctx, `UPDATE grants SET key_revoked_at = ? WHERE id = ? AND key_revoked_at IS NULL`, s.now(), id)
	return err
}

// ---------------------------------------------------------------- audit

func appendAudit(ctx context.Context, tx *sql.Tx, now string, a Audit) error {
	req, err := json.Marshal(a.Request)
	if err != nil {
		return err
	}
	var reason, outcome sql.NullString
	if a.Reason != "" {
		reason = sql.NullString{String: a.Reason, Valid: true}
	}
	if a.Outcome != "" {
		outcome = sql.NullString{String: a.Outcome, Valid: true}
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO audit (time, caller, tenant, action, target, reason, request, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		now, a.Caller, a.Tenant, a.Action, a.Target, reason, string(req), outcome)
	return err
}
