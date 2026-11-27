package executor

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// fakeEngine is a minimal Docker Engine that records the calls the volume prep
// makes. statMode lets a test make the daemon report a wrong mode.
type fakeEngine struct {
	mu        sync.Mutex
	calls     []string
	createReq map[string]any
	putPath   string
	putTar    []tarEntry
	statMode  os.FileMode
	putFail   bool
	rmDelay   time.Duration
	rmDoneAt  time.Time
	volRmAt   time.Time
}

// snapshotCalls returns a copy of the recorded calls and the removal time under
// the fake's mutex, so a test never reads them while a handler goroutine writes.
func (f *fakeEngine) snapshotCalls() ([]string, time.Time) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...), f.rmDoneAt
}

type tarEntry struct {
	Name     string
	Mode     int64
	Uid, Gid int
	Type     byte
}

func (f *fakeEngine) handler(w http.ResponseWriter, r *http.Request) {
	p := r.URL.Path
	f.mu.Lock()
	f.calls = append(f.calls, r.Method+" "+p)
	f.mu.Unlock()
	switch {
	case strings.HasSuffix(p, "/containers/create"):
		var m map[string]any
		json.NewDecoder(r.Body).Decode(&m)
		f.mu.Lock()
		f.createReq = m
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		io.WriteString(w, `{"Id":"prepctr","Warnings":[]}`)
	case strings.HasSuffix(p, "/archive") && r.Method == http.MethodPut:
		if f.putFail {
			http.Error(w, `{"message":"boom"}`, 500)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		tr := tar.NewReader(bytes.NewReader(raw))
		f.mu.Lock()
		f.putPath = r.URL.Query().Get("path")
		for {
			h, err := tr.Next()
			if err != nil {
				break
			}
			f.putTar = append(f.putTar, tarEntry{h.Name, h.Mode, h.Uid, h.Gid, h.Typeflag})
		}
		f.mu.Unlock()
		w.WriteHeader(200)
	case strings.HasSuffix(p, "/archive") && r.Method == http.MethodHead:
		mode := f.statMode | os.ModeDir
		st, _ := json.Marshal(map[string]any{"name": "x", "size": 4096, "mode": uint32(mode),
			"mtime": "2026-11-25T15:18:00Z", "linkTarget": ""})
		w.Header().Set("X-Docker-Container-Path-Stat", base64.StdEncoding.EncodeToString(st))
		w.WriteHeader(200)
	case strings.Contains(p, "/containers/prepctr") && r.Method == http.MethodDelete:
		time.Sleep(f.rmDelay)
		f.mu.Lock()
		f.rmDoneAt = time.Now()
		f.mu.Unlock()
		w.WriteHeader(204)
	case strings.Contains(p, "/volumes/") && r.Method == http.MethodDelete:
		f.mu.Lock()
		f.volRmAt = time.Now()
		f.mu.Unlock()
		w.WriteHeader(204)
	case strings.HasSuffix(p, "/start"):
		http.Error(w, `{"message":"no start in this fake"}`, 500)
	default:
		w.WriteHeader(404)
	}
}

func newPrepExec(t *testing.T, f *fakeEngine) (*Executor, *bytes.Buffer) {
	srv := httptest.NewServer(http.HandlerFunc(f.handler))
	t.Cleanup(srv.Close)
	cli, err := client.NewClientWithOpts(client.WithHost("tcp://"+strings.TrimPrefix(srv.URL, "http://")),
		client.WithVersion("1.45"))
	if err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	e := &Executor{docker: cli, workerName: "w1", instanceID: "i1", network: "net1",
		log: slog.New(slog.NewTextHandler(&logs, nil))}
	return e, &logs
}

const wantLsOut = "prep OK: drwxrwxrwx    4 root     root          4096 Nov 25 15:18 /data " +
	"drwxrwxrwx    2 root     root          4096 Nov 25 15:18 /data/input " +
	"drwxrwxrwx    2 root     root          4096 Nov 25 15:18 /data/output "

func TestPrepAPIContractAndText(t *testing.T) {
	f := &fakeEngine{statMode: 0o777}
	e, _ := newPrepExec(t, f)
	out, err := e.prepareDataVolumeAPI(context.Background(), "alpine:3.20", "vol-a")
	if err != nil {
		t.Fatal(err)
	}
	if out != wantLsOut {
		t.Fatalf("prep text drifted from the helper's ls output:\n got %q\nwant %q", out, wantLsOut)
	}
	if f.putPath != "/" {
		t.Fatalf("archive must be extracted at /, got %q (a PUT to /data leaves the root 0755)", f.putPath)
	}
	want := []tarEntry{{"data/", 0o777, 0, 0, tar.TypeDir}, {"data/input/", 0o777, 0, 0, tar.TypeDir},
		{"data/output/", 0o777, 0, 0, tar.TypeDir}}
	if len(f.putTar) != 3 {
		t.Fatalf("seed tar must hold exactly 3 dirs and no files, got %+v", f.putTar)
	}
	for i := range want {
		if f.putTar[i] != want[i] {
			t.Fatalf("tar entry %d = %+v, want %+v", i, f.putTar[i], want[i])
		}
	}
	cfg := f.createReq["Image"]
	hc := f.createReq["HostConfig"].(map[string]any)
	if cfg != "alpine:3.20" || hc["Binds"].([]any)[0] != "vol-a:/data:rw" {
		t.Fatalf("create must bind the task volume at /data rw on the helper image: %v", f.createReq)
	}
	lb := f.createReq["Labels"].(map[string]any)
	if lb[sidecarLabel] != "1" || lb[sidecarWorkerLabel] != "w1" || lb[sidecarInstanceLabel] != "i1" {
		t.Fatalf("cleanup labels missing: %v", lb)
	}
	// The prep container is removed by a background goroutine whose DELETE the
	// fake records into f.calls: join it first, then read the fake's state under
	// its mutex (an unlocked read of f.calls raced that append under -race).
	e.waitPrepRemoval("vol-a")
	calls, rmDoneAt := f.snapshotCalls()
	for _, c := range calls {
		if strings.Contains(c, "/start") || strings.Contains(c, "/wait") {
			t.Fatalf("the prep container must never be started: %v", calls)
		}
	}
	if rmDoneAt.IsZero() {
		t.Fatalf("prep container was not removed: %v", calls)
	}
}

func TestPrepareDataVolumeEmitsSameLogLine(t *testing.T) {
	f := &fakeEngine{statMode: 0o777}
	e, logs := newPrepExec(t, f)
	if err := e.prepareDataVolume(context.Background(), "alpine:3.20", "vol-b", transport.MinIOCredentials{}); err != nil {
		t.Fatal(err)
	}
	l := logs.String()
	if !strings.Contains(l, `msg="data volume prepared"`) || !strings.Contains(l, "volume=vol-b") ||
		!strings.Contains(l, `output="`+strings.TrimSpace(wantLsOut)+`"`) {
		t.Fatalf("log line changed: %s", l)
	}
	if strings.Contains(l, "api data seed failed") {
		t.Fatalf("unexpected fallback: %s", l)
	}
	e.waitPrepRemoval("vol-b")
}

func TestPrepFailureStillFailsTheTask(t *testing.T) {
	for name, f := range map[string]*fakeEngine{
		"put 500":       {statMode: 0o777, putFail: true},
		"wrong mode":    {statMode: 0o755},
		"mode 1777 bad": {statMode: os.ModeSticky | 0o777},
	} {
		t.Run(name, func(t *testing.T) {
			e, logs := newPrepExec(t, f)
			err := e.prepareDataVolume(context.Background(), "alpine:3.20", "vol-c", transport.MinIOCredentials{})
			if err == nil || !strings.HasPrefix(err.Error(), "prepare /data: ") {
				t.Fatalf("a failed prep must fail with the legacy 'prepare /data:' error, got %v", err)
			}
			if !strings.Contains(logs.String(), "api data seed failed") {
				t.Fatalf("fallback not taken: %s", logs.String())
			}
			e.waitPrepRemoval("vol-c")
		})
	}
}

func TestCleanupVolumeWaitsForPrepContainerRemoval(t *testing.T) {
	f := &fakeEngine{statMode: 0o777, rmDelay: 250 * time.Millisecond}
	e, _ := newPrepExec(t, f)
	if _, err := e.prepareDataVolumeAPI(context.Background(), "alpine:3.20", "vol-d"); err != nil {
		t.Fatal(err)
	}
	e.cleanupVolume("vol-d")
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.rmDoneAt.IsZero() || f.volRmAt.Before(f.rmDoneAt) {
		t.Fatalf("volume removed before the prep container was gone: ctr=%v vol=%v", f.rmDoneAt, f.volRmAt)
	}
}
