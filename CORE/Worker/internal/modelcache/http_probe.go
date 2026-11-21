package modelcache

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// probeBytes is how much of the target we read to estimate throughput. A small
// amount is enough: it keeps the probe cheap and avoids pulling megabytes over
// a WAN just to time it.
const probeBytes = 256 * 1024 // 256 KiB

// httpProbePath is the small, universal path probed on an endpoint. The
// platform mirror and the upstream hub both answer the root with a fast, tiny
// response, so "/" is the safest common denominator. (A deeper path such as a
// specific repo file would 404 on hosts that have not cached that repo.)
const httpProbePath = "/"

// NewHTTPProber returns a Prober that measures a candidate by GETting a small
// chunk (Range: 0..probeBytes) of its ProbeURL. Health is a 200 or 206.
// Latency is the full round trip; throughput is the bytes actually read over
// that time (0 when nothing could be measured). The probe never follows beyond
// a couple of redirects and bounds the read, so it stays cheap on any path.
//
// A nil or empty ProbeURL yields an unhealthy score (there is nothing to dial).
func NewHTTPProber(client *http.Client, probePath string) Prober {
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	if probePath == "" {
		probePath = httpProbePath
	}
	return func(ctx context.Context, c Candidate) Score {
		base := strings.TrimSpace(c.ProbeURL)
		if base == "" {
			return Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: false, Error: "no probe url"}
		}
		target := strings.TrimSuffix(base, "/") + probePath

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			return Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: false, Error: "build request: " + err.Error()}
		}
		req.Header.Set("Range", fmt.Sprintf("bytes=0-%d", probeBytes-1))
		req.Header.Set("User-Agent", "mod-worker-modelcache-probe")

		start := time.Now()
		resp, err := client.Do(req)
		if err != nil {
			// A context deadline / dial error is the normal "unreachable" case.
			return Score{Class: c.Class, Endpoint: c.Endpoint, Healthy: false,
				Error: shortErr(err), At: start}
		}
		defer resp.Body.Close()

		// Read at most probeBytes so a body that ignores Range still cannot
		// stall the probe or balloon memory.
		n, rerr := io.Copy(io.Discard, io.LimitReader(resp.Body, probeBytes))
		elapsed := time.Since(start)

		healthy := (resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusPartialContent) && rerr == nil
		sc := Score{
			Class:       c.Class,
			Endpoint:    c.Endpoint,
			Healthy:     healthy,
			LatencyMS:   float64(elapsed.Microseconds()) / 1000.0,
			ThroughputMBPS: throughputMBPS(n, elapsed),
			At:            start,
		}
		if !healthy {
			if rerr != nil {
				sc.Error = shortErr(rerr)
			} else {
				sc.Error = "http " + strconv.Itoa(resp.StatusCode)
			}
		}
		return sc
	}
}

// throughputMBPS converts bytes read over an elapsed interval to MB/s. It
// returns 0 for a zero/zero interval so an unmeasured path reads as "unknown"
// rather than "infinite".
func throughputMBPS(bytes int64, elapsed time.Duration) float64 {
	if bytes <= 0 || elapsed <= 0 {
		return 0
	}
	sec := elapsed.Seconds()
	if sec <= 0 {
		return 0
	}
	return float64(bytes) / (sec * 1024 * 1024)
}

// shortErr trims an error to a compact, secret-free string for the score.
func shortErr(err error) string {
	s := strings.TrimSpace(err.Error())
	if i := strings.IndexByte(s, '\n'); i > 0 {
		s = s[:i]
	}
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}
