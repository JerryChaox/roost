package driver

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"strconv"
	"sync"
	"time"
)

// maxControlLine bounds one control-channel message. A longer line is logged
// and skipped.
const maxControlLine = 16 << 20

// RPCError is a JSON-RPC error object returned by the agent host.
type RPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *RPCError) Error() string {
	return fmt.Sprintf("agent host error %d: %s", e.Code, e.Message)
}

type rpcMessage struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *RPCError       `json:"error,omitempty"`
}

type rpcReply struct {
	result json.RawMessage
	err    error
}

type writeDeadliner interface {
	SetWriteDeadline(time.Time) error
}

// rpcConn is the driver's end of the host control channel: JSON-RPC 2.0, one
// message per line, requests written to the host's stdin and replies and
// notifications read from its stdout (driver-protocol §5).
type rpcConn struct {
	log    *log.Logger
	notify func(method string, params json.RawMessage)

	wmu sync.Mutex
	w   io.Writer

	mu      sync.Mutex
	nextID  int64
	pending map[int64]chan rpcReply
	err     error // set once the channel is closed
}

func newRPCConn(w io.Writer, logger *log.Logger, notify func(string, json.RawMessage)) *rpcConn {
	return &rpcConn{log: logger, notify: notify, w: w, pending: map[int64]chan rpcReply{}}
}

// call sends one request and waits for its reply, ctx's end, or the channel's
// close. params is omitted from the request when nil.
func (c *rpcConn) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.mu.Lock()
	if c.err != nil {
		err := c.err
		c.mu.Unlock()
		return nil, err
	}
	c.nextID++
	id := c.nextID
	ch := make(chan rpcReply, 1)
	c.pending[id] = ch
	c.mu.Unlock()

	req := struct {
		JSONRPC string `json:"jsonrpc"`
		ID      int64  `json:"id"`
		Method  string `json:"method"`
		Params  any    `json:"params,omitempty"`
	}{"2.0", id, method, params}
	if err := c.send(ctx, req); err != nil {
		c.forget(id)
		return nil, fmt.Errorf("%s: %w", method, err)
	}
	select {
	case r := <-ch:
		if r.err != nil {
			return nil, fmt.Errorf("%s: %w", method, r.err)
		}
		return r.result, nil
	case <-ctx.Done():
		c.forget(id)
		return nil, fmt.Errorf("%s: %w", method, ctx.Err())
	}
}

func (c *rpcConn) send(ctx context.Context, msg any) error {
	b, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	c.wmu.Lock()
	defer c.wmu.Unlock()
	// A host that stops reading its stdin must not block the caller forever.
	if wd, ok := c.w.(writeDeadliner); ok {
		deadline, _ := ctx.Deadline()
		_ = wd.SetWriteDeadline(deadline)
	}
	_, err = c.w.Write(b)
	return err
}

func (c *rpcConn) forget(id int64) {
	c.mu.Lock()
	delete(c.pending, id)
	c.mu.Unlock()
}

// close fails every pending call and refuses new ones.
func (c *rpcConn) close(err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return
	}
	c.err = err
	for id, ch := range c.pending {
		ch <- rpcReply{err: err}
		delete(c.pending, id)
	}
}

// readLoop reads the host's stdout until it ends, then closes the channel.
func (c *rpcConn) readLoop(r io.Reader) {
	err := readLines(r, maxControlLine, func(line []byte, tooLong bool) {
		if tooLong {
			c.log.Printf("control channel: skipped a message over %d bytes", maxControlLine)
			return
		}
		c.handle(line)
	})
	if err == nil {
		err = errors.New("control channel closed")
	}
	c.close(fmt.Errorf("agent host: %w", err))
}

func (c *rpcConn) handle(line []byte) {
	line = bytes.TrimSpace(line)
	if len(line) == 0 {
		return
	}
	var m rpcMessage
	if err := json.Unmarshal(line, &m); err != nil || m.JSONRPC != "2.0" {
		c.malformed(line)
		return
	}
	hasID := len(m.ID) > 0 && string(m.ID) != "null"
	switch {
	case m.Method != "" && !hasID:
		c.notify(m.Method, m.Params)
	case m.Method != "":
		// The driver serves no requests on this channel.
		c.log.Printf("control channel: host called unknown method %q", m.Method)
		_ = c.send(context.Background(), rpcMessage{JSONRPC: "2.0", ID: m.ID,
			Error: &RPCError{Code: -32601, Message: "method not found"}})
	case hasID:
		id, err := strconv.ParseInt(string(m.ID), 10, 64)
		if err != nil {
			c.malformed(line)
			return
		}
		c.mu.Lock()
		ch := c.pending[id]
		delete(c.pending, id)
		c.mu.Unlock()
		if ch == nil {
			c.log.Printf("control channel: reply to unknown or abandoned request %d skipped", id)
			return
		}
		if m.Error != nil {
			ch <- rpcReply{err: m.Error}
		} else {
			ch <- rpcReply{result: m.Result}
		}
	default:
		c.malformed(line)
	}
}

func (c *rpcConn) malformed(line []byte) {
	const show = 80
	excerpt := line
	if len(excerpt) > show {
		excerpt = excerpt[:show]
	}
	c.log.Printf("control channel: skipped a malformed line (%d bytes): %q", len(line), excerpt)
}

// readLines calls fn for every line of r, without its line ending. A line
// longer than limit is passed once, cut to limit bytes, with tooLong set. fn
// must copy line to keep it. readLines returns nil at EOF.
func readLines(r io.Reader, limit int, fn func(line []byte, tooLong bool)) error {
	br := bufio.NewReaderSize(r, 64<<10)
	var buf []byte
	tooLong := false
	for {
		chunk, err := br.ReadSlice('\n')
		if room := limit - len(buf); len(chunk) > room {
			buf = append(buf, chunk[:max(room, 0)]...)
			tooLong = true
		} else {
			buf = append(buf, chunk...)
		}
		switch {
		case err == nil:
			fn(bytes.TrimRight(buf, "\r\n"), tooLong)
			buf, tooLong = buf[:0], false
		case errors.Is(err, bufio.ErrBufferFull):
		default:
			if len(buf) > 0 {
				fn(bytes.TrimRight(buf, "\r\n"), tooLong)
			}
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
	}
}
