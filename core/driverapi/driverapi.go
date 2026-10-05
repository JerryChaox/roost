// Package driverapi is the control plane's client of the driver protocol
// (driver-protocol §3): how a request reaches a grant's driver, and the
// driver's own routes the reconciler reads.
package driverapi

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// ProtocolHeader carries the driver protocol version on every request.
const ProtocolHeader = "Roost-Protocol"

// Target is a grant's driver: the base URL of its sandbox endpoint and the
// grant's driver token.
type Target struct {
	URL   string
	Token string
}

// String keeps the token out of logs.
func (t Target) String() string { return t.URL }

// NewRequest builds a request to the driver with the grant's token and the
// protocol header. path must already be escaped.
func (t Target) NewRequest(ctx context.Context, method, path string, query url.Values, body io.Reader) (*http.Request, error) {
	u := t.URL + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+t.Token)
	req.Header.Set(ProtocolHeader, "1")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return req, nil
}

// NewTransport is the transport for driver requests: no compression, so
// streams pass through as they arrive.
func NewTransport() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.DisableCompression = true
	t.MaxIdleConnsPerHost = 16
	return t
}

// Client reads the driver's own routes.
type Client struct {
	HTTP    *http.Client
	Timeout time.Duration // per call
}

func NewClient() *Client {
	return &Client{HTTP: &http.Client{Transport: NewTransport()}, Timeout: 15 * time.Second}
}

// StatusError is a driver answer other than the one expected.
type StatusError struct {
	Status int
	Code   string
}

func (e *StatusError) Error() string { return fmt.Sprintf("driver answered %d %s", e.Status, e.Code) }

func (c *Client) getJSON(ctx context.Context, t Target, path string, query url.Values, v any) error {
	ctx, cancel := context.WithTimeout(ctx, c.Timeout)
	defer cancel()
	req, err := t.NewRequest(ctx, http.MethodGet, path, query, nil)
	if err != nil {
		return err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		var e struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &e)
		return &StatusError{Status: resp.StatusCode, Code: e.Error}
	}
	return json.Unmarshal(body, v)
}

// Ready reports whether the driver is ready for the grant start: its health
// answers 200 and its state names that grant.
func (c *Client) Ready(ctx context.Context, t Target, start string) error {
	var health struct {
		Driver struct {
			Version string `json:"version"`
		} `json:"driver"`
	}
	if err := c.getJSON(ctx, t, "/v1/health", nil, &health); err != nil {
		return fmt.Errorf("health: %w", err)
	}
	var state struct {
		Start  string `json:"start"`
		Status string `json:"status"`
	}
	if err := c.getJSON(ctx, t, "/v1/state", nil, &state); err != nil {
		return fmt.Errorf("state: %w", err)
	}
	if state.Start != start {
		return fmt.Errorf("state: the driver holds grant %q, not %q", state.Start, start)
	}
	if state.Status != "ready" {
		return fmt.Errorf("state: %s", state.Status)
	}
	return nil
}

// RunInProgress reports whether any conversation has a run going, from the
// conversation interface's ?active=true listing.
func (c *Client) RunInProgress(ctx context.Context, t Target) (bool, error) {
	var list struct {
		Conversations []struct {
			Active bool `json:"active"`
		} `json:"conversations"`
	}
	if err := c.getJSON(ctx, t, "/v1/conversations", url.Values{"active": {"true"}}, &list); err != nil {
		return false, err
	}
	for _, c := range list.Conversations {
		if c.Active {
			return true, nil
		}
	}
	return false, nil
}
