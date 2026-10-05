package envdhttp

// SecureToken is referenced by envd.yaml via `x-go-type: SecureToken` (the
// /init body, orchestrator-internal). Upstream's own integration client
// declares the same alias (tests/integration/internal/envd/types.go).
type SecureToken = string
