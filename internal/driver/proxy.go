package driver

import (
	"context"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"time"
)

// newHostTransport dials the agent host's Unix socket for every connection.
// Compression is left to the two ends, and nothing times out a response that
// is still streaming.
func newHostTransport(socket string) *http.Transport {
	return &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", socket)
		},
		DisableCompression:  true,
		MaxIdleConnsPerHost: 32,
		IdleConnTimeout:     90 * time.Second,
	}
}

// newHostProxy forwards a conversation request to the agent host unparsed:
// same method, path (with its original escaping), raw query, headers and
// body, minus the hop-by-hop headers and the Authorization header, which
// carries the driver token. Responses are flushed as they arrive. A client
// that goes away cancels the request to the host; a host that goes away
// mid-response aborts the client's connection.
func newHostProxy(tr http.RoundTripper, logger *log.Logger) *httputil.ReverseProxy {
	return &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme = "http"
			pr.Out.URL.Host = "agent-host" // the transport ignores it and dials the socket
			// ReverseProxy drops query parameters it cannot parse; keep the
			// query exactly as the caller sent it.
			pr.Out.URL.RawQuery = pr.In.URL.RawQuery
			pr.Out.Header.Del("Authorization")
		},
		Transport:     tr,
		FlushInterval: -1,
		ErrorLog:      logger,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			if r.Context().Err() != nil {
				return // the caller went away
			}
			logger.Printf("proxy %s %s: %v", r.Method, r.URL.Path, err)
			writeError(w, http.StatusServiceUnavailable, "not_ready", "agent host unavailable")
		},
	}
}
