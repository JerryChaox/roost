//go:build unix

package driver

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"sync/atomic"
)

// ProtocolHeader carries the driver protocol version on every request.
const ProtocolHeader = "Roost-Protocol"

const maxRequestBody = 64 << 10

// HealthResponse is the body of GET /v1/health.
type HealthResponse struct {
	Driver Fingerprint `json:"driver"`
	Host   HostInfo    `json:"host"`
}

// StateResponse is the body of GET /v1/state. M1 has no restore and no backup
// capture, so status is always "ready" and the two backup fields are null.
type StateResponse struct {
	Start          string          `json:"start"`
	Status         string          `json:"status"`
	RestoredFrom   any             `json:"restoredFrom"`
	BackupPosition any             `json:"backupPosition"`
	Host           json.RawMessage `json:"host"`
}

// DrainRequest and DrainResponse are the bodies of POST /v1/drain.
type DrainRequest struct {
	Phase string `json:"phase"`
}

type DrainResponse struct {
	Running int `json:"running"`
}

// RotateRequest is the body of POST /v1/grant/rotate.
type RotateRequest struct {
	DriverToken string `json:"driverToken"`
}

// ErrorResponse is the body of every error (driver-protocol §7).
type ErrorResponse struct {
	Error  string `json:"error"`
	Detail string `json:"detail"`
}

type route int

const (
	routeUnknown route = iota
	routeConversations
	routeHealth
	routeState
	routeDrain
	routeRotate
	routeBackup   // M2
	routeRestore  // M2
	routeSnapshot // M2
)

func classify(path string) route {
	switch {
	case path == "/v1/conversations" || strings.HasPrefix(path, "/v1/conversations/"):
		return routeConversations
	case path == "/v1/health":
		return routeHealth
	case path == "/v1/state":
		return routeState
	case path == "/v1/drain":
		return routeDrain
	case path == "/v1/grant/rotate":
		return routeRotate
	case strings.HasPrefix(path, "/v1/backup/"):
		return routeBackup
	case path == "/v1/restore":
		return routeRestore
	case path == "/v1/snapshot":
		return routeSnapshot
	}
	return routeUnknown
}

// backupAllowed reports whether the backup token may call the route
// (driver-protocol §1).
func (r route) backupAllowed() bool {
	return r == routeBackup || r == routeRestore || r == routeState
}

// active is the driver's state once it holds a grant.
type active struct {
	start string
	gate  *gate
	host  *supervisor
}

// server answers the driver protocol (driver-protocol §3).
type server struct {
	log   *log.Logger
	fp    Fingerprint
	proxy http.Handler

	active atomic.Pointer[active] // nil until a grant is read
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	a := s.active.Load()
	if a == nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", "the driver holds no execution grant")
		return
	}
	if strings.TrimSpace(r.Header.Get(ProtocolHeader)) != "1" {
		writeError(w, http.StatusBadRequest, "unsupported_protocol", "this driver speaks Roost-Protocol 1")
		return
	}
	sc, seen := scopeNone, (*tokens)(nil)
	if tok, ok := bearer(r); ok {
		sc, seen = a.gate.authenticate(tok)
	}
	rt := classify(r.URL.Path)
	if sc == scopeNone || (sc == scopeBackup && !rt.backupAllowed()) {
		writeError(w, http.StatusUnauthorized, "unauthorized", "missing, invalid or superseded token")
		return
	}

	switch rt {
	case routeConversations:
		if a.host.current() == nil {
			writeError(w, http.StatusServiceUnavailable, "not_ready", "agent host not ready")
			return
		}
		s.proxy.ServeHTTP(w, r)
	case routeHealth:
		if methodIs(w, r, http.MethodGet) {
			s.health(w, a)
		}
	case routeState:
		if methodIs(w, r, http.MethodGet) {
			s.state(w, r, a)
		}
	case routeDrain:
		if methodIs(w, r, http.MethodPost) {
			s.drain(w, r, a)
		}
	case routeRotate:
		if methodIs(w, r, http.MethodPost) {
			s.rotate(w, r, a, seen)
		}
	case routeBackup, routeRestore, routeSnapshot:
		writeError(w, http.StatusUnprocessableEntity, "unsupported", "backup capture, snapshots and restore are not in this driver")
	default:
		writeError(w, http.StatusBadRequest, "invalid_request", "no such route")
	}
}

func (s *server) health(w http.ResponseWriter, a *active) {
	h := a.host.current()
	if h == nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", "agent host not ready")
		return
	}
	writeJSON(w, http.StatusOK, HealthResponse{Driver: s.fp, Host: h.info})
}

func (s *server) state(w http.ResponseWriter, r *http.Request, a *active) {
	h := a.host.current()
	if h == nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", "agent host not ready")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), rpcTimeout)
	defer cancel()
	res, err := h.rpc.call(ctx, "state", nil)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, StateResponse{Start: a.start, Status: "ready", Host: res})
}

func (s *server) drain(w http.ResponseWriter, r *http.Request, a *active) {
	var req DrainRequest
	if !decodeBody(w, r, &req) {
		return
	}
	var method string
	switch req.Phase {
	case "draining":
		method = "quiesce"
	case "open":
		method = "resume"
	default:
		writeError(w, http.StatusBadRequest, "invalid_request", `phase must be "draining" or "open"`)
		return
	}
	h := a.host.current()
	if h == nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", "agent host not ready")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), rpcTimeout)
	defer cancel()
	res, err := h.rpc.call(ctx, method, nil)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", err.Error())
		return
	}
	var reply struct {
		Running *int `json:"running"`
	}
	if err := json.Unmarshal(res, &reply); err != nil || reply.Running == nil {
		writeError(w, http.StatusServiceUnavailable, "not_ready", "agent host's "+method+" reply has no running count")
		return
	}
	writeJSON(w, http.StatusOK, DrainResponse{Running: *reply.Running})
}

func (s *server) rotate(w http.ResponseWriter, r *http.Request, a *active, seen *tokens) {
	var req RotateRequest
	if !decodeBody(w, r, &req) {
		return
	}
	next := []byte(req.DriverToken)
	if len(next) == 0 {
		writeError(w, http.StatusBadRequest, "invalid_request", "driverToken is required")
		return
	}
	if subtle.ConstantTimeCompare(next, seen.backup) == 1 {
		writeError(w, http.StatusBadRequest, "invalid_request", "driverToken must differ from the backup token")
		return
	}
	if !a.gate.rotate(seen, next) {
		writeError(w, http.StatusUnauthorized, "unauthorized", "missing, invalid or superseded token")
		return
	}
	s.log.Printf("driver token rotated")
	w.WriteHeader(http.StatusNoContent)
}

func methodIs(w http.ResponseWriter, r *http.Request, method string) bool {
	if r.Method != method {
		writeError(w, http.StatusBadRequest, "invalid_request", "this route takes "+method)
		return false
	}
	return true
}

func decodeBody(w http.ResponseWriter, r *http.Request, v any) bool {
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxRequestBody)).Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "body: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		// Every body is built from plain fields and JSON the driver already
		// parsed; failing to encode one is a bug.
		panic(err)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(append(b, '\n'))
}

func writeError(w http.ResponseWriter, status int, code, detail string) {
	b, _ := json.Marshal(ErrorResponse{Error: code, Detail: detail})
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(append(b, '\n'))
}
