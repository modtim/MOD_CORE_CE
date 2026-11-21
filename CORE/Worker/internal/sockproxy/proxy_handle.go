package sockproxy

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
)

// ServeHTTP inspects the request, applies the policy, then forwards
// through the ReverseProxy. Implementing the handler inline (instead
// of serving rproxy directly) is what lets us read the body, decide,
// and restore it before forwarding — while still getting the
// ReverseProxy's hijack handling for attach/exec-upgrade and its
// pass-through streaming for logs/events/build.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	uri := r.URL.RequestURI()
	apiPath, _ := splitPath(uri)

	body, err := s.maybeBuffer(r, apiPath)
	if err != nil {
		s.log.Warn("docker proxy: body read failed", "method", r.Method, "path", uri, "err", err)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		w.Write([]byte(`{"message":"docker proxy: body read failed"}`))
		return
	}

	allow, reason := s.policy.Decide(r.Method, uri, body)
	if !allow {
		// One structured line per denied (report) / blocked (enforce)
		// request: method, path, mode, reason. Never the body.
		s.log.Warn("docker proxy: request denied by policy",
			"method", r.Method, "path", uri,
			"mode", string(s.mode), "reason", reason)
		if s.mode == ModeEnforce {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusForbidden)
			b, _ := json.Marshal(map[string]string{"message": reason})
			w.Write(b)
			return
		}
		// ModeReport: logged above; fall through and forward anyway.
	}

	s.rproxy.ServeHTTP(w, r)
}

// maybeBuffer reads the body up to bodyCapBytes for the endpoints the
// policy inspects, restoring r.Body (what we read plus any unread
// tail) so the forward sees the original stream. Non-inspected
// endpoints keep their body untouched.
func (s *Server) maybeBuffer(r *http.Request, apiPath string) ([]byte, error) {
	if !wantsBody(r.Method, apiPath) || r.Body == nil {
		return nil, nil
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, bodyCapBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > bodyCapBytes {
		// Keep the whole stream forwardable: the read prefix, then
		// the unread tail. Leave framing untouched (the body is still
		// longer than what we read); the policy will reject the
		// truncated JSON.
		r.Body = io.NopCloser(io.MultiReader(bytes.NewReader(data), r.Body))
		return data, nil
	}
	r.Body = io.NopCloser(bytes.NewReader(data))
	if r.ContentLength >= 0 {
		r.ContentLength = int64(len(data))
	}
	r.Header.Set("Content-Length", strconv.Itoa(len(data)))
	return data, nil
}
