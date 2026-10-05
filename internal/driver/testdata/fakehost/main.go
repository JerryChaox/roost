// Command fakehost is the agent host the driver's tests run. It speaks the
// control channel on stdio and serves a few conversation routes on the Unix
// socket named in initialize:
//
//	/v1/conversations/_info     its pid, the initialize params it got, how many requests it saw cancelled
//	/v1/conversations/echo/...  the request as it arrived: method, request URI, headers, body
//	/v1/conversations/c1/stream SSE: four events, 300ms apart
//	/v1/conversations/hang      sends headers, then waits for the caller to go away
//
// FAKEHOST_INIT_DELAY (a Go duration) delays the initialize reply.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

var (
	outMu       sync.Mutex
	initParams  atomic.Pointer[json.RawMessage]
	disconnects atomic.Int64
)

func writeLine(v any) {
	b, _ := json.Marshal(v)
	outMu.Lock()
	defer outMu.Unlock()
	os.Stdout.Write(append(b, '\n'))
}

func reply(id json.RawMessage, result any) {
	writeLine(map[string]any{"jsonrpc": "2.0", "id": id, "result": result})
}

func main() {
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 64<<10), 16<<20)
	var socket string
	for in.Scan() {
		var req struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
		}
		if err := json.Unmarshal(in.Bytes(), &req); err != nil {
			fmt.Fprintf(os.Stderr, "bad request line: %v\n", err)
			continue
		}
		switch req.Method {
		case "initialize":
			var p struct {
				Socket string `json:"socket"`
			}
			_ = json.Unmarshal(req.Params, &p)
			params := append(json.RawMessage(nil), req.Params...)
			initParams.Store(&params)
			if d, err := time.ParseDuration(os.Getenv("FAKEHOST_INIT_DELAY")); err == nil {
				time.Sleep(d)
			}
			ln, err := net.Listen("unix", p.Socket)
			if err != nil {
				writeLine(map[string]any{"jsonrpc": "2.0", "id": req.ID,
					"error": map[string]any{"code": 1, "message": err.Error()}})
				continue
			}
			socket = p.Socket
			go http.Serve(ln, http.HandlerFunc(serve))
			// The driver must skip a line that is not JSON-RPC and accept a
			// snapshot notification before the reply.
			outMu.Lock()
			os.Stdout.WriteString("this is not a control message\n")
			outMu.Unlock()
			writeLine(map[string]any{"jsonrpc": "2.0", "method": "snapshot",
				"params": map[string]any{"conversation": "c1", "run": "m-1", "position": "1"}})
			fmt.Fprintln(os.Stderr, "initialized")
			reply(req.ID, map[string]any{"agent": "fake", "version": "0.0.1",
				"durability": "step", "capabilities": []string{"steer"}})
		case "quiesce":
			reply(req.ID, map[string]any{"running": 2})
		case "resume":
			reply(req.ID, map[string]any{"running": 0})
		case "state":
			reply(req.ID, map[string]any{"runs": []any{}, "queues": map[string]any{}})
		case "shutdown":
			reply(req.ID, map[string]any{})
			if socket != "" {
				os.Remove(socket)
			}
			os.Exit(0)
		default:
			writeLine(map[string]any{"jsonrpc": "2.0", "id": req.ID,
				"error": map[string]any{"code": -32601, "message": "method not found"}})
		}
	}
	// The control channel closed: the driver is gone.
	os.Exit(0)
}

func serve(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.URL.Path == "/v1/conversations/_info":
		var init json.RawMessage
		if p := initParams.Load(); p != nil {
			init = *p
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"pid": os.Getpid(), "init": init, "disconnects": disconnects.Load()})
	case strings.HasPrefix(r.URL.Path, "/v1/conversations/echo/"):
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Fake-Host", "yes")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]any{
			"method": r.Method, "uri": r.RequestURI, "header": r.Header, "body": string(body)})
	case r.URL.Path == "/v1/conversations/c1/stream":
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		f := w.(http.Flusher)
		f.Flush()
		for i := 1; i <= 4; i++ {
			fmt.Fprintf(w, "id: %d\ndata: {\"n\":%d}\n\n", i, i)
			f.Flush()
			if i < 4 {
				select {
				case <-time.After(300 * time.Millisecond):
				case <-r.Context().Done():
					return
				}
			}
		}
	case r.URL.Path == "/v1/conversations/hang":
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		disconnects.Add(1)
	default:
		http.Error(w, `{"error":"unknown_conversation","detail":"fake"}`, http.StatusNotFound)
	}
}
