// Package hostexec: worker-side relay client for the mod-host-exec unix-socket
// helper (opsx worker-host-exec, hx.7b). A containerized worker has no host
// privileges and cannot exec host jobs itself (D1); instead it signs the job
// with the API-issued HMAC key, sends exactly one WireRequest to the root
// helper's socket, and relays the helper's newline-delimited JSON frames to
// emit until the final exit-code frame. Cancelling ctx closes the connection,
// which is what makes the helper kill the whole process group (runner.go).
package hostexec

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"os"
	"strings"
	"syscall"
	"time"
)

const (
	// DefaultSocket matches cmd/host-exec-helper's default socket path, bind-
	// mounted into a containerized worker (linux/macOS).
	DefaultSocket = "/run/mod-host-exec/host-exec.sock"

	// DefaultPipe is the Windows equivalent transport (992b/hx.9w): Windows has
	// no unix-socket semantics, so the relay uses a named pipe instead, ACL'd
	// to SYSTEM only by cmd/host-exec-helper (see platform_windows.go there).
	// Not yet wired into cmd/worker/hostexec_wire.go's default (that's a
	// separate, follow-up brief) — this constant plus the dial/listen
	// transport below is the plumbing a later change will select.
	DefaultPipe = `\\.\pipe\mod-host-exec`

	// dialTimeout bounds only the connect. Job output streaming afterwards is
	// bounded by the job's own timeout_s, enforced by the helper.
	dialTimeout = 5 * time.Second
)

// PickPath returns DefaultPipe for goos == "windows", DefaultSocket
// otherwise. A pure function of an explicit goos string (not runtime.GOOS)
// so client_test.go can table-test every branch on any host, including a
// linux CI runner that will never actually dial a Windows named pipe.
func PickPath(goos string) string {
	if goos == "windows" {
		return DefaultPipe
	}
	return DefaultSocket
}

// SocketExists reports whether path is an existing, dialable transport: a
// unix socket on linux/macOS, a named pipe on Windows (see platformExists in
// client_other.go / client_windows.go). A var (not a func) so tests and
// unusual installs can inject the check.
var SocketExists = func(path string) bool {
	return platformExists(path)
}

// RunningInContainer reports whether the current process looks containerized:
// /.dockerenv, or a /proc/1/cgroup line naming docker/containerd. A var so
// tests can inject the detection.
var RunningInContainer = func() bool {
	if _, err := os.Stat("/.dockerenv"); err == nil {
		return true
	}
	cg, err := os.ReadFile("/proc/1/cgroup")
	if err != nil {
		return false
	}
	s := string(cg)
	return strings.Contains(s, "docker") || strings.Contains(s, "containerd")
}

// DefaultDial connects to a helper transport: a unix socket on linux/macOS,
// a named pipe on Windows (platformDial in client_other.go /
// client_windows.go). A var so tests can hand a Relay an in-memory
// connection instead of a filesystem socket.
var DefaultDial = func(ctx context.Context, path string) (net.Conn, error) {
	return platformDial(ctx, path)
}

// Mode picks the execution path for a host-exec job (hx.7 / D1): the helper
// socket wins whenever it is present; otherwise a non-containerized (systemd
// binary) install execs directly; a container with no helper has no way to
// reach the host and fails with an actionable error.
func Mode(socket string, runningInContainer bool) (string, error) {
	if SocketExists(socket) {
		return "helper", nil
	}
	if !runningInContainer {
		return "direct", nil
	}
	return "", errors.New("host-exec unavailable: running in a container without the mod-host-exec helper socket (see install docs)")
}

// IsRoot reports whether this process runs as uid 0 — the host-exec direct
// runner needs it (993a). A var so tests can inject the answer; the default
// is os.Geteuid() == 0 (client_other.go's platformIsRoot), which works
// identically on linux AND macOS (runner_darwin.go: a macOS direct runner
// needs the same root-equivalent install as linux, so a non-root macOS
// worker must claim "not_root" too — procfs-based detection would have
// missed that, since /proc doesn't exist on macOS). On Windows,
// client_windows.go's platformIsRoot reports whether the process token is
// elevated (the UAC/Administrator equivalent of uid 0) — ComputeStatus
// checks it the same as every other platform (993g).
var IsRoot = func() bool { return platformIsRoot() }

// Status is the worker's local view of host-exec availability (993a / hx.9-
// availability-ux): what THIS binary can do right now, before the API layers
// on agent-version and platform/tenant-setting gates (993b). Mode() answers
// "can I exec?"; Status answers "why, in a code the API can map to copy".
type Status struct {
	Available bool   `json:"available"`
	Mode      string `json:"mode"`
	Code      string `json:"code"`
	Message   string `json:"message"`
}

// ComputeStatus is Mode() plus the codes this worker can determine locally:
// "available" (helper or direct), "helper_missing" (containerized, no socket)
// and "not_root" (non-container, not root/elevated — every platform's
// IsRoot var is checked the same way, including Windows's elevation check,
// 993g). Codes it deliberately does NOT emit: agent version and
// platform/tenant settings, which only the API can see.
func ComputeStatus(socket string, runningInContainer bool) Status {
	if SocketExists(socket) {
		return Status{Available: true, Mode: "helper", Code: "available", Message: "helper socket present"}
	}
	if runningInContainer {
		return Status{
			Available: false,
			Mode:      "",
			Code:      "helper_missing",
			Message:   "containerized worker without the mod-host-exec helper socket",
		}
	}
	if !IsRoot() {
		return Status{Available: false, Mode: "", Code: "not_root", Message: "direct runner needs a root/Administrator (elevated) install"}
	}
	return Status{Available: true, Mode: "direct", Code: "available", Message: "direct runner available"}
}

// Relay is one worker's view of the helper: the socket, the HMAC key the API
// issued for this worker, and the worker_id the helper will accept.
type Relay struct {
	Socket   string
	Key      []byte
	WorkerID string
	Dial     func(ctx context.Context, path string) (net.Conn, error)
}

// DecodeFrameData decodes one output frame's base64 payload, matching the
// helper's runAndStream encoding.
func DecodeFrameData(f OutFrame) ([]byte, error) {
	if f.Data == "" {
		return nil, nil
	}
	b, err := base64.StdEncoding.DecodeString(f.Data)
	if err != nil {
		return nil, fmt.Errorf("hostexec: frame data is not base64: %w", err)
	}
	return b, nil
}

// Exec signs req, sends it to the helper, and relays frames to emit until the
// final exit-code frame, whose code it returns. req.WorkerID defaults to the
// relay's. A missing socket is reported as a clear, actionable error.
func (r *Relay) Exec(ctx context.Context, req WireRequest, emit func(OutFrame) error) (int, error) {
	if len(req.Argv) == 0 {
		return -1, errors.New("hostexec: empty argv")
	}
	if req.WorkerID == "" {
		req.WorkerID = r.WorkerID
	}
	req.Sig = ComputeSig(r.Key, req)

	dial := r.Dial
	if dial == nil {
		dial = DefaultDial
	}
	// A missing socket is reported before dialing: connect errors on a unix
	// path are shaped by the kernel (not a plain ENOENT), so the stat is the
	// reliable check. Injected dials (net.Pipe, a test conn) skip it.
	if r.Dial == nil && !SocketExists(r.Socket) {
		return -1, missingSocket(r.Socket)
	}
	conn, err := dial(ctx, r.Socket)
	if err != nil {
		return -1, dialError(r.Socket, err)
	}

	// ctx cancellation closes the connection; the helper sees the disconnect and
	// kills the whole process group. An uncancelable ctx (Background) has no
	// Done channel, so there is nothing to hook — we just close at the end.
	stopCloser := func() bool { return false }
	if ctx.Done() != nil {
		stopCloser = context.AfterFunc(ctx, func() { _ = conn.Close() })
	}
	code, streamErr := stream(ctx, conn, r.Socket, req, emit)
	stopCloser()
	_ = conn.Close()
	return code, streamErr
}

// stream writes the one-shot request then reads frames. Closing the conn (via
// the ctx hook in Exec) makes a blocked Decode return, so cancellation returns
// promptly instead of waiting for the job.
func stream(ctx context.Context, conn net.Conn, socket string, req WireRequest, emit func(OutFrame) error) (int, error) {
	b, err := json.Marshal(req)
	if err != nil {
		return -1, err
	}
	b = append(b, '\n')
	if err := writeAll(conn, b); err != nil {
		return -1, err
	}

	dec := json.NewDecoder(conn)
	for {
		var f OutFrame
		if err := dec.Decode(&f); err != nil {
			if ctxErr := ctx.Err(); ctxErr != nil {
				return -1, fmt.Errorf("hostexec: relay cancelled on %s: %v", socket, ctxErr)
			}
			return -1, fmt.Errorf("hostexec: relay read from %s failed: %w", socket, err)
		}
		if f.Error != "" {
			return -1, fmt.Errorf("hostexec: helper rejected the request: %s", f.Error)
		}
		if f.ExitCode != nil {
			return *f.ExitCode, nil
		}
		if emit != nil {
			if err := emit(f); err != nil {
				return -1, err
			}
		}
	}
}

func writeAll(c net.Conn, b []byte) error {
	for n := 0; n < len(b); {
		w, err := c.Write(b[n:])
		if err != nil {
			return err
		}
		if w == 0 {
			return errors.New("hostexec: relay wrote 0 bytes")
		}
		n += w
	}
	return nil
}

// dialError turns a missing or unserved socket into the message an operator
// can act on. The pre-dial SocketExists check in Exec only runs when Dial is
// nil (an injected Dial — e.g. net.Pipe in tests, or a prod wrapper — may not
// correspond to a real stat-able path), so this is the only place a missing
// or abandoned socket gets mapped for an injected dialer; it must not rely on
// os.IsNotExist, which does not unwrap the *net.OpError a real net.Dial
// returns and would silently fall through to the generic error below.
func dialError(socket string, err error) error {
	if errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOTDIR) || errors.Is(err, syscall.ECONNREFUSED) {
		return missingSocket(socket)
	}
	return fmt.Errorf("host-exec: connecting to %s: %w", socket, err)
}

// missingSocket is the one actionable message for "the helper is not here".
func missingSocket(socket string) error {
	return fmt.Errorf("host-exec: no helper socket at %s (mod-host-exec-helper is not listening; see install docs)", socket)
}
