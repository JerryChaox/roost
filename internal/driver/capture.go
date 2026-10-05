package driver

// SnapshotNotice is the params of the agent host's snapshot notification
// (driver-protocol §5): a step finished.
type SnapshotNotice struct {
	Conversation string `json:"conversation"`
	Run          string `json:"run"`
	Position     string `json:"position"`
}

// Capture receives the agent host's snapshot notifications. Backup capture
// (M2) implements it. HostSnapshot is called on the control channel's reader
// and must not block.
type Capture interface {
	HostSnapshot(SnapshotNotice)
}

// noCapture is M1's capture: there is nothing to record yet.
type noCapture struct{}

func (noCapture) HostSnapshot(SnapshotNotice) {}
