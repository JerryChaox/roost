package driver

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// GrantProtocol is the only version of the grant object this driver reads.
const GrantProtocol = 1

// Grant is the execution grant the control plane writes to the driver's
// standard input (driver-protocol §1, §2). Models and Agent are opaque to the
// driver and reach the agent host unchanged in initialize.
type Grant struct {
	Protocol    int             `json:"protocol"`
	Start       string          `json:"start"`
	DriverToken string          `json:"driverToken"`
	BackupToken string          `json:"backupToken"`
	Models      json.RawMessage `json:"models,omitempty"`
	Agent       json.RawMessage `json:"agent,omitempty"`
}

// String keeps the tokens and the model credential out of any log line that
// formats a grant by mistake.
func (g *Grant) String() string { return "grant " + g.Start }

// GoString does the same for %#v.
func (g *Grant) GoString() string { return g.String() }

// ReadGrant reads exactly one JSON object from r. It returns (nil, nil) when
// r ends before any value: a driver without a grant answers 503 not_ready.
func ReadGrant(r io.Reader) (*Grant, error) {
	var g Grant
	if err := json.NewDecoder(r).Decode(&g); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil
		}
		// encoding/json errors name types and fields, never values.
		return nil, fmt.Errorf("execution grant on stdin: %w", err)
	}
	switch {
	case g.Protocol != GrantProtocol:
		return nil, fmt.Errorf("execution grant on stdin: unsupported protocol %d (this driver speaks %d)", g.Protocol, GrantProtocol)
	case g.Start == "":
		return nil, errors.New("execution grant on stdin: start is missing")
	case g.DriverToken == "" || g.BackupToken == "":
		return nil, errors.New("execution grant on stdin: driverToken and backupToken are required")
	case g.DriverToken == g.BackupToken:
		return nil, errors.New("execution grant on stdin: driverToken and backupToken must differ")
	}
	return &g, nil
}
