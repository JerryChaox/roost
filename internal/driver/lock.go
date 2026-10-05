//go:build unix

package driver

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// lockStorage takes the exclusive lock on <storage>/lock (driver-protocol §2
// step 3). The lock lasts as long as the returned file stays open; os opens
// it close-on-exec, so the agent host never inherits it.
func lockStorage(storage string) (*os.File, error) {
	path := filepath.Join(storage, "lock")
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, fmt.Errorf("agent storage lock: %w", err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, fmt.Errorf("agent storage lock %s is held by another process (another roost-driver is running on this storage)", path)
		}
		return nil, fmt.Errorf("agent storage lock %s: %w", path, err)
	}
	return f, nil
}
