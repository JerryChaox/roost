package driver

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"runtime/debug"
)

// Fingerprint identifies the running driver binary.
type Fingerprint struct {
	Version string `json:"version"`
	SHA256  string `json:"sha256"`
}

// selfFingerprint hashes the driver's own executable. version, when empty,
// falls back to the main module version Go stamps at build time.
func selfFingerprint(version string) (Fingerprint, error) {
	if version == "" {
		if bi, ok := debug.ReadBuildInfo(); ok {
			version = bi.Main.Version
		}
	}
	exe, err := os.Executable()
	if err != nil {
		return Fingerprint{}, fmt.Errorf("fingerprint: %w", err)
	}
	f, err := os.Open(exe)
	if err != nil {
		return Fingerprint{}, fmt.Errorf("fingerprint: %w", err)
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return Fingerprint{}, fmt.Errorf("fingerprint: %w", err)
	}
	return Fingerprint{Version: version, SHA256: hex.EncodeToString(h.Sum(nil))}, nil
}
