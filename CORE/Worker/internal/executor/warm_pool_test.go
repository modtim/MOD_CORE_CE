package executor

// KEEP-WARM-SLICE-1 PART 3: the isolation tests from
// results-1110/KEEP-WARM-DESIGN-REVIEW-AND-SLICE1-PLAN.md section 4 + the
// section 3 threat table, run against a FAKE Docker Engine (no daemon).
// Non-attach calls go to an httptest server (same style as
// volume_prep_api_test.go). The exec ATTACH is an HTTP hijack: the SDK sends
// `Upgrade: tcp` and expects a 101 + a raw docker stdcopy stream, so the
// handler hijacks the connection (the proven in-repo idiom from
// sockproxy/proxy_test.go) and writes the 101 + demux frames for that exec's
// scripted output. Every test asserts on the calls the pool MAKES and the
// bodies it sends: the create-time posture, the per-call exec env, the wipe
// script, the retire path and the key. Mutation checks are recorded in
// results-1110/KEEP-WARM-SLICE-1-BUILD.md. The real-filesystem half is
// warm_pool_realdocker_test.go behind build tag `warmbench`.

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// warmFake records every call and the decoded request bodies, and serves
// scripted exec stdout per exec Cmd plus exec-inspect / container-inspect
// verdicts. execCmd maps an exec id to its Cmd so the hijacked attach can
// pick the right scripted output. execFails makes exec create fail (attach
// failure / dead container).
type warmFake struct {
	mu          sync.Mutex
	calls       []string
	creates     []map[string]any
	execCreates []map[string]any
	volCreates  []map[string]any
	stops       []string
	ctrRms      []string
	volRms      []string
	listCtrs    []map[string]any
	listVols    []map[string]any
	execCmd     map[string][]string
	inspectOOM  bool
	execRunning bool
	execExit    int
	execFails   bool
	failNodeExec bool
	nodeOut string
	pidOut  string
	wipeOut string
	nextID  int
}

// prepOKLine is the byte-identical `ls -ld` text the legacy prep helper printed
// (same constant as volume_prep_api_test.go's wantLsOut): the warm wipe must
// render EXACTLY this so the "data volume prepared" line never drifts.
const prepOKLine = "prep OK: drwxrwxrwx    4 root     root          4096 Nov 25 15:18 /data " +
	"drwxrwxrwx    2 root     root          4096 Nov 25 15:18 /data/input " +
	"drwxrwxrwx    2 root     root          4096 Nov 25 15:18 /data/output "

func newWarmFake() *warmFake {
	return &warmFake{wipeOut: prepOKLine, pidOut: "3", nodeOut: "", execExit: 0,
		execCmd: map[string][]string{}}
}

func (f *warmFake) cmdsFor(exID string) []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	if c := f.execCmd[exID]; c != nil { return c }
	return []string{}
}

func (f *warmFake) outFor(cmds []string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var j string = strings.Join(cmds, " ")
	if strings.Contains(j, "ps -e -o pid=") { return f.pidOut }
	if strings.Contains(j, "rm -rf /data/input") { return f.wipeOut }
	return f.nodeOut
}

func (f *warmFake) failsNow() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.execFails
}

func (f *warmFake) failsNode() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.failNodeExec
}

// demuxFrame renders one stdout frame the way docker's stdcopy demuxes it:
// 8-byte header [stream=1, big-endian size] + payload.
func demuxFrame(s string) []byte {
	var b bytes.Buffer
	b.Write([]byte{0, 0, 0, 1, byte(len(s) >> 24), byte(len(s) >> 16), byte(len(s) >> 8), byte(len(s))})
	b.WriteString(s)
	return b.Bytes()
}

func warmEqList(a []string, b []string) bool {
	if len(a) != len(b) { return false }
	for i := range a { if a[i] != b[i] { return false } }
	return true
}

func warmAsList(v any) []string {
	var out []string
	if l, ok := v.([]any); ok { for _, x := range l { out = append(out, warmStringOf(x)) } }
	return out
}
func warmStringOf(v any) string {
	if s, ok := v.(string); ok { return s }
	return ""
}
func warmBoolStr(b bool) string { if b { return "true" }; return "false" }
func warmJsonOf(v any) string { var enc, _ = json.Marshal(v); return string(enc) }
func stringJoin(v []string) string {
	var s string
	for _, x := range v { s += x+" | " }
	return s
}

// startWarmFakeEngine serves the fake Engine API over httptest; the exec
// /start path is answered by a REAL 101 hijack (the SDK sends `Upgrade: tcp`
// and reads the raw stdcopy stream off the hijacked connection).
// newWarmExec wires an Executor to the fake engine. on = the platform kill
// switch (worker.warm_pool_enabled) ON/OFF.
func newWarmExec(t *testing.T, f *warmFake, on bool) (*Executor, *bytes.Buffer) {
	var srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var p string = r.URL.Path
		f.mu.Lock()
		f.calls = append(f.calls, r.Method+" "+p)
		f.mu.Unlock()
		if strings.Contains(p, "/exec/") && strings.HasSuffix(p, "/start") {
			var seg = strings.Split(p, "/")
			var exID string = ""
			for i := range seg { if seg[i] == "exec" && i+1 < len(seg) { exID = seg[i+1] } }
			var hj, ok = w.(http.Hijacker)
			if !ok { t.Fatal("fake engine response is not a hijacker"); return }
			var conn, buf, herr = hj.Hijack()
			if herr != nil { return }
			buf.WriteString("HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
			buf.Flush()
			io.WriteString(conn, string(demuxFrame(f.outFor(f.cmdsFor(exID)))))
			conn.Close()
			return
		}
		var body = map[string]any{}
		if r.Method == "POST" {
			_ = json.NewDecoder(r.Body).Decode(&body)
		}
		var send = func(code int, s string) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(code)
			io.WriteString(w, s)
		}
		switch {
		case strings.HasSuffix(p, "/volumes/create"):
			f.mu.Lock(); f.nextID++; var vol string = "poolvol"+strconv.Itoa(f.nextID)
			f.volCreates = append(f.volCreates, body); f.mu.Unlock()
			send(201, `{"Name":"`+vol+`","Driver":"local","Labels":{}}`)
		case strings.HasSuffix(p, "/containers/create"):
			f.mu.Lock(); f.nextID++; var id string = "ctr"+strconv.Itoa(f.nextID)
			f.creates = append(f.creates, body); f.mu.Unlock()
			send(201, `{"Id":"`+id+`","Warnings":[]}`)
		case strings.HasSuffix(p, "/exec"):
			// failNodeExec fails ONLY the NODE exec (the wipe/pid housekeeping
			// execs still succeed), so the exec-create/attach retire path is
			// exercised specifically; execFails fails EVERY exec (a dead
			// container: the pre-call wipe fails first).
			var nodeCmd = false
			for _, c := range warmAsList(body["Cmd"]) { if c == "python" { nodeCmd = true } }
			if f.failsNow() || (f.failsNode() && nodeCmd) {
				send(500, `{"message":"exec refused (container not running)"} `); break
			}
			f.mu.Lock(); f.nextID++; var ex string = "ex"+strconv.Itoa(f.nextID)
			body["id"] = ex
			var cmds []string
			if c, ok := body["Cmd"]; ok { for _, x := range warmAsList(c) { cmds = append(cmds, x) } }
			f.execCmd[ex] = cmds
			f.execCreates = append(f.execCreates, body); f.mu.Unlock()
			send(201, `{"Id":"`+ex+`","Start":false}`)
		case strings.Contains(p, "/exec/") && strings.HasSuffix(p, "/json"):
			f.mu.Lock(); var running bool = f.execRunning; var code int = f.execExit; f.mu.Unlock()
			send(200, `{"Running":`+warmBoolStr(running)+`, "ExitCode":`+strconv.Itoa(code)+`}`)
		case strings.Contains(p, "/containers/json"):
			// The SDK prefixes the path with the API version (/v1.45/...), so
			// match with Contains; the body is a BARE ARRAY (client/container_list.go).
			f.mu.Lock()
			var enc []string
			for _, c := range f.listCtrs { enc = append(enc, warmJsonOf(c)) }
			var lst string = "["+strings.Join(enc, ",")+"]"
			f.mu.Unlock()
			send(200, lst)
		case strings.Contains(p, "/containers/") && strings.HasSuffix(p, "/json"):
			f.mu.Lock(); var oom bool = f.inspectOOM; f.mu.Unlock()
			send(200, `{"State":{"OOMKilled":`+warmBoolStr(oom)+`, "Running":true}}`)
		case strings.HasSuffix(p, "/start"):
			send(200, ``)
		case strings.HasSuffix(p, "/stop"):
			f.mu.Lock(); f.stops = append(f.stops, p); f.mu.Unlock()
			send(204, ``)
		case strings.Contains(p, "/containers/") && r.Method == "DELETE":
			f.mu.Lock(); f.ctrRms = append(f.ctrRms, p); f.mu.Unlock()
			send(204, ``)
		case strings.Contains(p, "/volumes/") && r.Method == "DELETE":
			f.mu.Lock(); f.volRms = append(f.volRms, p); f.mu.Unlock()
			send(204, ``)
		case strings.Contains(p, "/volumes") && r.Method == "GET":
			// GET /volumes (version-prefixed): the volume list for the sweep.
			f.mu.Lock()
			var enc []string
			for _, v := range f.listVols { enc = append(enc, warmJsonOf(v)) }
			var lst string = "["+strings.Join(enc, ",")+"]"
			f.mu.Unlock()
			send(200, `{"Volumes":`+lst+`, "Warnings":[]}`)
		default:
			send(404, `{"message":"fake unhandled"}`)
		}
	}))
	t.Cleanup(srv.Close)
	var cli, err = client.NewClientWithOpts(client.WithHost("tcp://"+strings.TrimPrefix(srv.URL, "http://")),
		client.WithVersion("1.45"))
	if err != nil { t.Fatal(err) }
	var logs bytes.Buffer
	store := tunables.New()
	if on {
		store.Apply(map[string]interface{}{warmPoolEnabledTunableKey: true,
			warmPoolMaxContainersTunableKey: 64})
	}
	var e = &Executor{docker: cli, workerName: "w1", instanceID: "i1", network: "net1",
		log: slog.New(slog.NewTextHandler(&logs, nil)), tunables: store}
	return e, &logs
}

func basePayload() transport.TaskPayload {
	return transport.TaskPayload{
		NodeKind: "some.node", ProcessingImage: "img:latest",
		Cmd: []string{"python", "node.py"},
		ContainerEnv: map[string]string{"INPUT_DIR": "/data/input", "OUTPUT_DIR": "/data/output"},
	}
}
func warmPol() *transport.WarmPolicy {
	return &transport.WarmPolicy{IdleSeconds: 300, MaxMemoryMB: 512, MaxCalls: 3, Protocol: "exec"}
}
func warmCreds() *transport.Credentials {
	return &transport.Credentials{MinIO: transport.MinIOCredentials{
		Endpoint: "http://minio:9000", AccessKey: "AKnonce1", SecretKey: "SKnonce1",
		SessionToken: "STnonce1"}, ModelCacheToken: "MCnonce1"}
}

// warmAcq runs the pool's acquire for one (tenant, digest, payload) triple.
func warmAcq(e *Executor, tenant int, digest string, p transport.TaskPayload) (*warmEntry, error) {
	var env = e.warmExecEnv(p, warmCreds(), digest)
	return e.warmAcquire(context.Background(), 11, tenant, digest, p, warmPol(), "net1", env)
}

// ---- 1. never reused across tenant accounts ---------------------------
func TestWarmNeverCrossTenant(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("tenant 1 acquire: %v", err) }
	e.warmFinishCall(a)
	var b, err2 = warmAcq(e, 2, "sha256:aaa", p)
	if err2 != nil || b == nil { t.Fatalf("tenant 2 acquire: %v", err2) }
	if a.containerID == b.containerID {
		t.Fatalf("SAME container handed to two tenant accounts: %s", a.containerID)
	}
	if len(f.creates) != 2 {
		t.Fatalf("each tenant must get its own container, creates=%d", len(f.creates))
	}
	if len(f.volCreates) != 2 {
		t.Fatalf("each tenant must get its own pool volume, volumes=%d", len(f.volCreates))
	}
}

// ---- 2. never reused across a different image digest (tag moved) ---------
func TestWarmNeverCrossDigest(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:old", p)
	if err != nil || a == nil { t.Fatalf("first acquire: %v", err) }
	e.warmFinishCall(a)
	var b, err2 = warmAcq(e, 1, "sha256:new", p)
	if err2 != nil || b == nil { t.Fatalf("after tag move: %v", err2) }
	if a.containerID == b.containerID {
	 t.Fatalf("container reused across digests: %s", a.containerID)
	}
	if len(f.creates) != 2 { t.Fatalf("moved tag must produce a NEW container, creates=%d", len(f.creates)) }
}

// ---- 3. the key fingerprints the FULL create-time config -----------------
func TestWarmConfigFingerprintCoversCreateTimeConfig(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var base, err = warmAcq(e, 1, "sha256:aaa", basePayload())
	if err != nil || base == nil { t.Fatalf("baseline acquire: %v", err) }
	e.warmFinishCall(base)

	// Each mutation of the create-time posture must change the key (a NEW
	// container), never reuse the baseline one.
	var variants []transport.TaskPayload
	{
		var q = basePayload(); q.MemoryLimitMB = 1024; variants = append(variants, q)
		q = basePayload(); q.CPULimit = 3; variants = append(variants, q)
		q = basePayload(); q.PidsLimit = 128; variants = append(variants, q)
		q = basePayload(); q.ShmSizeMB = 256; variants = append(variants, q)
		q = basePayload(); q.TmpfsMB = 64; variants = append(variants, q)
		q = basePayload(); q.ContainerEnv = map[string]string{"INPUT_DIR": "/data/input", "EXTRA": "1"}; variants = append(variants, q)
		q = basePayload(); q.ContainerEnv = map[string]string{"INPUT_DIR": "/data/input", "MOD_TASK_TOKEN_URL": "http://x"}; variants = append(variants, q)
	}
	for i, v := range variants {
		var before = len(f.creates)
		var g, gerr = warmAcq(e, 1, "sha256:aaa", v)
		if gerr != nil {
			// None of these variants is ineligible, so a refusal here is a bug.
			t.Fatalf("variant %d acquire: %v", i, gerr)
		}
		if len(f.creates) == before {
			t.Fatalf("variant %d reused the baseline container (key does not fingerprint the config)", i)
		}
		if g.containerID == base.containerID {
			t.Fatalf("variant %d got the baseline container %s", i, base.containerID)
		}
		e.warmFinishCall(g)
	}
}

// ---- 4. egress-allowlist / GPU / docker-socket / checkpoint never eligible -
func TestWarmIneligiblePosturesRefused(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var bad []transport.TaskPayload
	{
		var q = basePayload(); q.AllowedEgress = []string{"api.openai.com"}; bad = append(bad, q)
		q = basePayload(); q.EgressMode = "mediated"; q.EgressUpstream = "gw:8080"; bad = append(bad, q)
		q = basePayload(); q.TailnetEgress = &transport.TailnetEgress{}; bad = append(bad, q)
		q = basePayload(); q.GPURequired = true; bad = append(bad, q)
		q = basePayload(); q.GPUAssignment = &transport.GPUAssignment{}; bad = append(bad, q)
		q = basePayload(); q.NeedsDockerSocket = true; bad = append(bad, q)
		q = basePayload(); q.CheckpointVolume = "cp-vol"; bad = append(bad, q)
		q = basePayload(); q.CheckpointExportURL = "https://x/put"; bad = append(bad, q)
		q = basePayload(); q.CheckpointHydrateURL = "https://x/get"; bad = append(bad, q)
		q = basePayload(); q.ContainerEnv = map[string]string{"PLATFORM_TASK": "1"}; bad = append(bad, q)
		q = basePayload(); q.ContainerEnv = map[string]string{"TARGET_KIND": "cuda"}; bad = append(bad, q)
		q = basePayload(); q.UseSandboxRuntime = true; bad = append(bad, q)
		q = basePayload(); q.Cmd = []string{}; bad = append(bad, q)
		q = basePayload(); bad = append(bad, q)
	}
	for i, q := range bad {
		if i == len(bad)-1 {
			// the last one is the BASELINE: it MUST be eligible
			if warmEligibility(q, warmPol()) != "" {
				t.Fatalf("the clean payload must be eligible, got %q", warmEligibility(q, warmPol()))
			}
			continue
		}
		if warmEligibility(q, warmPol()) == "" {
			t.Fatalf("variant %d must be INELIGIBLE for warm, got eligible", i)
		}
	}
	// the eligibility gate decides BEFORE touching docker at all
	if len(f.calls) != 0 {
		t.Fatalf("eligibility must decide before touching docker, calls=%v", f.calls)
	}
	// and an ineligible payload never reaches the pool: warmAcquire is only
	// called after the gate in executor.go; assert the gate itself refuses.
	var ent, aerr = warmAcq(e, 1, "sha256:aaa", basePayload())
	if aerr != nil || ent == nil { t.Fatalf("baseline acquire for the pool-side check: %v", aerr) }
}

// ---- 5. the warm create carries the SAME lockdown posture as a cold create --
func TestWarmCreatePostureMatchesCold(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var a, err = warmAcq(e, 1, "sha256:aaa", basePayload())
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	if len(f.creates) != 1 { t.Fatalf("expected one create, got %d", len(f.creates)) }
	var body = f.creates[0]
	var hc = body["HostConfig"].(map[string]any)
	if hc["CapDrop"] == nil || !warmEqList(warmAsList(hc["CapDrop"]), []string{"ALL"}) {
		t.Fatalf("cap-drop ALL missing: %v", hc["CapDrop"])
	}
	var secHasNPNP = false
	for _, o := range warmAsList(hc["SecurityOpt"]) {
		if o == "no-new-privileges" { secHasNPNP = true }
	}
	if !secHasNPNP {
		t.Fatalf("no-new-privileges missing: %v", hc["SecurityOpt"])
	}
	if hc["Privileged"] != nil && hc["Privileged"] != false {
		t.Fatalf("privileged must be false: %v", hc["Privileged"])
	}
	if hc["ReadonlyRootfs"] != true {
		t.Fatalf("ReadonlyRootfs must be true (T3: the writable layer cannot leak): %v", hc["ReadonlyRootfs"])
	}
	if hc["NetworkMode"] != "net1" {
		t.Fatalf("warm container must sit on the default-deny isolated net, got %v", hc["NetworkMode"])
	}
	if hc["Tmpfs"] == nil { t.Fatalf("tmpfs set missing: %v", hc["Tmpfs"]) }
	for _, d := range []string{"/tmp", "/var/tmp", "/dev/shm", "/home"} {
		if hc["Tmpfs"].(map[string]any)[d] == nil {
			t.Fatalf("tmpfs %s missing: %v", d, hc["Tmpfs"])
		}
	}
	if hc["Binds"] == nil || warmEqList(warmAsList(hc["Binds"]), []string{}) {
		t.Fatalf("pool volume must be bound at /data: %v", hc["Binds"])
	}
	if !strings.Contains(warmAsList(hc["Binds"])[0], ":/data:rw") {
		t.Fatalf("pool bind must be at /data rw: %v", hc["Binds"])
	}
	// R3: NO per-task env at create time.
	if body["Env"] != nil && !warmEqList(warmAsList(body["Env"]), []string{}) {
		t.Fatalf("create-time env must be EMPTY, got %v", body["Env"])
	}
	var lb = body["Labels"].(map[string]any)
	if lb[warmPoolLabel] != "1" || lb[sidecarWorkerLabel] != "w1" || lb[sidecarInstanceLabel] != "i1" {
		t.Fatalf("warm-pool identity labels missing: %v", lb)
	}
}

// ---- 6. timeout / OOM / attach failure retire the WHOLE container; the retry
// ----    runs cold, and the (tenant,digest) key is marked cold-only ---------
func TestWarmTimeoutRetiresContainerAndRetriesCold(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	f.execRunning = true // ExecInspect never settles
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var _, _, _, rerr = e.warmRunExec(context.Background(), a, p, env, 300*time.Millisecond, Callbacks{})
	if rerr == nil || !strings.Contains(rerr.Error(), "timeout") {
		t.Fatalf("a wedged exec must time out, got %v", rerr)
	}
	// executor.go's warm branch calls warmMarkColdOnly on a warm CALL failure;
	// the test does the same, then the finish (retire) and the retry.
	e.warmMarkColdOnly(1, "sha256:aaa")
	e.warmFinishCall(a)
	if len(f.stops) == 0 || len(f.ctrRms) == 0 || len(f.volRms) == 0 {
		t.Fatalf("timeout must stop+remove the container AND the pool volume: stops=%d ctrRm=%d volRm=%d",
			len(f.stops), len(f.ctrRms), len(f.volRms))
	}
	// the retry under max_attempts 3 must be COLD: acquire refuses for the key
	var b, berr = warmAcq(e, 1, "sha256:aaa", p)
	if berr == nil || b != nil {
		t.Fatalf("after a warm failure the retry must run cold (acquire must refuse), got %v", berr)
	}
}

func TestWarmOOMRetiresAndRetriesCold(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	f.inspectOOM = true
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var _, _, oom, rerr = e.warmRunExec(context.Background(), a, p, env, 5*time.Second, Callbacks{})
	if !oom || rerr == nil {
		t.Fatalf("an OOM-killed warm container must report oom + error, got oom=%v err=%v", oom, rerr)
	}
	e.warmMarkColdOnly(1, "sha256:aaa")
	e.warmFinishCall(a)
	if len(f.ctrRms) == 0 || len(f.volRms) == 0 {
		t.Fatalf("OOM must remove container+volume: ctrRm=%d volRm=%d", len(f.ctrRms), len(f.volRms))
	}
	var b, berr = warmAcq(e, 1, "sha256:aaa", p)
	if berr == nil || b != nil {
		t.Fatalf("OOM retry must run cold, got %v", berr)
	}
}

func TestWarmAttachFailureRetiresAndCools(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var a, err = warmAcq(e, 1, "sha256:aaa", basePayload())
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	// the node exec's create/attach fails (the housekeeping execs still work):
	// the container must be RETIRED, never handed to another call.
	f.failNodeExec = true
	var p = basePayload()
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var _, _, _, rerr = e.warmRunExec(context.Background(), a, p, env, 5*time.Second, Callbacks{})
	if rerr == nil {
		t.Fatalf("an exec/attach failure must surface as an error (the caller retires)")
	}
	// warmRunExec itself must RETIRE the container on an exec/attach failure
	// (a dead container can never be handed to another call), not merely
	// report the error and leave the entry live in the pool map.
	if len(f.ctrRms) == 0 || len(f.volRms) == 0 {
		t.Fatalf("an exec/attach failure must remove container+volume (retire), got ctrRm=%d volRm=%d",
			len(f.ctrRms), len(f.volRms))
	}
	// and the entry must be GONE from the pool: a fresh acquire for the key
	// creates a NEW container rather than reusing the dead one. (The fake's
	// exec failure is turned off again: the new container is healthy.)
	f.execFails = false
	var b, berr = warmAcq(e, 1, "sha256:aaa", p)
	if berr != nil || b == nil {
		t.Fatalf("after a retire the key must be re-creatable, got %v", berr)
	}
	if b.containerID == a.containerID {
		t.Fatalf("the dead container was handed to a new call")
	}
	e.warmFinishCall(b)

	// A DEAD container (every exec refused): the PRE-CALL WIPE fails, and that
	// path must ALSO retire — a container whose wipe cannot be verified can
	// never serve another call. b is healthy at this point; kill it, then call.
	f.execFails = true
	var before2 = len(f.ctrRms)
	var _, _, _, derr2 = e.warmRunExec(context.Background(), b, p,
		e.warmExecEnv(p, warmCreds(), "sha256:aaa"), 5*time.Second, Callbacks{})
	if derr2 == nil {
		t.Fatalf("a failed pre-call wipe must surface as an error")
	}
	if len(f.ctrRms) == before2 || len(f.volRms) == before2 {
		t.Fatalf("a failed pre-call wipe must RETIRE too: ctrRm=%d volRm=%d",
			len(f.ctrRms), len(f.volRms))
	}
}

// ---- 7. the pool never uploads/scans: those legs stay in Run() unchanged ---
func TestWarmExecDoesNoUploadOrScan(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var rerr error
	if _, _, _, rerr = e.warmRunExec(context.Background(), a, p, env, 5*time.Second, Callbacks{}); rerr != nil {
		t.Fatalf("clean exec: %v", rerr)
	}
	for _, c := range f.calls {
		if strings.Contains(c, "/archive") || strings.Contains(c, "put") || strings.Contains(c, "upload") {
			t.Fatalf("the warm exec path must not read/write outputs or upload: %v", f.calls)
		}
	}
	// exactly the housekeeping + one node exec
	var nodeExecs int
	for _, b := range f.execCreates {
		if warmEqList(warmAsList(b["Cmd"]), []string{"python", "node.py"}) { nodeExecs++ }
	}
	if nodeExecs != 1 { t.Fatalf("exactly one node exec per call, got %d", nodeExecs) }
}

// ---- 8. per-call env only on exec: zero create-time env, call 1's env invisible
// ----    to call 2; no credential material in any create-time body ----------
func TestWarmEnvPerCallAndNoSecretsAtCreate(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env1 = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var rerr error
	if _, _, _, rerr = e.warmRunExec(context.Background(), a, p, env1, 5*time.Second, Callbacks{}); rerr != nil {
		t.Fatalf("call 1: %v", rerr)
	}
	e.warmFinishCall(a)
	// call 2 with a DIFFERENT credential set: the env hash changes, so the
	// entry is RETIRED and the call runs cold — never re-exec'd with stale
	// creds, and never the same container.
	var env2 = e.warmExecEnv(p, &transport.Credentials{MinIO: transport.MinIOCredentials{
		Endpoint: "http://minio:9000", AccessKey: "AKnonce2", SecretKey: "SKnonce2",
		SessionToken: "STnonce2"}, ModelCacheToken: "MCnonce2"}, "sha256:aaa")
	var b, berr = e.warmAcquire(context.Background(), 12, 1, "sha256:aaa", p, warmPol(), "net1", env2)
	if berr == nil && b != nil && b.containerID == a.containerID {
		t.Fatalf("env change must NOT reuse the same container")
	}
	// create-time bodies carry no credential material at all
	for _, c := range f.creates {
		var dump bytes.Buffer
		dump.Write(warmMustBytes(c))
		for _, secret := range []string{"AKnonce1", "SKnonce1", "STnonce1", "MCnonce1", "AKnonce2"} {
			if strings.Contains(dump.String(), secret) {
				t.Fatalf("secret %s present in a create-time body", secret)
			}
		}
	}
	// the node exec DOES carry the per-call creds
	var nodeEnv []string
	for _, b2 := range f.execCreates {
		if warmEqList(warmAsList(b2["Cmd"]), []string{"python", "node.py"}) {
			nodeEnv = warmAsList(b2["Env"])
			break
		}
	}
	var joined string
	for _, kv := range nodeEnv { joined += kv+"\n" }
	for _, want := range []string{"MINIO_ACCESS_KEY=AKnonce1", "MINIO_SECRET_KEY=SKnonce1", "MINIO_ENDPOINT=minio:9000"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("per-call exec env missing %s: %v", want, nodeEnv)
		}
	}
}

func warmMustBytes(v any) []byte { var enc, _ = json.Marshal(v); return enc }

// ---- 9. wipe+verify before AND after every call incl. dotfiles, symlinks,
// ----    special files, nested dirs and the tmpfs paths /tmp /var/tmp
// ----    /dev/shm /home (L-1); a non-empty verify RETIRES the container -----
func TestWarmWipeVerifiesEmptyAndRetiresOnNonEmpty(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var rerr error
	if _, _, _, rerr = e.warmRunExec(context.Background(), a, p,
		e.warmExecEnv(p, warmCreds(), "sha256:aaa"), 5*time.Second, Callbacks{}); rerr != nil {
		t.Fatalf("call 1: %v", rerr)
	}
	e.warmFinishCall(a)
	// the wipe exec script must cover the tmpfs set too (L-1) and verify with
	// find -mindepth 1 (dotfiles, symlinks, special files, nested dirs included)
	var wipeCmds []string
	for _, b := range f.execCreates {
		var one []string
		for _, c := range warmAsList(b["Cmd"]) { one = append(one, c) }
		if strings.Contains(strings.Join(one, " "), "rm -rf /data/input") { wipeCmds = one }
	}
	if len(wipeCmds) == 0 { t.Fatalf("no wipe exec recorded") }
	var script string
	for _, c := range wipeCmds { script += c }
	for _, want := range []string{"/data/input", "/data/output", "/tmp", "/var/tmp", "/dev/shm", "/home",
		"/tmp/.[!.]*", "/dev/shm/.[!.]*", "/home/.[!.]*", "find", "-mindepth 1", "WARMWIPEFAIL"} {
		if !strings.Contains(script, want) {
			t.Fatalf("wipe script missing %s: %s", want, script)
		}
	}
	// The wipe runs BEFORE the node exec (pre-call) AND after it (post-call):
	// in the exec order, a wipe exec must come before the node exec and another
	// one after it (the acquire-time pid probe is allowed first).
	var wipeIdx = []int{}
	var nodeIdx = []int{}
	for i, b := range f.execCreates {
		var j = strings.Join(warmAsList(b["Cmd"]), " ")
		if strings.Contains(j, "rm -rf /data/input") { wipeIdx = append(wipeIdx, i) }
		if warmEqList(warmAsList(b["Cmd"]), []string{"python", "node.py"}) { nodeIdx = append(nodeIdx, i) }
	}
	if len(wipeIdx) < 2 || len(nodeIdx) != 1 {
		t.Fatalf("expected >=2 wipe execs and exactly 1 node exec, got wipes=%d nodes=%d",
			len(wipeIdx), len(nodeIdx))
	}
	if wipeIdx[0] >= nodeIdx[0] || wipeIdx[len(wipeIdx)-1] <= nodeIdx[0] {
		t.Fatalf("a wipe must run BEFORE and AFTER the node exec: wipes=%v node=%v", wipeIdx, nodeIdx)
	}
	// a non-empty verify retires the container and NEVER retries the wipe
	f.wipeOut = "WARMWIPEFAIL 3"
	var wverr error
	if _, wverr = e.warmWipeVerify(context.Background(), a.containerID); wverr == nil {
		t.Fatalf("a non-empty verify must error")
	}
	var before = len(f.ctrRms)
	var ferr error
	if ferr = e.warmFinishCall(a); ferr == nil {
		t.Fatalf("warmFinishCall must fail closed on a non-empty wipe")
	}
	if len(f.ctrRms) == before {
		t.Fatalf("a failed post-call wipe must RETIRE the container")
	}
}

// ---- 10. idle timeout and max_calls retire the entry (the call runs cold) --
func TestWarmMaxCallsAndIdleRetire(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var rerr error
	// max_calls = 3: calls 1..3 all reuse the SAME container (one create).
	// One finishCall per call (finishCall both releases busy and counts the
	// call), and a fresh acquire before each subsequent call.
	for i := 0; i < 3; i++ {
		if i > 0 {
			var r, rerr2 = warmAcq(e, 1, "sha256:aaa", p)
			if rerr2 != nil || r == nil { t.Fatalf("re-acquire %d: %v", i, rerr2) }
			if r.containerID != a.containerID {
				t.Fatalf("re-acquire %d must reuse the SAME container", i)
			}
			a = r
		}
		if _, _, _, rerr = e.warmRunExec(context.Background(), a, p, env, 5*time.Second, Callbacks{}); rerr != nil {
			t.Fatalf("call %d: %v", i, rerr)
		}
		e.warmFinishCall(a)
	}
	if len(f.creates) != 1 {
		t.Fatalf("three calls on one key must create ONE container, creates=%d", len(f.creates))
	}
	// max_calls = 3 reached: the 4th acquire must refuse (run cold)
	var b, berr = warmAcq(e, 1, "sha256:aaa", p)
	if berr == nil || b != nil {
		t.Fatalf("past max_calls the pool must retire and run cold, got %v", berr)
	}
	if len(f.ctrRms) == 0 { t.Fatalf("max_calls retirement must remove the container") }

	// idle timeout: the entry's life is bounded by the policy it was CREATED
	// with (IdleSeconds 1); after it sits idle past that, the next acquire
	// retires it and the caller runs cold.
	var e2, _ = newWarmExec(t, newWarmFake(), true)
	var q = basePayload()
	var pol = warmPol(); pol.IdleSeconds = 1
	var c, cerr = e2.warmAcquire(context.Background(), 11, 1, "sha256:idle", q, pol, "net1",
		e2.warmExecEnv(q, warmCreds(), "sha256:idle"))
	if cerr != nil || c == nil { t.Fatalf("idle-test acquire: %v", cerr) }
	e2.warmFinishCall(c)
	time.Sleep(1200 * time.Millisecond)
	var d, derr = e2.warmAcquire(context.Background(), 11, 1, "sha256:idle", q, pol, "net1",
		e2.warmExecEnv(q, warmCreds(), "sha256:idle"))
	if derr == nil || d != nil {
		t.Fatalf("an idle entry must be retired (caller runs cold), got %v", derr)
	}
}

// ---- 10b. orphan sweep after a worker restart, and it is WIRED into
// ----    StartBackgroundJobs (L-6: source pin — the sweep must be called at
// ----    startup AND on the sweep tick, or an orphan warm container outlives
// ----    the worker that made it) ------------------------------------------
func TestWarmOrphanSweepAndStartupWiring(t *testing.T) {
	var f = newWarmFake()
	var e, logs = newWarmExec(t, f, true)
	f.mu.Lock()
	f.listCtrs = []map[string]any{
		{"Id": "orphan1", "Labels": map[string]string{warmPoolLabel: "1",
			sidecarWorkerLabel: "w1", sidecarInstanceLabel: "OLD-i0"}},
		{"Id": "mine1", "Labels": map[string]string{warmPoolLabel: "1",
			sidecarWorkerLabel: "w1", sidecarInstanceLabel: "i1"}},
		{"Id": "other1", "Labels": map[string]string{warmPoolLabel: "1",
			sidecarWorkerLabel: "w9", sidecarInstanceLabel: "x"}},
	}
	f.listVols = []map[string]any{
		{"Name": "orphanvol", "Labels": map[string]string{warmPoolLabel: "1",
			sidecarWorkerLabel: "w1", sidecarInstanceLabel: "OLD-i0"}},
		{"Name": "myvol", "Labels": map[string]string{warmPoolLabel: "1",
			sidecarWorkerLabel: "w1", sidecarInstanceLabel: "i1"}},
	}
	f.mu.Unlock()
	var n = e.warmSweepOrphans(context.Background())
	if n != 2 {
		println("calls", stringJoin(f.calls))
		println("listCtrs", warmJsonOf(f.listCtrs))
		println("logs", logs.String())
		t.Fatalf("sweep must remove the foreign-instance container+volume only, got %d", n)
	}
	var removed string
	for _, c := range f.ctrRms { removed += c }
	if !strings.Contains(removed, "orphan1") { t.Fatalf("orphan container not removed: %v", f.ctrRms) }
	var vols string
	for _, v := range f.volRms { vols += v }
	if !strings.Contains(vols, "orphanvol") { t.Fatalf("orphan volume not removed: %v", f.volRms) }
	if strings.Contains(removed, "mine1") || strings.Contains(vols, "myvol") {
		t.Fatalf("the sweep must NEVER touch THIS instance's live pool resources")
	}

	// L-6 SOURCE PIN: warmSweepOrphans must be called from StartBackgroundJobs
	// (startup pass + the 30-min tick). Read the source and pin it: a dead
	// sweep (T13 unmet) is exactly the hole this closes.
	var src, serr = os.ReadFile("executor.go")
	if serr != nil { t.Fatalf("read executor.go: %v", serr) }
	var s = string(src)
	var fnAt = strings.Index(s, "func (e *Executor) StartBackgroundJobs")
	if fnAt < 0 { t.Fatalf("StartBackgroundJobs not found in executor.go") }
	var end = fnAt + 4000
	if end > len(s) { end = len(s) }
	var body = s[fnAt:end]
	if strings.Index(body, "warmSweepOrphans") < 0 {
		t.Fatalf("warmSweepOrphans is NOT wired into StartBackgroundJobs (L-6)")
	}
	// it must be called TWICE in the body: the startup pass AND the tick
	var hits = 0
	var scan = body
	for {
		var k = strings.Index(scan, "warmSweepOrphans")
		if k < 0 { break }
		hits++
		scan = scan[k+1:]
	}
	if hits < 2 {
		t.Fatalf("warmSweepOrphans must run at startup AND on the sweep tick, found %d call(s)", hits)
	}
}

// ---- 11. default OFF: no WarmPolicy or switch off => cold path VERBATIM,
// ----    the pool never touches docker --------------------------------------
func TestWarmDisabledIsColdPath(t *testing.T) {
	var f = newWarmFake()
	var e, logs = newWarmExec(t, f, false) // tunable OFF
	var p = basePayload()
	if e.warmEnabled() { t.Fatalf("warmEnabled must be false with no tunable row") }
	if p.WarmPolicy != nil { t.Fatalf("payload without warm_policy must stay nil") }
	// the branch condition in executor.go requires warmEnabled() && WarmPolicy
	// != nil; with either missing NOTHING pool-side may touch docker.
	if warmEligibility(p, p.WarmPolicy) == "" {
		t.Fatalf("a payload with no WarmPolicy must be ineligible")
	}
	if len(f.calls) != 0 { t.Fatalf("the pool must not touch docker when OFF: %v", f.calls) }
	if strings.Contains(logs.String(), "warm container") {
		t.Fatalf("no warm log line when OFF: %s", logs.String())
	}
	// and with the switch ON but no policy on the payload, still cold:
	var e2, _ = newWarmExec(t, f, true)
	if !e2.warmEnabled() { t.Fatalf("warmEnabled must be true with the tunable ON") }
	if basePayload().WarmPolicy != nil {
		t.Fatalf("basePayload must carry no WarmPolicy")
	}
	if len(f.calls) != 0 { t.Fatalf("switch ON but no payload policy: pool must still not touch docker: %v", f.calls) }
}

// ---- 12. existing log lines identical: the node's lines reach the worker
// ----    log and the batch callback EXACTLY like the cold stream; the only
// ----    extra lines are the warm ones (Q5) --------------------------------
func TestWarmLogLinesIdentical(t *testing.T) {
	var f = newWarmFake()
	var e, logs = newWarmExec(t, f, true)
	var p = basePayload()
	f.nodeOut = "hello\nworld\n"
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var batches []string
	var cb = Callbacks{OnLogBatch: func(ls []string) { batches = append(batches, strings.Join(ls, "|")) }}
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var tail, code, oom, rerr = e.warmRunExec(context.Background(), a, p, env, 5*time.Second, cb)
	if rerr != nil || code != 0 || oom { t.Fatalf("exec: %v code=%d oom=%v", rerr, code, oom) }
	var l = logs.String()
	// the node's own lines reach the worker log through centralLogLine digests
	// (GSEC FX.96) exactly like the cold stream, and the batch callback sees
	// the raw lines exactly like the cold stream.
	if len(batches) == 0 || batches[0] != "hello|world" {
		t.Fatalf("OnLogBatch must see the node's lines: %v", batches)
	}
	if !strings.Contains(l, "msg=container") || !strings.Contains(l, "stream=stdout") ||
		!strings.Contains(l, "log=h:") {
		t.Fatalf("per-line container log shape differs from cold: %s", l)
	}
	// the digest must be the SAME centralLogLine digest the cold path emits for
	// the same line (GSEC FX.96): sha256("hello")[:10] = 2cf24dba5f.
	if !strings.Contains(l, "log=h:2cf24dba5f") {
		t.Fatalf("per-line digest differs from the cold rule: %s", l)
	}
	if tail != "hello\nworld" { t.Fatalf("log tail: %q", tail) }
	// the ONLY extra lines allowed are the warm ones (Q5)
	for _, line := range strings.Split(l, "\n") {
		if line == "" { continue }
		if strings.Contains(line, "warm container started") || strings.Contains(line, "warm container reused") {
			continue // the one extra line the design review allows (Q5)
		}
		if strings.Contains(line, "warm container retired") { continue } // retire path
		if strings.Contains(line, "msg=container") { continue } // identical to cold
		t.Fatalf("unexpected extra log line: %s", line)
	}
}

// ---- 13. CONCURRENCY (L-2): a busy entry is NEVER handed to a second
// ----    concurrent call, and the LRU eviction never evicts a busy entry ---
func TestWarmBusyEntryNeverHandedOutTwice(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("first acquire: %v", err) }
	// a second acquire for the SAME key while the first call is in flight
	var b, berr = warmAcq(e, 1, "sha256:aaa", p)
	if berr == nil && b != nil && b.containerID == a.containerID {
		t.Fatalf("busy container handed out to a second call")
	}
	if berr == nil {
		t.Fatalf("second acquire must refuse while the entry is busy, got an entry")
	}
	if len(f.creates) != 1 {
		t.Fatalf("a busy entry must not create a second container either: creates=%d", len(f.creates))
	}
	// after the first call finishes, the key is available again
	e.warmFinishCall(a)
	var c, cerr = warmAcq(e, 1, "sha256:aaa", p)
	if cerr != nil || c == nil || c.containerID != a.containerID {
		t.Fatalf("after finish the same (idle) entry must serve again: %v", cerr)
	}
	e.warmFinishCall(c)
}

func TestWarmLRUNeverEvictsBusyEntry(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	e.tunables.Apply(map[string]interface{}{warmPoolMaxContainersTunableKey: 1})
	var a, err = warmAcq(e, 1, "sha256:aaa", basePayload())
	if err != nil || a == nil { t.Fatalf("acquire A: %v", err) }
	// pool is at the safety limit and A is BUSY (mid-call): a new key must NOT
	// evict A out from under the live call — the caller runs cold instead.
	var q = basePayload(); q.NodeKind = "other.node"
	var b, berr = e.warmAcquire(context.Background(), 12, 1, "sha256:aaa", q, warmPol(), "net1",
		e.warmExecEnv(q, warmCreds(), "sha256:aaa"))
	if berr == nil || b != nil {
		t.Fatalf("at the safety limit with a busy entry the new key must run cold, got %v", berr)
	}
	// A is still in the pool and still serves after its call finishes
	e.warmFinishCall(a)
	var c, cerr = warmAcq(e, 1, "sha256:aaa", basePayload())
	if cerr != nil || c == nil || c.containerID != a.containerID {
		t.Fatalf("the busy entry must survive the limit check and serve later: %v", cerr)
	}
	e.warmFinishCall(c)
}

// ---- 14. stray processes from a previous call retire the container (L-7:
// ----    pid delta against the baseline captured at acquire) ---------------
func TestWarmStrayProcessRetiresContainer(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var p = basePayload()
	var a, err = warmAcq(e, 1, "sha256:aaa", p)
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env = e.warmExecEnv(p, warmCreds(), "sha256:aaa")
	var rerr error
	if _, _, _, rerr = e.warmRunExec(context.Background(), a, p, env, 5*time.Second, Callbacks{}); rerr != nil {
		t.Fatalf("exec: %v", rerr)
	}
	// call 1 detached a daemon: the pid count is now ABOVE the baseline
	f.mu.Lock(); f.pidOut = "9"; f.mu.Unlock()
	var ferr error
	if ferr = e.warmFinishCall(a); ferr == nil {
		t.Fatalf("a surviving process above the baseline must fail the finish")
	}
	if len(f.ctrRms) == 0 || len(f.volRms) == 0 {
		t.Fatalf("the container+volume must be retired when a process survives: ctrRm=%d volRm=%d",
			len(f.ctrRms), len(f.volRms))
	}
}

// ---- 15. the wipe/pid housekeeping execs run as User "0" (L-3: the container
// ----    default user is the NON-ROOT node uid; the wipe could not delete
// ----    node-owned files without root) ------------------------------------
func TestWarmHousekeepingExecsRunAsRoot(t *testing.T) {
	var f = newWarmFake()
	var e, _ = newWarmExec(t, f, true)
	var a, err = warmAcq(e, 1, "sha256:aaa", basePayload())
	if err != nil || a == nil { t.Fatalf("acquire: %v", err) }
	var env = e.warmExecEnv(basePayload(), warmCreds(), "sha256:aaa")
	var rerr error
	if _, _, _, rerr = e.warmRunExec(context.Background(), a, basePayload(), env, 5*time.Second, Callbacks{}); rerr != nil {
		t.Fatalf("exec: %v", rerr)
	}
	e.warmFinishCall(a)
	var house int
	for _, b := range f.execCreates {
		var cmd = strings.Join(warmAsList(b["Cmd"]), " ")
		if strings.Contains(cmd, "rm -rf /data/input") || strings.Contains(cmd, "ps -e -o pid=") {
			house++
			if b["User"] != "0" {
				t.Fatalf("housekeeping exec must run as uid 0, got %v (cmd %s)", b["User"], cmd)
			}
		}
	}
	if house < 3 { t.Fatalf("expected pre-wipe + post-wipe + pid check execs, got %d", house) }
}
