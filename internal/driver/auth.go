package driver

import (
	"crypto/subtle"
	"net/http"
	"strings"
	"sync/atomic"
)

type scope int

const (
	scopeNone scope = iota
	scopeDriver
	scopeBackup
)

// tokens is one immutable generation of the grant's tokens. A rotate swaps
// the whole value, so every request sees either the old generation or the new
// one, never a mix.
type tokens struct {
	driver []byte
	backup []byte
}

// gate holds the current tokens. They live only here, in memory.
type gate struct {
	cur atomic.Pointer[tokens]
}

func newGate(driverToken, backupToken string) *gate {
	g := &gate{}
	g.cur.Store(&tokens{driver: []byte(driverToken), backup: []byte(backupToken)})
	return g
}

// authenticate returns the scope of the presented token and the generation it
// was checked against.
func (g *gate) authenticate(presented []byte) (scope, *tokens) {
	t := g.cur.Load()
	isDriver := subtle.ConstantTimeCompare(presented, t.driver) == 1
	isBackup := subtle.ConstantTimeCompare(presented, t.backup) == 1
	switch {
	case isDriver:
		return scopeDriver, t
	case isBackup:
		return scopeBackup, t
	}
	return scopeNone, t
}

// rotate replaces the driver token, keeping the backup token. It succeeds
// only if seen, the generation the caller authenticated against, is still
// current: of two racing rotates authorized by the same token, one wins and
// the other's token is already stale.
func (g *gate) rotate(seen *tokens, driverToken []byte) bool {
	return g.cur.CompareAndSwap(seen, &tokens{driver: driverToken, backup: seen.backup})
}

// bearer extracts the token of an "Authorization: Bearer <token>" header.
func bearer(r *http.Request) ([]byte, bool) {
	const prefix = "Bearer "
	h := r.Header.Get("Authorization")
	if len(h) <= len(prefix) || !strings.EqualFold(h[:len(prefix)], prefix) {
		return nil, false
	}
	return []byte(h[len(prefix):]), true
}
