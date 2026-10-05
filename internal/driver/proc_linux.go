package driver

import "syscall"

// hostSysProcAttr puts the agent host in its own process group and has the
// kernel kill it when the driver goes away. Pdeathsig fires when the thread
// that started the child exits, so the host is started and waited for on one
// locked OS thread (see spawnHost).
func hostSysProcAttr(cred *syscall.Credential) *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL, Credential: cred}
}
