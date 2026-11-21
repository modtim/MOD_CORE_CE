// Package hostexec: wire protocol for the mod-host-exec unix-socket helper
// (opsx worker-host-exec, hx.7a). The wire types and HMAC signing live here
// so the root helper (cmd/host-exec-helper) and any future in-worker relay
// speak exactly one protocol. JSON field names are byte-identical to the
// helper's original (main.go) definitions — no protocol change.
package hostexec

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
)

// WireRequest is the one-shot JSON request sent over the helper's unix
// socket. It is named WireRequest rather than Request because hostexec.Request
// (runner.go) already owns that name for the runner's own request type; the
// helper maps one onto the other. Field order here is the canonical order the
// signature is computed over — do not reorder.
type WireRequest struct {
	JobID    string            `json:"job_id"`
	WorkerID string            `json:"worker_id"`
	Argv     []string          `json:"argv"`
	Cwd      string            `json:"cwd"`
	Env      map[string]string `json:"env"`
	RunAs    string            `json:"run_as"`
	TimeoutS int               `json:"timeout_s"`
	Sig      string            `json:"sig"`
}

// sigPayload mirrors WireRequest minus Sig. Its field order is fixed by Go
// struct declaration order and json.Marshal sorts map keys, so marshaling it
// is the canonical form the signature is computed over.
type sigPayload struct {
	JobID    string            `json:"job_id"`
	WorkerID string            `json:"worker_id"`
	Argv     []string          `json:"argv"`
	Cwd      string            `json:"cwd"`
	Env      map[string]string `json:"env"`
	RunAs    string            `json:"run_as"`
	TimeoutS int               `json:"timeout_s"`
}

// OutFrame is one newline-delimited JSON frame written back to the caller.
// Either (Stream, Data) for a chunk of output, or ExitCode for the final
// frame, or Error for a rejection/failure.
type OutFrame struct {
	Stream   string `json:"stream,omitempty"`
	Data     string `json:"data,omitempty"`
	ExitCode *int   `json:"exit_code,omitempty"`
	Error    string `json:"error,omitempty"`
}

// wirePayload builds the sig-less canonical form of req.
func wirePayload(req WireRequest) sigPayload {
	return sigPayload{
		JobID:    req.JobID,
		WorkerID: req.WorkerID,
		Argv:     req.Argv,
		Cwd:      req.Cwd,
		Env:      req.Env,
		RunAs:    req.RunAs,
		TimeoutS: req.TimeoutS,
	}
}

// ComputeSig returns the hex HMAC-SHA256 of the canonical (sig-less) JSON
// encoding of req, keyed by key.
func ComputeSig(key []byte, req WireRequest) string {
	b, _ := json.Marshal(wirePayload(req))
	mac := hmac.New(sha256.New, key)
	mac.Write(b)
	return hex.EncodeToString(mac.Sum(nil))
}

func VerifySig(key []byte, req WireRequest) bool {
	if req.Sig == "" {
		return false
	}
	want := ComputeSig(key, req)
	return subtle.ConstantTimeCompare([]byte(want), []byte(req.Sig)) == 1
}

// WriteFrame serializes f as one line of JSON plus a newline onto w.
func WriteFrame(w io.Writer, f OutFrame) error {
	b, err := json.Marshal(f)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	_, err = w.Write(b)
	return err
}
