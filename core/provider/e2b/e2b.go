// Package e2b implements roost's sandbox provider on E2B (contracts §9).
// Everything specific to E2B stays here: its REST API and metadata encoding,
// envd's process API and auth, network rules, and the template build and
// file upload protocol.
package e2b

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/provider/e2b/internal/api"
)

const (
	envdPort    = 49983
	defaultUser = "user" // the non-root user E2B templates run processes as
	pageLimit   = 100    // E2B's maximum page size
)

// Provider talks to one E2B API (E2B Cloud, or E2B Embed by its URL).
type Provider struct {
	api    *api.ClientWithResponses
	http   *http.Client // REST calls and uploads
	stream *http.Client // envd streams: no overall timeout
	domain string       // sandbox traffic domain: <port>-<id>.<domain>

	mu         sync.Mutex
	envdTokens map[string]string // sandbox id → envd access token, in memory only
}

var _ provider.Provider = (*Provider)(nil)

// New returns a provider for the E2B API at apiURL. The sandbox domain is the
// API host without its "api." prefix (api.e2b.app → e2b.app).
func New(apiURL, apiKey string) (*Provider, error) {
	u, err := url.Parse(apiURL)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return nil, fmt.Errorf("e2b: api_url %q is not an absolute URL", apiURL)
	}
	domain := strings.TrimPrefix(u.Hostname(), "api.")
	p := &Provider{
		http:       &http.Client{Timeout: 2 * time.Minute},
		stream:     &http.Client{},
		domain:     domain,
		envdTokens: map[string]string{},
	}
	p.api, err = api.NewClientWithResponses(strings.TrimRight(apiURL, "/"),
		api.WithHTTPClient(p.http),
		api.WithRequestEditorFn(func(_ context.Context, r *http.Request) error {
			r.Header.Set("X-API-Key", apiKey)
			return nil
		}))
	if err != nil {
		return nil, fmt.Errorf("e2b: %w", err)
	}
	return p, nil
}

// apiError turns a non-success response into an error carrying E2B's message.
// Response bodies are never logged whole: a sandbox's detail carries its
// network rules, and they carry injected credentials.
func apiError(op string, status int, body []byte) error {
	var e struct {
		Message string `json:"message"`
	}
	msg := ""
	if json.Unmarshal(body, &e) == nil {
		msg = e.Message
	}
	if status == http.StatusNotFound {
		return fmt.Errorf("e2b %s: %w: %s", op, provider.ErrNotFound, msg)
	}
	return fmt.Errorf("e2b %s: status %d: %s", op, status, msg)
}

func (p *Provider) Create(ctx context.Context, spec provider.CreateSpec) (string, error) {
	if spec.Labels[provider.LabelTenant] == "" || spec.Labels[provider.LabelWorkspace] == "" {
		return "", errors.New("e2b create: the tenant and workspace labels are required")
	}
	timeout := int32(spec.Timeout / time.Second)
	md := api.SandboxMetadata(spec.Labels)
	public := true // the driver's tokens guard every request
	body := api.NewSandboxV2{
		TemplateID: spec.Template,
		Timeout:    &timeout,
		AutoPause:  ptr(true), // a timeout pauses, never kills
		AutoResume: &api.SandboxAutoResumeConfig{Enabled: true},
		Metadata:   &md,
		Network:    &api.SandboxNetworkConfig{AllowPublicTraffic: &public},
	}
	if len(spec.HeaderRules) > 0 {
		rules := map[string][]api.SandboxNetworkRule{}
		for host, headers := range spec.HeaderRules {
			h := map[string]string{}
			for k, v := range headers {
				h[k] = v
			}
			rules[host] = []api.SandboxNetworkRule{{Transform: &api.SandboxNetworkTransform{Headers: &h}}}
		}
		body.Network.Rules = &rules
	}
	r, err := p.api.PostV2SandboxesWithResponse(ctx, body)
	if err != nil {
		return "", fmt.Errorf("e2b create: %w", err)
	}
	if r.JSON201 == nil {
		return "", apiError("create", r.StatusCode(), r.Body)
	}
	if t := r.JSON201.EnvdAccessToken; t != nil {
		p.setEnvdToken(r.JSON201.SandboxID, *t)
	}
	return r.JSON201.SandboxID, nil
}

func (p *Provider) Get(ctx context.Context, id string) (provider.Sandbox, error) {
	d, err := p.detail(ctx, id)
	if err != nil {
		return provider.Sandbox{}, err
	}
	return sandboxOf(d.SandboxID, d.State, d.Metadata, d.StartedAt, d.EndAt), nil
}

func (p *Provider) detail(ctx context.Context, id string) (*api.SandboxDetail, error) {
	r, err := p.api.GetSandboxesSandboxIDWithResponse(ctx, id)
	if err != nil {
		return nil, fmt.Errorf("e2b get %s: %w", id, err)
	}
	if r.JSON200 == nil {
		return nil, apiError("get "+id, r.StatusCode(), r.Body)
	}
	if t := r.JSON200.EnvdAccessToken; t != nil {
		p.setEnvdToken(id, *t)
	}
	return r.JSON200, nil
}

func (p *Provider) List(ctx context.Context, labels map[string]string) ([]provider.Sandbox, error) {
	md := encodeMetadata(labels)
	states := []api.SandboxState{"running", "paused"}
	limit := api.PaginationLimit(pageLimit)
	params := &api.GetV2SandboxesParams{State: &states, Limit: &limit}
	if md != "" {
		params.Metadata = &md
	}
	var out []provider.Sandbox
	for {
		r, err := p.api.GetV2SandboxesWithResponse(ctx, params)
		if err != nil {
			return nil, fmt.Errorf("e2b list: %w", err)
		}
		if r.JSON200 == nil {
			return nil, apiError("list", r.StatusCode(), r.Body)
		}
		for _, s := range *r.JSON200 {
			out = append(out, sandboxOf(s.SandboxID, s.State, s.Metadata, s.StartedAt, s.EndAt))
		}
		next := r.HTTPResponse.Header.Get("X-Next-Token")
		if next == "" || len(*r.JSON200) == 0 {
			return out, nil
		}
		params.NextToken = &next
	}
}

// encodeMetadata builds the metadata filter: "k=v&k2=v2" with each key and
// value URL-encoded; the client encodes the whole value once more as a query
// parameter.
func encodeMetadata(m map[string]string) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(m[k]))
	}
	return strings.Join(parts, "&")
}

func sandboxOf(id string, state api.SandboxState, md *api.SandboxMetadata, started, end time.Time) provider.Sandbox {
	labels := map[string]string{}
	if md != nil {
		for k, v := range *md {
			labels[k] = v
		}
	}
	return provider.Sandbox{ID: id, State: provider.State(state), Labels: labels, StartedAt: started, EndAt: end}
}

func (p *Provider) Connect(ctx context.Context, id string, timeout time.Duration) error {
	t := int32(timeout / time.Second)
	r, err := p.api.PostV2SandboxesSandboxIDConnectWithResponse(ctx, id, api.ConnectSandboxV2{Timeout: &t})
	if err != nil {
		return fmt.Errorf("e2b connect %s: %w", id, err)
	}
	if r.StatusCode() != http.StatusOK && r.StatusCode() != http.StatusCreated {
		return apiError("connect "+id, r.StatusCode(), r.Body)
	}
	var s api.Sandbox
	if json.Unmarshal(r.Body, &s) == nil && s.EnvdAccessToken != nil {
		p.setEnvdToken(id, *s.EnvdAccessToken)
	}
	return nil
}

func (p *Provider) pause(ctx context.Context, id string, memory bool) error {
	r, err := p.api.PostSandboxesSandboxIDPauseWithResponse(ctx, id, api.SandboxPauseRequest{Memory: &memory})
	if err != nil {
		return fmt.Errorf("e2b pause %s: %w", id, err)
	}
	if r.StatusCode() != http.StatusNoContent && r.StatusCode() != http.StatusConflict { // 409: already paused
		return apiError("pause "+id, r.StatusCode(), r.Body)
	}
	return nil
}

func (p *Provider) Pause(ctx context.Context, id string) error { return p.pause(ctx, id, true) }

func (p *Provider) Reboot(ctx context.Context, id string, timeout time.Duration) error {
	if err := p.pause(ctx, id, false); err != nil {
		return err
	}
	return p.Connect(ctx, id, timeout)
}

func (p *Provider) Kill(ctx context.Context, id string) error {
	r, err := p.api.DeleteSandboxesSandboxIDWithResponse(ctx, id)
	if err != nil {
		return fmt.Errorf("e2b kill %s: %w", id, err)
	}
	switch r.StatusCode() {
	case http.StatusNoContent, http.StatusNotFound:
		p.mu.Lock()
		delete(p.envdTokens, id)
		p.mu.Unlock()
		return nil
	}
	return apiError("kill "+id, r.StatusCode(), r.Body)
}

func (p *Provider) SetTimeout(ctx context.Context, id string, timeout time.Duration) error {
	r, err := p.api.PostSandboxesSandboxIDTimeoutWithResponse(ctx, id, api.SandboxTimeoutRequest{Timeout: int32(timeout / time.Second)})
	if err != nil {
		return fmt.Errorf("e2b timeout %s: %w", id, err)
	}
	if r.StatusCode() != http.StatusNoContent {
		return apiError("timeout "+id, r.StatusCode(), r.Body)
	}
	return nil
}

func (p *Provider) DefaultUser() string { return defaultUser }

func (p *Provider) Endpoint(id string, port int) string {
	return fmt.Sprintf("https://%d-%s.%s", port, id, p.domain)
}

func (p *Provider) setEnvdToken(id, token string) {
	p.mu.Lock()
	p.envdTokens[id] = token
	p.mu.Unlock()
}

// envdToken returns the sandbox's envd access token, asking the API for the
// sandbox's detail (which does not resume it) when it is not cached.
func (p *Provider) envdToken(ctx context.Context, id string) (string, error) {
	p.mu.Lock()
	t, ok := p.envdTokens[id]
	p.mu.Unlock()
	if ok {
		return t, nil
	}
	d, err := p.detail(ctx, id)
	if err != nil {
		return "", err
	}
	if d.EnvdAccessToken == nil {
		return "", fmt.Errorf("e2b: sandbox %s has no envd access token", id)
	}
	return *d.EnvdAccessToken, nil
}

func ptr[T any](v T) *T { return &v }
