//go:build unix && !linux

package driver

import "syscall"

// hostSysProcAttr puts the agent host in its own process group. Outside Linux
// there is no parent-death signal: the driver kills the host when it stops,
// and the host is expected to exit when its control channel closes.
func hostSysProcAttr(cred *syscall.Credential) *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true, Credential: cred}
}
