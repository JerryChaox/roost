package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const valid = `
server: { listen: 127.0.0.1:7070, database: sqlite://./roost.db }
workspace:
  provider: { api_url: https://api.e2b.app, api_key: secret://e2b }
agent:
  model: anthropic/deepseek/deepseek-v4-flash
  thinking: low
models:
  access: e2b
  providers:
    anthropic: { base_url: https://llm-gateway.example.com/anthropic, key: secret://gateway }
tenants:
  acme: { api_keys: [secret://acme-api-key] }
`

var env = map[string]string{
	"ROOST_SECRET_E2B":          "e2b-key",
	"ROOST_SECRET_GATEWAY":      "gateway-key",
	"ROOST_SECRET_ANTHROPIC":    "anthropic-key",
	"ROOST_SECRET_ACME_API_KEY": "acme-key",
}

func load(t *testing.T, yaml string, env map[string]string) (*Config, error) {
	t.Helper()
	p := filepath.Join(t.TempDir(), "roost.yaml")
	if err := os.WriteFile(p, []byte(yaml), 0o600); err != nil {
		t.Fatal(err)
	}
	return LoadWith(p, func(k string) string { return env[k] })
}

func TestLoadResolvesSecretsAndPaths(t *testing.T) {
	c, err := load(t, valid, env)
	if err != nil {
		t.Fatal(err)
	}
	if c.Provider.APIKey != "e2b-key" || c.Models.Providers["anthropic"].Key != "gateway-key" || c.Tenants["acme"].APIKeys[0] != "acme-key" {
		t.Fatalf("secrets not resolved: %+v", c)
	}
	if c.Server.Database != filepath.Join(c.Directory, "roost.db") {
		t.Fatalf("database %q is not relative to the config file", c.Server.Database)
	}
}

// The example shipped in the repository must stay loadable.
func TestExampleLoads(t *testing.T) {
	_, err := LoadWith("../../roost.example.yaml", func(k string) string { return env[k] })
	if err != nil {
		t.Fatal(err)
	}
}

func TestUnknownKeysAreErrors(t *testing.T) {
	for _, tc := range []struct{ name, yaml string }{
		{"top level", valid + "extra: 1\n"},
		{"nested", strings.Replace(valid, "thinking: low", "thinking: low\n  temperature: 0.2", 1)},
		{"tenant", strings.Replace(valid, "acme: { api_keys: [secret://acme-api-key] }", "acme: { api_keys: [secret://acme-api-key], admins: [x] }", 1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := load(t, tc.yaml, env); err == nil || !strings.Contains(err.Error(), "not found") {
				t.Fatalf("want an unknown-field error, got %v", err)
			}
		})
	}
}

func TestDocumentedKeysAreAccepted(t *testing.T) {
	yaml := valid + `run:
  watchdog: { stall: 5m, ceiling: 3h }
  retries: { restart_driver: 2, reboot_sandbox: 1 }
backup:
  repository: file:///var/lib/roost/backup
  snapshot_interval: 60s
  retention: { keep_within: 30d }
  api: { url: http://127.0.0.1:7071, token: secret://roost-backup-api }
`
	if _, err := load(t, yaml, env); err != nil {
		t.Fatal(err)
	}
}

func TestSecretsMustResolve(t *testing.T) {
	missing := map[string]string{"ROOST_SECRET_E2B": "e2b-key", "ROOST_SECRET_ACME_API_KEY": "acme-key"}
	_, err := load(t, valid, missing)
	if err == nil || !strings.Contains(err.Error(), "ROOST_SECRET_GATEWAY") {
		t.Fatalf("want an unresolvable secret://gateway, got %v", err)
	}
	literal := strings.Replace(valid, "api_key: secret://e2b", "api_key: e2b_live_value", 1)
	if _, err := load(t, literal, env); err == nil || strings.Contains(err.Error(), "e2b_live_value") {
		t.Fatalf("want a secret given as a value refused without echoing it, got %v", err)
	}
}
