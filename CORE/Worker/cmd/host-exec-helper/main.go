// Command host-exec-helper is the mod-host-exec root-level helper (opsx
// worker-host-exec, hx.4 + hx.7a). It listens on a unix socket bind-mounted
// into a containerized worker (e.g. my_mod_worker) and runs host-exec jobs
// that the API has signed for this specific worker. One JSON request per
// connection; the reply is a stream of newline-delimited JSON frames.
//
// The wire protocol (types + HMAC signing) lives in
// CORE/Worker/internal/hostexec/protocol.go and execution goes through
// hostexec.Run (CORE/Worker/internal/hostexec/runner.go), so the helper and
// the in-worker `host-exec` task kind share one execution path.
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/user"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/hostexec"
)

const (
	defaultSocketPath = "/run/mod-host-exec/host-exec.sock"
	defaultGroup      = "mod-worker"

	// maxRequestBytes bounds the one-shot JSON request so a client can't
	// exhaust memory with an oversized body.
	maxRequestBytes = 1 << 20 // 1 MiB
	// requestReadTimeout bounds how long handleConn waits for the full
	// request to arrive, so a client can't tie up a goroutine/fd by
	// trickling bytes in slowly (slowloris-style). Cleared once the
	// request has been decoded; job output streaming afterwards is
	// governed by the job's own timeout_s, not this deadline.
	requestReadTimeout = 10 * time.Second
)

// config holds the flags the helper is started with.
//
// hx.9: workerID and workerIDFile are alternatives. The manager's worker_id is
// assigned by the API on registration (hx.7e), i.e. AFTER install.sh has
// written the unit, so the unit cannot hard-code it. install.sh instead bind-
// mounts a tiny state file the worker writes once it learns its id, and starts
// the helper with --worker-id-file. The helper re-reads that file PER REQUEST
// (see resolveWorkerID), so it binds to whatever id the worker actually has and
// refuses every request until the file exists — fail closed, never "any id".
type config struct {
	socketPath    string
	group         string
	workerID      string
	workerIDFile  string
	keyFile       string
}

func parseFlags(args []string) (config, error) {
	fs := flag.NewFlagSet("host-exec-helper", flag.ContinueOnError)
	cfg := config{}
	fs.StringVar(&cfg.socketPath, "socket", defaultSocketPath, "unix socket path to listen on")
	fs.StringVar(&cfg.group, "group", defaultGroup, "group allowed to connect to the socket dir")
	fs.StringVar(&cfg.workerID, "worker-id", "", "this worker's id; requests for any other worker_id are rejected")
	fs.StringVar(&cfg.workerIDFile, "worker-id-file", "",
		"path to a file holding this worker's id, re-read per request; use INSTEAD of --worker-id when the id is assigned after install (hx.9)")
	fs.StringVar(&cfg.keyFile, "key-file", "", "path to the HMAC signing key (root-owned, 0600)")
	if err := fs.Parse(args); err != nil {
		return cfg, err
	}
	if cfg.workerID == "" && cfg.workerIDFile == "" {
		return cfg, errors.New("--worker-id or --worker-id-file is required")
	}
	if cfg.keyFile == "" {
		return cfg, errors.New("--key-file is required")
	}
	return cfg, nil
}

func readKey(path string) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("key file %s must not be group/world readable (mode %o)", path, info.Mode().Perm())
	}
	return os.ReadFile(path)
}

// listen binds the helper's transport: a unix socket (listenUnix, this file,
// !windows) or a SYSTEM-only named pipe (listenWindows, platform_windows.go,
// 992b/hx.9w) — see platform_unix.go / platform_windows.go for the two
// implementations.

// maxWorkerIDFileBytes caps how much of --worker-id-file we ever read, so a
// misconfigured or hostile bind mount (e.g. a socket, fifo, or huge file)
// can't make the helper block or allocate unbounded memory per request.
const maxWorkerIDFileBytes = 64

// resolveWorkerID returns the id this helper accepts for ONE request. A literal
// --worker-id is fixed at startup; with --worker-id-file the file is re-read
// every request, so the helper picks up the id the worker learned after install
// (hx.7e assigns it on the first answered heartbeat) without restarting, and
// refuses everything while the file is absent, empty, or not a bare positive
// integer. The file is root-read and written by the containerized worker, so
// it is trusted only for the ID, never for authority: the HMAC signature is
// still what authorizes the job. Strict parsing (trim + positive-int-only,
// never defaulting to 0 or passing garbage through) means a truncated or
// corrupted write can only ever fail closed, not silently bind to an
// unintended id.
func resolveWorkerID(cfg config) (string, error) {
	if cfg.workerID != "" {
		return cfg.workerID, nil
	}
	if cfg.workerIDFile == "" {
		return "", errors.New("no worker id configured")
	}
	f, err := os.Open(cfg.workerIDFile)
	if err != nil {
		return "", fmt.Errorf("worker id file %s: %w", cfg.workerIDFile, err)
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxWorkerIDFileBytes+1))
	if err != nil {
		return "", fmt.Errorf("worker id file %s: %w", cfg.workerIDFile, err)
	}
	if len(b) > maxWorkerIDFileBytes {
		return "", fmt.Errorf("worker id file %s exceeds %d bytes", cfg.workerIDFile, maxWorkerIDFileBytes)
	}
	id := strings.TrimSpace(string(b))
	if id == "" {
		return "", fmt.Errorf("worker id file %s is empty", cfg.workerIDFile)
	}
	if n, err := strconv.Atoi(id); err != nil || n <= 0 {
		return "", fmt.Errorf("worker id file %s does not contain a positive integer", cfg.workerIDFile)
	}
	return id, nil
}

func groupGID(name string) (int, error) {
	g, err := user.LookupGroup(name)
	if err != nil {
		return 0, err
	}
	return strconv.Atoi(g.Gid)
}

func main() {
	cfg, err := parseFlags(os.Args[1:])
	if err != nil {
		log.Fatalf("host-exec-helper: %v", err)
	}
	key, err := readKey(cfg.keyFile)
	if err != nil {
		log.Fatalf("host-exec-helper: reading key file: %v", err)
	}
	l, err := listen(cfg)
	if err != nil {
		log.Fatalf("host-exec-helper: %v", err)
	}
	defer l.Close()
	idSrc := "worker_id=" + cfg.workerID
	if cfg.workerIDFile != "" {
		idSrc = "worker_id_file=" + cfg.workerIDFile
	}
	log.Printf("host-exec-helper: listening on %s (%s)", cfg.socketPath, idSrc)
	done    := make(chan struct{})
	waitSvc := runMaybeAsService(func() { _ = l.Close() }, done)
	serve(l, cfg, key)
	close(done)
	waitSvc()
}

func serve(l net.Listener, cfg config, key []byte) {
	for {
		conn, err := l.Accept()
		if err != nil {
			log.Printf("host-exec-helper: accept: %v", err)
			return
		}
		go func() {
			defer conn.Close()
			if err := handleConn(conn, cfg, key); err != nil {
				log.Printf("host-exec-helper: connection error: %v", err)
			}
		}()
	}
}

// handleConn reads exactly one WireRequest, logs the connecting peer's uid via
// SO_PEERCRED, verifies the signature and worker_id, then runs the job through
// the shared hostexec runner.
func handleConn(conn net.Conn, cfg config, key []byte) error {
	if uid, err := peerUID(conn); err == nil {
		log.Printf("host-exec-helper: peer uid=%d", uid)
	} else {
		log.Printf("host-exec-helper: SO_PEERCRED unavailable: %v", err)
	}

	if err := conn.SetReadDeadline(time.Now().Add(requestReadTimeout)); err != nil {
		log.Printf("host-exec-helper: set read deadline: %v", err)
	}
	var req hostexec.WireRequest
	dec := json.NewDecoder(io.LimitReader(conn, maxRequestBytes+1))
	if err := dec.Decode(&req); err != nil {
		return hostexec.WriteFrame(conn, hostexec.OutFrame{Error: "bad request: " + err.Error()})
	}
	// Clear the deadline: the request is fully decoded, and job output
	// streaming below is write-only and bounded by the job's own
	// timeout_s, not a connection read deadline.
	if err := conn.SetReadDeadline(time.Time{}); err != nil {
		log.Printf("host-exec-helper: clear read deadline: %v", err)
	}
	if !hostexec.VerifySig(key, req) {
		return hostexec.WriteFrame(conn, hostexec.OutFrame{Error: "bad signature"})
	}
	// The accepted id is resolved PER REQUEST (hx.9): with --worker-id-file the
	// worker_id only exists once the containerized worker has written its state
	// file, and a re-registered worker gets a new id without the helper
	// restarting. No id yet => refuse, never accept an arbitrary one.
	accepted, idErr := resolveWorkerID(cfg)
	if idErr != nil {
		return hostexec.WriteFrame(conn, hostexec.OutFrame{Error: "worker_id not bound: " + idErr.Error()})
	}
	if req.WorkerID != accepted {
		return hostexec.WriteFrame(conn, hostexec.OutFrame{Error: "wrong worker_id"})
	}
	return runAndStream(conn, req)
}

// peerUID reads the connecting process's uid off the transport: SO_PEERCRED
// on the unix socket (listenUnix / platform_unix.go), unimplemented on the
// Windows named pipe (platform_windows.go, 992b/hx.9w — handleConn already
// treats a peerUID error as a non-fatal "unavailable" log line, so a stub
// there is safe).

// runAndStream maps the wire request onto hostexec.Request and relays the
// run's frames back onto the connection. Output frames are base64-encoded,
// matching the wire protocol the API's signer/client speak.
func runAndStream(conn net.Conn, req hostexec.WireRequest) error {
	runnerReq := hostexec.Request{
		Argv:    req.Argv,
		Cwd:     req.Cwd,
		Env:     req.Env,
		RunAs:   req.RunAs,
		Timeout: time.Duration(req.TimeoutS) * time.Second,
	}

	// hostexec.Run may call emit from concurrent stdout/stderr readers, so
	// serialize the frame writes onto the single connection.
	var mu sync.Mutex
	emit := func(f hostexec.Frame) {
		mu.Lock()
		defer mu.Unlock()
		_ = hostexec.WriteFrame(conn, hostexec.OutFrame{
			Stream: f.Stream,
			Data:   base64.StdEncoding.EncodeToString(f.Data),
		})
	}

	// ctx is the run's cancellation context; hostexec.Run extends it with the
	// job's timeout_s and kills the whole process group when it fires.
	code, err := hostexec.Run(context.Background(), runnerReq, emit)
	if err != nil {
		return hostexec.WriteFrame(conn, hostexec.OutFrame{Error: err.Error()})
	}
	return hostexec.WriteFrame(conn, hostexec.OutFrame{ExitCode: &code})
}
