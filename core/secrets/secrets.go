// Package secrets mints and revokes the model credential of each execution
// grant (contracts §6). Config secrets (secret:// references) are a different
// thing and live in core/config.
package secrets

import (
	"context"
	"errors"
	"net/url"
	"sort"

	"github.com/JerryChaox/roost/core/config"
)

// ErrUnsupported is returned by a SecretProvider that cannot do what was
// asked; it is final, not worth retrying.
var ErrUnsupported = errors.New("unsupported by this model access")

// GrantRef names the grant a credential is for.
type GrantRef struct {
	ID        string
	Tenant    string
	Workspace string
}

// Credential is what a grant's agent host uses to call models: the providers'
// base URLs and a key. Ref identifies the key for revocation ("" if there is
// nothing to revoke).
type Credential struct {
	BaseURLs map[string]string
	Value    string
	Ref      string
}

// SecretProvider mints a grant's model credential and revokes it.
type SecretProvider interface {
	Issue(ctx context.Context, g GrantRef) (Credential, error)
	Revoke(ctx context.Context, ref string) error // may return ErrUnsupported
}

// Placeholder is the key the agent host sends on E2B Cloud; E2B's egress
// proxy replaces it with the real one.
const Placeholder = "roost-e2b-injected"

// E2B is the model access of E2B Cloud (models.access: e2b): the agent gets
// a fixed placeholder, and the real keys reach requests only through E2B's
// header injection, configured when the sandbox is created (HeaderRules).
type E2B struct {
	providers map[string]config.ModelProvider
}

func NewE2B(providers map[string]config.ModelProvider) *E2B {
	return &E2B{providers: providers}
}

func (e *E2B) Issue(_ context.Context, _ GrantRef) (Credential, error) {
	urls := map[string]string{}
	for name, p := range e.providers {
		urls[name] = p.BaseURL
	}
	return Credential{BaseURLs: urls, Value: Placeholder}, nil
}

// Revoke is unsupported: E2B Cloud can only drop the injection for the whole
// sandbox, which also cuts off the next grant on it.
func (e *E2B) Revoke(context.Context, string) error { return ErrUnsupported }

// HeaderRules returns the headers E2B's egress proxy injects for each
// configured provider's base-URL host: x-api-key for anthropic,
// Authorization: Bearer for openai. They are set when a sandbox is created.
func (e *E2B) HeaderRules() (map[string]map[string]string, error) {
	rules := map[string]map[string]string{}
	names := make([]string, 0, len(e.providers))
	for n := range e.providers {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, name := range names {
		p := e.providers[name]
		u, err := url.Parse(p.BaseURL)
		if err != nil {
			return nil, err
		}
		switch name {
		case "anthropic":
			rules[u.Hostname()] = map[string]string{"x-api-key": p.Key}
		case "openai":
			rules[u.Hostname()] = map[string]string{"authorization": "Bearer " + p.Key}
		default:
			return nil, errors.New("no header injection known for model provider " + name)
		}
	}
	return rules, nil
}
