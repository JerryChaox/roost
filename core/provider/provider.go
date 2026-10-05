// Package provider is roost's provider-neutral view of a sandbox provider
// (contracts §9). Everything specific to one provider (its API, metadata
// encoding, auth, template build protocol) stays in its own package.
package provider

import (
	"context"
	"errors"
	"time"
)

// Labels every sandbox carries (contracts §9).
const (
	LabelTenant    = "roost.tenant"
	LabelWorkspace = "roost.workspace"
)

// ErrNotFound is returned for a sandbox or template the provider does not
// know.
var ErrNotFound = errors.New("not found")

// State is a sandbox's lifecycle state as the provider reports it. roost
// never stores it (invariant 11).
type State string

const (
	Running State = "running"
	Paused  State = "paused"
)

// Sandbox is one sandbox as the provider lists it.
type Sandbox struct {
	ID        string
	State     State
	Labels    map[string]string
	StartedAt time.Time
	EndAt     time.Time // when the sandbox times out (and pauses) unless extended
}

// CreateSpec describes a new sandbox.
type CreateSpec struct {
	Template string            // template name, as returned by FindTemplate or BuildTemplate
	Labels   map[string]string // must include LabelTenant and LabelWorkspace
	Timeout  time.Duration     // the sandbox pauses (never dies) after this long unless extended
	// HeaderRules injects headers into outbound HTTPS requests by host: the
	// provider's egress proxy sets them, so the values never enter the
	// sandbox (contracts §6, E2B Cloud). Host → header name → value.
	HeaderRules map[string]map[string]string
}

// Process is a command to run in a sandbox.
type Process struct {
	Argv  []string
	Env   map[string]string // never secret
	User  string            // empty: the provider's default user
	Stdin []byte            // Spawn only: written to the process's standard input after it starts
}

// ExecResult is the outcome of a process run to completion.
type ExecResult struct {
	ExitCode int
	Stdout   []byte
	Stderr   []byte
}

// TemplateSpec is a template definition. Files referenced by Copy steps are
// read from Dir.
type TemplateSpec struct {
	Name     string // the alias; FindTemplate finds it by this name
	Image    string // base image
	CPU      int
	MemoryMB int
	Dir      string // local directory Copy sources are relative to
	Steps    []Step
}

// Step is one template build step: exactly one of Run or Copy is set.
type Step struct {
	Run  string // shell command
	Copy *Copy
	User string // the user the step runs as, or owns the copied files
}

// Copy copies Src (a file or directory, relative to TemplateSpec.Dir) to the
// absolute path Dest in the image.
type Copy struct {
	Src  string
	Dest string
}

// Provider is a sandbox provider (contracts §9).
type Provider interface {
	// Create starts a sandbox from a template and returns its id.
	Create(ctx context.Context, spec CreateSpec) (string, error)
	// Get returns one sandbox, or ErrNotFound if it no longer exists.
	Get(ctx context.Context, id string) (Sandbox, error)
	// List returns every running or paused sandbox carrying all of labels,
	// across as many pages as the provider needs.
	List(ctx context.Context, labels map[string]string) ([]Sandbox, error)
	// Connect resumes a paused sandbox, and sets its timeout.
	Connect(ctx context.Context, id string, timeout time.Duration) error
	// Pause pauses a sandbox, keeping its memory.
	Pause(ctx context.Context, id string) error
	// Reboot pauses a sandbox without keeping its memory and resumes it: a
	// cold boot from its own disk.
	Reboot(ctx context.Context, id string, timeout time.Duration) error
	// Kill removes a sandbox. Removing one that no longer exists succeeds.
	Kill(ctx context.Context, id string) error
	// SetTimeout makes a running sandbox pause timeout from now.
	SetTimeout(ctx context.Context, id string, timeout time.Duration) error

	// Exec runs a process to completion and returns its output.
	Exec(ctx context.Context, id string, p Process) (ExecResult, error)
	// Spawn starts a process that outlives the call, writes p.Stdin to its
	// standard input and returns its pid without waiting for it.
	Spawn(ctx context.Context, id string, p Process) (int, error)
	// DefaultUser is the non-root user processes run as by default.
	DefaultUser() string

	// Endpoint returns the base URL of a sandbox port.
	Endpoint(id string, port int) string

	// FindTemplate reports whether a template with this name has a build
	// that sandboxes can be created from.
	FindTemplate(ctx context.Context, name string) (bool, error)
	// BuildTemplate builds a template and waits until it is ready.
	BuildTemplate(ctx context.Context, spec TemplateSpec) error
}
