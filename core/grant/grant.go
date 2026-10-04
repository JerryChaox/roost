// Package grant builds execution grants (driver-protocol §1): their ids,
// tokens, and the JSON object the driver reads on its standard input.
package grant

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"

	"github.com/oklog/ulid/v2"

	"github.com/JerryChaox/roost/core/secrets"
)

// Protocol is the grant object's version (driver-protocol §2).
const Protocol = 1

// NewID returns a new grant id, which is also the grant's start: g_<ulid>.
func NewID() string { return "g_" + ulid.Make().String() }

// NewToken returns 32 random bytes, base64url without padding.
func NewToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		panic(err) // crypto/rand does not fail on supported platforms
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// Tokens returns a driver token and a backup token that differ.
func Tokens() (driver, backup string) {
	for {
		driver, backup = NewToken(), NewToken()
		if driver != backup {
			return driver, backup
		}
	}
}

// Agent is the agent section of a grant: the workspace's runtime
// configuration for the agent host.
type Agent struct {
	Model        string       `json:"model"`
	Thinking     string       `json:"thinking"`
	SystemPrompt SystemPrompt `json:"systemPrompt"`
	ModelInfo    *ModelInfo   `json:"modelInfo,omitempty"` // omitted when not configured
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
	Append *string `json:"append"` // the appended text, or null
}

type models struct {
	BaseURLs   map[string]string `json:"baseUrls"`
	Credential string            `json:"credential"`
}

type object struct {
	Protocol    int    `json:"protocol"`
	Start       string `json:"start"`
	DriverToken string `json:"driverToken"`
	BackupToken string `json:"backupToken"`
	Models      models `json:"models"`
	Agent       Agent  `json:"agent"`
}

// JSON returns the grant object for the driver's standard input, followed by
// a newline. It carries secrets: it goes to the driver and nowhere else.
func JSON(start, driverToken, backupToken string, cred secrets.Credential, agent Agent) []byte {
	b, err := json.Marshal(object{
		Protocol:    Protocol,
		Start:       start,
		DriverToken: driverToken,
		BackupToken: backupToken,
		Models:      models{BaseURLs: cred.BaseURLs, Credential: cred.Value},
		Agent:       agent,
	})
	if err != nil {
		panic(err) // plain strings and maps always encode
	}
	return append(b, '\n')
}
