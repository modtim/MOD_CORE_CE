package credsrefresher

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// hardenLogBuf is a goroutine-safe log sink (the loop logs concurrently).
type hardenLogBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (h *hardenLogBuf) Write(p []byte) (int, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.b.Write(p)
}

func (h *hardenLogBuf) String() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.b.String()
}

// hardenGateProvider: call 1 waits on gate then succeeds; every later call
// fails with errText. Lets a test pin exactly one successful file write.
type hardenGateProvider struct {
	mu      sync.Mutex
	calls   int
	gate    <-chan struct{}
	errText string
}

func (g *hardenGateProvider) GetCredentials(ctx context.Context, taskID int) (*transport.Credentials, error) {
	g.mu.Lock()
	g.calls++
	n := g.calls
	g.mu.Unlock()
	if n == 1 {
		<-g.gate
		return &transport.Credentials{MinIO: transport.MinIOCredentials{AccessKey: "ak-fresh"}}, nil
	}
	return nil, errors.New(g.errText)
}

func (g *hardenGateProvider) count() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.calls
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// (a) The provider's message text (a sentinel here) never reaches the log;
// only the status class does.
func TestProviderErrorTextNotLogged(t *testing.T) {
	const sentinel = "SENTINEL-PROVIDER-BODY-7f3a"
	dir := t.TempDir()
	closed := make(chan struct{})
	close(closed)
	gp := &hardenGateProvider{gate: closed, errText: "get credentials: HTTP 500: " + sentinel}
	var buf hardenLogBuf
	opts := fastOpts(dir)
	opts.Log = slog.New(slog.NewTextHandler(&buf, nil))
	r, err := Start(gp, 11, &transport.MinIOCredentials{AccessKey: "ak"}, opts)
	if err != nil {
		t.Fatal(err)
	}
	waitFor(t, "failing refresh call", func() bool { return gp.count() >= 2 })
	r.Stop()
	logged := buf.String()
	if !strings.Contains(logged, "creds refresh failed") {
		t.Fatalf("expected a refresh-failed log line, got: %s", logged)
	}
	if strings.Contains(logged, sentinel) {
		t.Fatalf("provider message text leaked into the log: %s", logged)
	}
	if !strings.Contains(logged, "http_500") {
		t.Fatalf("expected status class in the log, got: %s", logged)
	}
}

// (c) A failed rename (destination is a non-empty directory) leaves no .tmp.
func TestRenameFailureLeavesNoTmp(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-12.json")
	gate := make(chan struct{})
	gp := &hardenGateProvider{gate: gate, errText: "get credentials: HTTP 500: down"}
	r, err := Start(gp, 12, &transport.MinIOCredentials{AccessKey: "ak"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	// Make the destination a non-empty directory so rename(tmp, path) fails.
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(path, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, "keep"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	close(gate) // call 1 succeeds -> writeFile -> rename fails
	waitFor(t, "write attempt and a later failing call", func() bool { return gp.count() >= 3 })
	// Check BEFORE Stop(): Stop also removes a stale .tmp, which would mask
	// a missing rename cleanup. After call 1 no further writes happen.
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("temp file left behind after failed rename (stat err=%v)", err)
	}
	r.Stop()
}

// (d) Stop() removes a stale .tmp left by an earlier crash or failed write.
func TestStopRemovesStaleTmp(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-13.json")
	fp := &fakeProvider{}
	r, err := Start(fp, 13, &transport.MinIOCredentials{AccessKey: "ak"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path+".tmp", []byte(`{"secret":"stale"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r.Stop()
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("stale .tmp survived Stop (stat err=%v)", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("creds file survived Stop (stat err=%v)", err)
	}
}

// (e) A normal refresh writes the expected content at mode 0600 and no tmp.
func TestNormalRefreshWritesMode0600(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "task-14.json")
	fp := &fakeProvider{creds: &transport.Credentials{
		MinIO: transport.MinIOCredentials{AccessKey: "ak-fresh", Endpoint: "minio.test:9000"},
	}}
	r, err := Start(fp, 14, &transport.MinIOCredentials{AccessKey: "ak-old"}, fastOpts(dir))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	waitFor(t, "refreshed creds in file", func() bool {
		data, err := os.ReadFile(path)
		return err == nil && strings.Contains(string(data), "ak-fresh")
	})
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("creds file mode = %o, want 600", perm)
	}
	if cf := credsFile(t, path); cf.AccessKey != "ak-fresh" || cf.Endpoint != "minio.test:9000" || cf.TaskID != 14 {
		t.Fatalf("unexpected creds content: %+v", cf)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("tmp file lingers after a successful write (stat err=%v)", err)
	}
}
