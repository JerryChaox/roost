// Package config loads roost.yaml (contracts §13). Unknown keys are errors.
// Secrets appear in the file only as secret://<name> references; M1 resolves
// a reference from the environment variable ROOST_SECRET_<NAME>, and a
// reference that does not resolve fails loading.
package config

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// Config is the part of roost.yaml M1 acts on, with secrets resolved.
type Config struct {
	Server    Server
	Provider  Provider
	Agent     Agent
	Models    Models
	Tenants   map[string]Tenant
	Directory string // the directory of the config file
}

type Server struct {
	Listen   string
	Database string // path of the SQLite database file
}

type Provider struct {
	APIURL string
	APIKey string // resolved secret
}

type Agent struct {
	Model    string // <provider>/<model id>
	Thinking string
	// SystemPrompt.Append is the content of the file named by append, or nil.
	SystemPrompt SystemPrompt
	ModelInfo    *ModelInfo // nil when not configured
}

// ModelInfo describes a model the agent's own catalog may not know.
type ModelInfo struct {
	ContextWindow   int      `json:"contextWindow"`
	MaxOutputTokens int      `json:"maxOutputTokens"`
	Reasoning       bool     `json:"reasoning"`
	Input           []string `json:"input"`
}

type SystemPrompt struct {
	Base   string  `json:"base"`
	Append *string `json:"append"`
}

type Models struct {
	Access    string
	Providers map[string]ModelProvider
}

// ModelProvider is one model provider reached through E2B's header injection.
type ModelProvider struct {
	BaseURL string
	Key     string // resolved secret
}

type Tenant struct {
	APIKeys []string // resolved secrets
}

// DefaultBaseURLs are the official endpoints of the model providers M1
// supports.
var DefaultBaseURLs = map[string]string{
	"anthropic": "https://api.anthropic.com",
	"openai":    "https://api.openai.com/v1",
}

var (
	tenantName = regexp.MustCompile(`^[a-z0-9-]{1,63}$`)
	secretName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)
)

// The file's shape. Every key contracts §13 documents is declared so that it
// is accepted; M1 ignores those it does not act on.
type file struct {
	Server    *fileServer           `yaml:"server"`
	Workspace *fileWorkspace        `yaml:"workspace"`
	Agent     *fileAgent            `yaml:"agent"`
	Run       *fileRun              `yaml:"run"`
	Backup    *fileBackup           `yaml:"backup"`
	Models    *fileModels           `yaml:"models"`
	Tenants   map[string]fileTenant `yaml:"tenants"`
}

type fileServer struct {
	Listen   string `yaml:"listen"`
	Database string `yaml:"database"`
}

type fileWorkspace struct {
	Provider    *fileProvider `yaml:"provider"`
	Kits        []string      `yaml:"kits"`
	SleepAfter  string        `yaml:"sleep_after"`
	RecoverWait string        `yaml:"recover_wait"`
	QuiesceWait string        `yaml:"quiesce_wait"`
}

type fileProvider struct {
	APIURL string `yaml:"api_url"`
	APIKey string `yaml:"api_key"`
}

type fileAgent struct {
	Model        string            `yaml:"model"`
	Thinking     string            `yaml:"thinking"`
	SystemPrompt *fileSystemPrompt `yaml:"system_prompt"`
	ModelInfo    *fileModelInfo    `yaml:"model_info"`
}

type fileModelInfo struct {
	ContextWindow   int      `yaml:"context_window"`
	MaxOutputTokens int      `yaml:"max_output_tokens"`
	Reasoning       bool     `yaml:"reasoning"`
	Input           []string `yaml:"input"`
}

type fileSystemPrompt struct {
	Base   string `yaml:"base"`
	Append string `yaml:"append"`
}

type fileRun struct {
	Watchdog *struct {
		Stall   string `yaml:"stall"`
		Ceiling string `yaml:"ceiling"`
	} `yaml:"watchdog"`
	Retries *struct {
		RestartDriver int `yaml:"restart_driver"`
		RebootSandbox int `yaml:"reboot_sandbox"`
	} `yaml:"retries"`
}

type fileBackup struct {
	Repository       string         `yaml:"repository"`
	SnapshotInterval string         `yaml:"snapshot_interval"`
	Retention        map[string]any `yaml:"retention"` // a kopia retention policy
	API              *struct {
		URL   string `yaml:"url"`
		Token string `yaml:"token"`
	} `yaml:"api"`
}

type fileModels struct {
	Access  string `yaml:"access"`
	Gateway *struct {
		URL      string `yaml:"url"`
		AdminKey string `yaml:"admin_key"`
	} `yaml:"gateway"`
	Providers map[string]fileModelProvider `yaml:"providers"`
}

type fileModelProvider struct {
	BaseURL string `yaml:"base_url"`
	Key     string `yaml:"key"`
}

type fileTenant struct {
	APIKeys []string `yaml:"api_keys"`
	Quotas  *struct {
		MaxWorkspaces       int `yaml:"max_workspaces"`
		MaxRunningSandboxes int `yaml:"max_running_sandboxes"`
	} `yaml:"quotas"`
	Backup *struct {
		Password   string `yaml:"password"`
		Repository string `yaml:"repository"`
	} `yaml:"backup"`
}

// Load reads and validates the config file at path, resolving secrets with
// os.Getenv.
func Load(path string) (*Config, error) {
	return LoadWith(path, os.Getenv)
}

// LoadWith is Load with the environment lookup given.
func LoadWith(path string, getenv func(string) string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	var f file
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	if err := dec.Decode(&f); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("config %s: empty", path)
		}
		return nil, fmt.Errorf("config %s: %w", path, err)
	}
	c, err := resolve(&f, filepath.Dir(abs), getenv)
	if err != nil {
		return nil, fmt.Errorf("config %s: %w", path, err)
	}
	return c, nil
}

func resolve(f *file, dir string, getenv func(string) string) (*Config, error) {
	c := &Config{Directory: dir, Tenants: map[string]Tenant{}}
	secret := func(field, ref string) (string, error) { return resolveSecret(field, ref, getenv) }

	// server
	if f.Server == nil || f.Server.Listen == "" {
		return nil, errors.New("server.listen is required")
	}
	c.Server.Listen = f.Server.Listen
	db, err := databasePath(f.Server.Database, dir)
	if err != nil {
		return nil, err
	}
	c.Server.Database = db

	// workspace
	if f.Workspace == nil || f.Workspace.Provider == nil {
		return nil, errors.New("workspace.provider is required")
	}
	if len(f.Workspace.Kits) > 0 {
		return nil, errors.New("workspace.kits: Kits are not supported yet; leave it empty")
	}
	u, err := url.Parse(f.Workspace.Provider.APIURL)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return nil, fmt.Errorf("workspace.provider.api_url: %q is not an http(s) URL", f.Workspace.Provider.APIURL)
	}
	c.Provider.APIURL = f.Workspace.Provider.APIURL
	if c.Provider.APIKey, err = secret("workspace.provider.api_key", f.Workspace.Provider.APIKey); err != nil {
		return nil, err
	}

	// models
	if f.Models == nil {
		return nil, errors.New("models is required")
	}
	if f.Models.Access != "e2b" {
		return nil, fmt.Errorf("models.access: %q is not supported; this version supports e2b", f.Models.Access)
	}
	c.Models.Access = f.Models.Access
	if len(f.Models.Providers) == 0 {
		return nil, errors.New("models.providers: at least one provider is required")
	}
	c.Models.Providers = map[string]ModelProvider{}
	hosts := map[string]string{}
	for _, name := range sortedKeys(f.Models.Providers) {
		p := f.Models.Providers[name]
		field := "models.providers." + name
		def, ok := DefaultBaseURLs[name]
		if !ok {
			return nil, fmt.Errorf("%s: unsupported provider; supported: anthropic, openai", field)
		}
		base := p.BaseURL
		if base == "" {
			base = def
		}
		bu, err := url.Parse(base)
		if err != nil || bu.Scheme != "https" || bu.Hostname() == "" {
			// E2B injects headers into HTTPS requests only.
			return nil, fmt.Errorf("%s.base_url: %q is not an https URL", field, base)
		}
		if other, dup := hosts[bu.Hostname()]; dup {
			return nil, fmt.Errorf("%s.base_url: host %s is already used by %s; one host carries one provider's key", field, bu.Hostname(), other)
		}
		hosts[bu.Hostname()] = name
		key, err := secret(field+".key", p.Key)
		if err != nil {
			return nil, err
		}
		c.Models.Providers[name] = ModelProvider{BaseURL: strings.TrimRight(base, "/"), Key: key}
	}

	// agent
	if f.Agent == nil {
		return nil, errors.New("agent is required")
	}
	prov, model, ok := strings.Cut(f.Agent.Model, "/")
	if !ok || prov == "" || model == "" {
		return nil, fmt.Errorf("agent.model: %q is not <provider>/<model>", f.Agent.Model)
	}
	if _, ok := c.Models.Providers[prov]; !ok {
		return nil, fmt.Errorf("agent.model: provider %q is not in models.providers", prov)
	}
	c.Agent.Model = f.Agent.Model
	// The thinking level means what the agent says it means; it is passed on.
	if f.Agent.Thinking == "" {
		return nil, errors.New("agent.thinking is required")
	}
	c.Agent.Thinking = f.Agent.Thinking
	c.Agent.SystemPrompt.Base = "pi"
	if sp := f.Agent.SystemPrompt; sp != nil {
		if sp.Base != "" {
			if sp.Base != "pi" && sp.Base != "none" {
				return nil, fmt.Errorf("agent.system_prompt.base: %q is not pi or none", sp.Base)
			}
			c.Agent.SystemPrompt.Base = sp.Base
		}
		if sp.Append != "" {
			p, err := filePath("agent.system_prompt.append", sp.Append, dir)
			if err != nil {
				return nil, err
			}
			b, err := os.ReadFile(p)
			if err != nil {
				return nil, fmt.Errorf("agent.system_prompt.append: %w", err)
			}
			s := string(b)
			c.Agent.SystemPrompt.Append = &s
		}
	}

	if mi := f.Agent.ModelInfo; mi != nil {
		if mi.ContextWindow <= 0 || mi.MaxOutputTokens <= 0 {
			return nil, errors.New("agent.model_info: context_window and max_output_tokens must be positive")
		}
		if len(mi.Input) == 0 {
			return nil, errors.New("agent.model_info.input: at least one of text, image is required")
		}
		for _, in := range mi.Input {
			if in != "text" && in != "image" {
				return nil, fmt.Errorf("agent.model_info.input: %q is not text or image", in)
			}
		}
		c.Agent.ModelInfo = &ModelInfo{ContextWindow: mi.ContextWindow, MaxOutputTokens: mi.MaxOutputTokens, Reasoning: mi.Reasoning, Input: mi.Input}
	}

	// tenants
	if len(f.Tenants) == 0 {
		return nil, errors.New("tenants: at least one tenant is required")
	}
	seen := map[string]string{}
	for _, name := range sortedKeys(f.Tenants) {
		t := f.Tenants[name]
		if !tenantName.MatchString(name) {
			return nil, fmt.Errorf("tenants.%s: a tenant name is [a-z0-9-]{1,63}", name)
		}
		if len(t.APIKeys) == 0 {
			return nil, fmt.Errorf("tenants.%s.api_keys: at least one key is required", name)
		}
		var keys []string
		for i, ref := range t.APIKeys {
			k, err := secret(fmt.Sprintf("tenants.%s.api_keys[%d]", name, i), ref)
			if err != nil {
				return nil, err
			}
			if other, dup := seen[k]; dup {
				return nil, fmt.Errorf("tenants.%s.api_keys[%d]: the same key is also a key of tenant %s", name, i, other)
			}
			seen[k] = name
			keys = append(keys, k)
		}
		c.Tenants[name] = Tenant{APIKeys: keys}
	}
	return c, nil
}

// SecretEnv is the environment variable a secret://<name> reference
// resolves from.
func SecretEnv(name string) string {
	return "ROOST_SECRET_" + strings.ToUpper(strings.ReplaceAll(name, "-", "_"))
}

func resolveSecret(field, ref string, getenv func(string) string) (string, error) {
	name, ok := strings.CutPrefix(ref, "secret://")
	if !ok {
		if ref == "" {
			return "", fmt.Errorf("%s is required (a secret://<name> reference)", field)
		}
		return "", fmt.Errorf("%s: secrets are given as secret://<name> references, never as values", field)
	}
	if !secretName.MatchString(name) {
		return "", fmt.Errorf("%s: %q is not a valid secret name", field, name)
	}
	v := getenv(SecretEnv(name))
	if v == "" {
		return "", fmt.Errorf("%s: secret://%s does not resolve: %s is not set", field, name, SecretEnv(name))
	}
	return v, nil
}

// filePath resolves a file:// reference relative to the config directory.
func filePath(field, ref, dir string) (string, error) {
	p, ok := strings.CutPrefix(ref, "file://")
	if !ok || p == "" {
		return "", fmt.Errorf("%s: %q is not a file:// reference", field, ref)
	}
	if !filepath.IsAbs(p) {
		p = filepath.Join(dir, p)
	}
	return p, nil
}

// databasePath reads sqlite://<path>; a relative path is relative to the
// config directory (sqlite:///var/lib/roost/roost.db is absolute).
func databasePath(ref, dir string) (string, error) {
	p, ok := strings.CutPrefix(ref, "sqlite://")
	if !ok || p == "" {
		return "", fmt.Errorf("server.database: %q is not a sqlite://<path> URL; this version supports SQLite only", ref)
	}
	if !filepath.IsAbs(p) {
		p = filepath.Join(dir, p)
	}
	return p, nil
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
