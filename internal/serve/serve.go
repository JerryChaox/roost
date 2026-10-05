// Package serve is roost serve's HTTP API (contracts §3, §4): tenant API key
// authentication, the workspace routes, and the conversation routes mapped
// onto the conversation interface of the workspace's driver.
package serve

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/JerryChaox/roost/core/driverapi"
	"github.com/JerryChaox/roost/core/store"
	"github.com/JerryChaox/roost/core/workspace"
)

const (
	maxBody  = 1 << 20
	pageSize = 100
)

// Router finds the driver of an active workspace.
type Router interface {
	Route(ctx context.Context, tenant, name string) (driverapi.Target, error)
}

// Server is the HTTP API.
type Server struct {
	Workspaces *workspace.Service
	Store      *store.Store
	Router     Router            // defaults to Workspaces
	HTTP       *http.Client      // to drivers; no overall timeout (streams)
	Keys       map[string]string // tenant API key → tenant
	Log        *log.Logger
}

// Handler returns the API's handler.
func (s *Server) Handler() http.Handler {
	if s.Router == nil {
		s.Router = s.Workspaces
	}
	if s.HTTP == nil {
		s.HTTP = &http.Client{Transport: driverapi.NewTransport()}
	}
	m := http.NewServeMux()
	m.HandleFunc("PUT /v1/workspaces/{w}", s.auth(s.putWorkspace))
	m.HandleFunc("GET /v1/workspaces/{w}", s.auth(s.getWorkspace))
	m.HandleFunc("GET /v1/workspaces", s.auth(s.listWorkspaces))
	m.HandleFunc("POST /v1/workspaces/{w}/conversations", s.auth(s.createConversation))
	m.HandleFunc("GET /v1/workspaces/{w}/conversations", s.auth(s.listConversations))
	m.HandleFunc("POST /v1/workspaces/{w}/conversations/{c}/messages", s.auth(s.sendMessage))
	m.HandleFunc("GET /v1/workspaces/{w}/conversations/{c}/entries", s.auth(s.entries))
	m.HandleFunc("GET /v1/workspaces/{w}/conversations/{c}/events", s.auth(s.events))
	m.HandleFunc("POST /v1/workspaces/{w}/conversations/{c}/interrupt", s.auth(s.interrupt))
	m.HandleFunc("POST /v1/workspaces/{w}/conversations/{c}/reset", s.auth(s.reset))
	return m
}

type handler func(w http.ResponseWriter, r *http.Request, tenant string)

// auth resolves the tenant from "Authorization: Bearer <tenant API key>",
// comparing against every key in constant time.
func (s *Server) auth(h handler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		const prefix = "Bearer "
		hdr := r.Header.Get("Authorization")
		tenant := ""
		if len(hdr) > len(prefix) && strings.EqualFold(hdr[:len(prefix)], prefix) {
			presented := []byte(hdr[len(prefix):])
			for key, t := range s.Keys {
				if subtle.ConstantTimeCompare(presented, []byte(key)) == 1 {
					tenant = t
				}
			}
		}
		if tenant == "" {
			writeError(w, http.StatusUnauthorized, "unauthorized", "")
			return
		}
		h(w, r, tenant)
	}
}

// ---------------------------------------------------------------- workspaces

type workspaceJSON struct {
	Name        string          `json:"name"`
	Owner       string          `json:"owner"`
	ForkedFrom  *string         `json:"forked_from"`
	Phase       string          `json:"phase"`
	PhaseSince  string          `json:"phase_since"`
	PhaseDetail json.RawMessage `json:"phase_detail"`
	CreatedAt   string          `json:"created_at"`
}

func toJSON(ws store.Workspace) workspaceJSON {
	detail := ws.PhaseDetail
	if detail == nil {
		detail = json.RawMessage("null")
	}
	return workspaceJSON{
		Name: ws.Name, Owner: ws.Owner, ForkedFrom: ws.ForkedFrom, Phase: ws.Phase,
		PhaseSince: ws.PhaseSince.UTC().Format(store.TimeFormat), PhaseDetail: detail,
		CreatedAt: ws.CreatedAt.UTC().Format(store.TimeFormat),
	}
}

func (s *Server) putWorkspace(w http.ResponseWriter, r *http.Request, tenant string) {
	name := r.PathValue("w")
	if !workspace.ValidName(name) {
		writeError(w, http.StatusBadRequest, "invalid_request", workspace.ErrInvalidName.Error())
		return
	}
	var body struct {
		Owner  string `json:"owner"`
		Reason string `json:"reason"`
	}
	if !decode(w, r, &body) {
		return
	}
	if body.Owner == "" {
		writeError(w, http.StatusBadRequest, "invalid_request", "owner is required")
		return
	}
	ws, created, err := s.Workspaces.Create(r.Context(), tenant, name, body.Owner, "tenant:"+tenant, body.Reason)
	switch {
	case errors.Is(err, store.ErrWorkspaceExists):
		writeError(w, http.StatusConflict, "workspace_exists", "the workspace exists with another owner")
	case err != nil:
		s.internal(w, r, err)
	case created:
		writeJSON(w, http.StatusCreated, toJSON(ws))
	default:
		writeJSON(w, http.StatusOK, toJSON(ws))
	}
}

func (s *Server) getWorkspace(w http.ResponseWriter, r *http.Request, tenant string) {
	ws, err := s.Store.GetWorkspace(r.Context(), tenant, r.PathValue("w"))
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, "unknown_workspace", "")
	case err != nil:
		s.internal(w, r, err)
	default:
		writeJSON(w, http.StatusOK, toJSON(ws))
	}
}

func (s *Server) listWorkspaces(w http.ResponseWriter, r *http.Request, tenant string) {
	q := r.URL.Query()
	after := ""
	if c := q.Get("cursor"); c != "" {
		b, err := base64.RawURLEncoding.DecodeString(c)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid_request", "cursor is not one this API returned")
			return
		}
		after = string(b)
	}
	f := store.Filter{Owner: q.Get("owner"), Phase: q.Get("phase"), ForkedFrom: q.Get("forked_from")}
	list, more, err := s.Store.ListWorkspaces(r.Context(), tenant, f, after, pageSize)
	if err != nil {
		s.internal(w, r, err)
		return
	}
	out := struct {
		Workspaces []workspaceJSON `json:"workspaces"`
		Next       *string         `json:"next"`
	}{Workspaces: []workspaceJSON{}}
	for _, ws := range list {
		out.Workspaces = append(out.Workspaces, toJSON(ws))
	}
	if more {
		n := base64.RawURLEncoding.EncodeToString([]byte(list[len(list)-1].Name))
		out.Next = &n
	}
	writeJSON(w, http.StatusOK, out)
}

// ---------------------------------------------------------------- conversations

// route finds the workspace's driver, answering the error itself when there
// is none: 404 unknown_workspace, or 409 workspace_busy when it is not
// active (M1 has no read-only projection, so reads are refused too).
func (s *Server) route(w http.ResponseWriter, r *http.Request, tenant string) (driverapi.Target, bool) {
	name := r.PathValue("w")
	t, err := s.Router.Route(r.Context(), tenant, name)
	switch {
	case errors.Is(err, store.ErrNotFound):
		writeError(w, http.StatusNotFound, "unknown_workspace", "")
	case errors.Is(err, workspace.ErrBusy):
		writeError(w, http.StatusConflict, "workspace_busy", "the workspace is not active")
	case err != nil:
		s.internal(w, r, err)
	default:
		return t, true
	}
	return t, false
}

// conversation returns the escaped driver path of the conversation in the
// request, or answers 400 for an id containing "/".
func conversation(w http.ResponseWriter, r *http.Request) (string, bool) {
	c := r.PathValue("c")
	if c == "" || strings.Contains(c, "/") {
		writeError(w, http.StatusBadRequest, "invalid_request", "a conversation id cannot contain /")
		return "", false
	}
	return "/v1/conversations/" + url.PathEscape(c), true
}

func (s *Server) createConversation(w http.ResponseWriter, r *http.Request, tenant string) {
	var body struct {
		Key string `json:"key"`
	}
	if !decode(w, r, &body) {
		return
	}
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	out := map[string]string{}
	if body.Key != "" {
		out["key"] = body.Key
	}
	s.forward(w, r, t, http.MethodPost, "/v1/conversations", nil, out)
}

func (s *Server) listConversations(w http.ResponseWriter, r *http.Request, tenant string) {
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	s.forward(w, r, t, http.MethodGet, "/v1/conversations", pick(r.URL.Query(), "key", "active", "cursor"), nil)
}

// deliveries maps the public delivery onto the conversation interface's
// whenBusy.
var deliveries = map[string]string{"queue": "follow_up", "steer": "steer"}

func (s *Server) sendMessage(w http.ResponseWriter, r *http.Request, tenant string) {
	var body struct {
		ID       string  `json:"id"`
		Text     *string `json:"text"`
		Delivery string  `json:"delivery"`
	}
	if !decode(w, r, &body) {
		return
	}
	if body.ID == "" || body.Text == nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "id and text are required")
		return
	}
	if body.Delivery == "" {
		body.Delivery = "queue"
	}
	whenBusy, ok := deliveries[body.Delivery]
	if !ok {
		writeError(w, http.StatusBadRequest, "invalid_request", `delivery is "queue" or "steer"`)
		return
	}
	path, ok := conversation(w, r)
	if !ok {
		return
	}
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	s.forward(w, r, t, http.MethodPost, path+"/messages", nil, map[string]string{
		"requestId": body.ID, "content": *body.Text, "whenBusy": whenBusy,
	})
}

func (s *Server) entries(w http.ResponseWriter, r *http.Request, tenant string) {
	path, ok := conversation(w, r)
	if !ok {
		return
	}
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	s.forward(w, r, t, http.MethodGet, path+"/entries", pick(r.URL.Query(), "after", "limit"), nil)
}

func (s *Server) interrupt(w http.ResponseWriter, r *http.Request, tenant string) {
	path, ok := conversation(w, r)
	if !ok {
		return
	}
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	s.forward(w, r, t, http.MethodPost, path+"/abort", nil, map[string]string{})
}

func (s *Server) reset(w http.ResponseWriter, r *http.Request, tenant string) {
	var body struct {
		Note string `json:"note"`
	}
	if !decode(w, r, &body) {
		return
	}
	path, ok := conversation(w, r)
	if !ok {
		return
	}
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	out := map[string]string{}
	if body.Note != "" {
		out["note"] = body.Note
	}
	s.forward(w, r, t, http.MethodPost, path+"/reset", nil, out)
}

// events streams the conversation's SSE from the driver unbuffered: each read
// from the driver is written and flushed at once, and a client that goes away
// cancels the request to the driver.
func (s *Server) events(w http.ResponseWriter, r *http.Request, tenant string) {
	path, ok := conversation(w, r)
	if !ok {
		return
	}
	t, ok := s.route(w, r, tenant)
	if !ok {
		return
	}
	req, err := t.NewRequest(r.Context(), http.MethodGet, path+"/stream", pick(r.URL.Query(), "after"), nil)
	if err != nil {
		s.internal(w, r, err)
		return
	}
	req.Header.Set("Accept", "text/event-stream")
	if id := r.Header.Get("Last-Event-ID"); id != "" {
		req.Header.Set("Last-Event-ID", id)
	}
	resp, err := s.HTTP.Do(req)
	if err != nil {
		s.unreachable(w, r, err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK || !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		s.relay(w, r, resp)
		return
	}
	h := w.Header()
	h.Set("Content-Type", "text/event-stream")
	h.Set("Cache-Control", "no-cache")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	rc := http.NewResponseController(w)
	_ = rc.Flush()
	buf := make([]byte, 32<<10)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if rc.Flush() != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

// forward sends one request to the driver and relays its answer.
func (s *Server) forward(w http.ResponseWriter, r *http.Request, t driverapi.Target, method, path string, query url.Values, body any) {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			s.internal(w, r, err)
			return
		}
		rd = bytes.NewReader(b)
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Minute)
	defer cancel()
	req, err := t.NewRequest(ctx, method, path, query, rd)
	if err != nil {
		s.internal(w, r, err)
		return
	}
	resp, err := s.HTTP.Do(req)
	if err != nil {
		s.unreachable(w, r, err)
		return
	}
	defer resp.Body.Close()
	s.relay(w, r, resp)
}

// relay passes a driver answer through: success bodies unchanged, errors as
// {error, detail} with their status. An answer that is not the driver's or
// the host's (the provider's proxy when nothing listens, a 401 for a token
// the driver no longer holds) means the driver is not reachable: 503.
func (s *Server) relay(w http.ResponseWriter, r *http.Request, resp *http.Response) {
	body, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		s.unreachable(w, r, err)
		return
	}
	if resp.StatusCode < 300 {
		ct := resp.Header.Get("Content-Type")
		if ct == "" {
			ct = "application/json"
		}
		w.Header().Set("Content-Type", ct)
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(body)
		return
	}
	var e struct {
		Error  string `json:"error"`
		Detail string `json:"detail"`
	}
	if resp.StatusCode == http.StatusUnauthorized || json.Unmarshal(body, &e) != nil || e.Error == "" {
		s.unreachable(w, r, errors.New("driver answered "+resp.Status))
		return
	}
	writeError(w, resp.StatusCode, e.Error, e.Detail)
}

func (s *Server) unreachable(w http.ResponseWriter, r *http.Request, err error) {
	if r.Context().Err() != nil {
		return // the caller went away
	}
	s.logf("%s %s: driver unreachable: %v", r.Method, r.URL.Path, err)
	writeError(w, http.StatusServiceUnavailable, "not_ready", "the workspace's driver is not reachable")
}

func (s *Server) internal(w http.ResponseWriter, r *http.Request, err error) {
	s.logf("%s %s: %v", r.Method, r.URL.Path, err)
	writeError(w, http.StatusInternalServerError, "internal", "")
}

func (s *Server) logf(format string, args ...any) {
	if s.Log != nil {
		s.Log.Printf(format, args...)
	}
}

// pick keeps only the named query parameters.
func pick(q url.Values, names ...string) url.Values {
	out := url.Values{}
	for _, n := range names {
		if v, ok := q[n]; ok {
			out[n] = v
		}
	}
	return out
}

// decode reads an optional JSON object body.
func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	b, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "body: "+err.Error())
		return false
	}
	if len(bytes.TrimSpace(b)) == 0 {
		return true
	}
	if err := json.Unmarshal(b, v); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "body: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal", "")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(append(b, '\n'))
}

func writeError(w http.ResponseWriter, status int, code, detail string) {
	body := map[string]string{"error": code}
	if detail != "" {
		body["detail"] = detail
	}
	b, _ := json.Marshal(body)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(append(b, '\n'))
}
