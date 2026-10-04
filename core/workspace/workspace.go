// Package workspace is the workspace lifecycle seen from the API: creating,
// reading and listing workspaces, and routing a request to the driver of a
// workspace's live grant.
package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"

	"github.com/JerryChaox/roost/core/config"
	"github.com/JerryChaox/roost/core/driverapi"
	"github.com/JerryChaox/roost/core/grant"
	"github.com/JerryChaox/roost/core/provider"
	"github.com/JerryChaox/roost/core/sandbox"
	"github.com/JerryChaox/roost/core/store"
)

var (
	ErrInvalidName = errors.New("a workspace name is [a-z0-9-_.]{1,128}")
	ErrBusy        = errors.New("the workspace is not active")
)

var nameRE = regexp.MustCompile(`^[a-z0-9\-_.]{1,128}$`)

// ValidName reports whether name is a valid workspace name (contracts §1).
func ValidName(name string) bool { return nameRE.MatchString(name) }

// RuntimeConfig is the resolved configuration a workspace is created with
// and runs with. It holds no secret.
type RuntimeConfig struct {
	Agent  grant.Agent `json:"agent"`
	Models struct {
		BaseURLs map[string]string `json:"baseUrls"`
	} `json:"models"`
}

// RuntimeConfigOf resolves the runtime configuration new workspaces get.
func RuntimeConfigOf(c *config.Config) RuntimeConfig {
	var rc RuntimeConfig
	rc.Agent = grant.Agent{
		Model:    c.Agent.Model,
		Thinking: c.Agent.Thinking,
		SystemPrompt: grant.SystemPrompt{
			Base:   c.Agent.SystemPrompt.Base,
			Append: c.Agent.SystemPrompt.Append,
		},
	}
	if mi := c.Agent.ModelInfo; mi != nil {
		rc.Agent.ModelInfo = &grant.ModelInfo{ContextWindow: mi.ContextWindow, MaxOutputTokens: mi.MaxOutputTokens, Reasoning: mi.Reasoning, Input: mi.Input}
	}
	rc.Models.BaseURLs = map[string]string{}
	for name, p := range c.Models.Providers {
		rc.Models.BaseURLs[name] = p.BaseURL
	}
	return rc
}

// Service answers workspace requests.
type Service struct {
	Store         *store.Store
	Provider      provider.Provider
	RuntimeConfig RuntimeConfig
	// Changed is called after a workspace is created, to reconcile it now.
	Changed func(tenant, name string)
}

// Create creates the workspace in provisioning, or returns it if it exists
// with the same owner (created false). It fails with
// store.ErrWorkspaceExists if it exists with another owner.
func (s *Service) Create(ctx context.Context, tenant, name, owner, caller, reason string) (store.Workspace, bool, error) {
	if !ValidName(name) {
		return store.Workspace{}, false, ErrInvalidName
	}
	rc, err := json.Marshal(s.RuntimeConfig)
	if err != nil {
		return store.Workspace{}, false, err
	}
	w, created, err := s.Store.CreateWorkspace(ctx, tenant, name, owner, rc, store.Audit{
		Caller:  caller,
		Tenant:  tenant,
		Action:  "workspace.create",
		Target:  name,
		Reason:  reason,
		Request: map[string]string{"owner": owner},
		Outcome: "created",
	})
	if err == nil && created && s.Changed != nil {
		s.Changed(tenant, name)
	}
	return w, created, err
}

// Route returns the driver of an active workspace's live grant:
// store.ErrNotFound for an unknown workspace, ErrBusy for one not active.
func (s *Service) Route(ctx context.Context, tenant, name string) (driverapi.Target, error) {
	w, err := s.Store.GetWorkspace(ctx, tenant, name)
	if err != nil {
		return driverapi.Target{}, err
	}
	if w.Phase != store.Active {
		return driverapi.Target{}, ErrBusy
	}
	g, err := s.Store.LiveGrant(ctx, tenant, name)
	if errors.Is(err, store.ErrNotFound) {
		return driverapi.Target{}, ErrBusy
	}
	if err != nil {
		return driverapi.Target{}, err
	}
	return driverapi.Target{URL: s.Provider.Endpoint(g.SandboxID, sandbox.DriverPort), Token: g.DriverToken}, nil
}
