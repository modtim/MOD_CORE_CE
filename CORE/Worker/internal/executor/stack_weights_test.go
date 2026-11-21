package executor

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/weights"
	"gopkg.in/yaml.v3"
)

// envOf parses a compose YAML and returns the parsed `environment` values for
// each service, keyed by service name, as string maps regardless of the
// source form (mapping or K=V list).
func envOf(t *testing.T, y string) map[string]map[string]string {
	t.Helper()
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(y), &doc); err != nil {
		t.Fatalf("parse output: %v", err)
	}
	if len(doc.Content) == 0 || doc.Content[0].Kind != yaml.MappingNode {
		t.Fatalf("output is not a mapping: %s", y)
	}
	svcs := mapGet(doc.Content[0], "services")
	if svcs == nil || svcs.Kind != yaml.MappingNode {
		t.Fatalf("output has no services mapping: %s", y)
	}
	out := map[string]map[string]string{}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		name := svcs.Content[i].Value
		env := mapGet(svcs.Content[i+1], "environment")
		if env == nil {
			continue
		}
		m := map[string]string{}
		switch env.Kind {
		case yaml.MappingNode:
			for j := 0; j+1 < len(env.Content); j += 2 {
				m[env.Content[j].Value] = env.Content[j+1].Value
			}
		case yaml.SequenceNode:
			for _, e := range env.Content {
				if k, v, ok := cutEnvKV(e.Value); ok {
					m[k] = v
				}
			}
		}
		out[name] = m
	}
	return out
}

func assertHF1(t *testing.T, got map[string]map[string]string, svc string) {
	t.Helper()
	if got[svc][hfOfflineVar] != "1" {
		t.Fatalf("service %q: HF_HUB_OFFLINE = %q, want \"1\" (full env: %v)", svc, got[svc][hfOfflineVar], got[svc])
	}
}

func TestInjectHFOfflineMapEnv(t *testing.T) {
	in := `
services:
  vllm:
    image: vllm:latest
    environment:
      MODEL_PATH: /models
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	envs := envOf(t, out)
	assertHF1(t, envs, "vllm")
	if envs["vllm"]["MODEL_PATH"] != "/models" {
		t.Fatalf("MODEL_PATH lost: %v", envs["vllm"])
	}
}

func TestInjectHFOfflineListEnv(t *testing.T) {
	in := `
services:
  vllm:
    image: vllm:latest
    environment:
      - MODEL_PATH=/models
      - PORT=8000
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	envs := envOf(t, out)
	assertHF1(t, envs, "vllm")
	if envs["vllm"]["MODEL_PATH"] != "/models" || envs["vllm"]["PORT"] != "8000" {
		t.Fatalf("list entries lost: %v", envs["vllm"])
	}
}

func TestInjectHFOfflineMissingEnv(t *testing.T) {
	in := `
services:
  vllm:
    image: vllm:latest
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	envs := envOf(t, out)
	assertHF1(t, envs, "vllm")
	if len(envs["vllm"]) != 1 {
		t.Fatalf("expected only HF_HUB_OFFLINE to be created, got %v", envs["vllm"])
	}
}

func TestInjectHFOfflineOverridesExisting(t *testing.T) {
	in := `
services:
  vllm:
    image: vllm:latest
    environment:
      HF_HUB_OFFLINE: 0
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	envs := envOf(t, out)
	assertHF1(t, envs, "vllm")
	// Exactly one entry — the override replaced, not appended.
	if len(envs["vllm"]) != 1 {
		t.Fatalf("expected a single HF_HUB_OFFLINE entry, got %v", envs["vllm"])
	}
}

func TestInjectHFOfflineOverridesListValue(t *testing.T) {
	in := `
services:
  vllm:
    image: vllm:latest
    environment:
      - HF_HUB_OFFLINE=0
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	envs := envOf(t, out)
	assertHF1(t, envs, "vllm")
	if len(envs["vllm"]) != 1 {
		t.Fatalf("expected a single HF_HUB_OFFLINE entry, got %v", envs["vllm"])
	}
}

func TestInjectHFOfflineTwoServices(t *testing.T) {
	in := `
services:
  a:
    image: a:1
    environment:
      A: "1"
  b:
    image: b:1
    environment:
      - B=2
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	envs := envOf(t, out)
	assertHF1(t, envs, "a")
	assertHF1(t, envs, "b")
	if envs["a"]["A"] != "1" || envs["b"]["B"] != "2" {
		t.Fatalf("existing vars lost: %v", envs)
	}
}

func TestInjectHFOfflineInvalidYAML(t *testing.T) {
	if _, err := injectHFOffline("services: [unclosed"); err == nil {
		t.Fatal("expected an error for invalid YAML, got nil")
	}
}

func TestInjectHFOfflinePreservesOtherKeys(t *testing.T) {
	in := `
services:
  vllm:
    image: vllm:latest
    ports:
      - "8000:8000"
    volumes:
      - /models:/models
    environment:
      MODEL_PATH: /models
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatalf("parse output: %v", err)
	}
	svc := mapGet(doc.Content[0], "services").Content[1]
	img := mapGet(svc, "image")
	if img == nil || img.Value != "vllm:latest" {
		t.Fatalf("image changed: %v", img)
	}
	if ports := mapGet(svc, "ports"); ports == nil || ports.Content[0].Value != "8000:8000" {
		t.Fatalf("ports changed: %v", ports)
	}
	if vols := mapGet(svc, "volumes"); vols == nil || vols.Content[0].Value != "/models:/models" {
		t.Fatalf("volumes changed: %v", vols)
	}
	assertHF1(t, envOf(t, out), "vllm")
}

func TestInjectHFOfflineNoServicesKey(t *testing.T) {
	in := `
version: "3"
networks:
  default:
    name: net
`
	out, err := injectHFOffline(in)
	if err != nil {
		t.Fatalf("injectHFOffline: %v", err)
	}
	if out != in {
		t.Fatalf("input must be returned unchanged when there are no services:\n in=%s\nout=%s", in, out)
	}
}

// --- prepareStackWeights / toWeightRefs -------------------------------------

const testCompose = `
services:
  vllm:
    image: vllm:latest
    ports:
      - "8000:8000"
`

type weightFetchStub struct {
	calls     int
	lastCache string
	lastRefs  []weights.Ref
	lastCreds transport.HarborCredentials
	fetched   bool
	err       error
}

func (s *weightFetchStub) call(ctx context.Context, cacheRoot string, refs []weights.Ref, creds transport.HarborCredentials) (bool, error) {
	s.calls++
	s.lastCache = cacheRoot
	s.lastRefs = refs
	s.lastCreds = creds
	return s.fetched, s.err
}

func setFetchStub(t *testing.T, s *weightFetchStub) {
	t.Helper()
	prev := fetchWeights
	fetchWeights = s.call
	t.Cleanup(func() { fetchWeights = prev })
}

func TestToWeightRefs(t *testing.T) {
	in := []transport.WeightRef{{
		ArtifactRef: "registry.example.com/tenant-1/granite",
		RepoID:      "ibm-granite/granite-3.3-2b",
		Revision:    "main",
		Files:       []transport.WeightFile{{Path: "a.gguf", SHA256: "abc", Size: 10}},
	}}
	got := toWeightRefs(in)
	want := []weights.Ref{{
		ArtifactRef: "registry.example.com/tenant-1/granite",
		RepoID:      "ibm-granite/granite-3.3-2b",
		Revision:    "main",
		Files:       []weights.File{{Path: "a.gguf", SHA256: "abc", Size: 10}},
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("toWeightRefs = %+v, want %+v", got, want)
	}
	if got := toWeightRefs(nil); got != nil {
		t.Fatalf("toWeightRefs(nil) = %v, want nil", got)
	}
}

func TestPrepareStackWeightsFetchesAndGoesOffline(t *testing.T) {
	stub := &weightFetchStub{fetched: true}
	setFetchStub(t, stub)
	logs := [][]string{}
	p := &transport.TaskPayload{Weights: []transport.WeightRef{{
		ArtifactRef: "example.com/tenant-1/granite",
		RepoID:      "ibm-granite/granite",
		Revision:    "rev1",
		Files:       []transport.WeightFile{{Path: "m.gguf", Size: 5}, {Path: "t.gguf", Size: 6}},
	}}}
	creds := &transport.Credentials{
		TenantHarbor: transport.HarborCredentials{Registry: "example.com", Username: "u", Password: "p"},
	}
	out, err := prepareStackWeights(context.Background(), p, testCompose, creds, func(msgs ...string) {
		logs = append(logs, msgs)
	})
	if err != nil {
		t.Fatalf("prepareStackWeights: %v", err)
	}
	if stub.calls != 1 {
		t.Fatalf("fetch called %d times, want 1", stub.calls)
	}
	if stub.lastCache != hfCachePath() {
		t.Fatalf("cache root = %q, want %q", stub.lastCache, hfCachePath())
	}
	if len(stub.lastRefs) != 1 || stub.lastRefs[0].RepoID != "ibm-granite/granite" ||
		stub.lastRefs[0].Revision != "rev1" || len(stub.lastRefs[0].Files) != 2 ||
		stub.lastRefs[0].Files[1].Size != 6 {
		t.Fatalf("refs not mapped: %+v", stub.lastRefs)
	}
	if stub.lastCreds.Username != "u" || stub.lastCreds.Registry != "example.com" {
		t.Fatalf("tenant creds not selected: %+v", stub.lastCreds)
	}
	assertHF1(t, envOf(t, out), "vllm")
	if len(logs) != 1 || len(logs[0]) != 1 || logs[0][0] != "weights: ibm-granite/granite@rev1 ready (2 files)" {
		t.Fatalf("logs = %v", logs)
	}
}

func TestPrepareStackWeightsErrorNamesRepo(t *testing.T) {
	stub := &weightFetchStub{err: fmt.Errorf("weights: repo ibm-granite/granite: resolve: denied")}
	setFetchStub(t, stub)
	p := &transport.TaskPayload{Weights: []transport.WeightRef{{
		ArtifactRef: "harbor/granite", RepoID: "ibm-granite/granite", Revision: "r",
		Files: []transport.WeightFile{{Path: "m.gguf"}},
	}}}
	out, err := prepareStackWeights(context.Background(), p, testCompose, nil, nil)
	if err == nil {
		t.Fatal("want error, got nil")
	}
	if !strings.Contains(err.Error(), "ibm-granite/granite") {
		t.Fatalf("error must name the repo: %v", err)
	}
	if strings.Contains(err.Error(), "secret-password") {
		t.Fatalf("error must not contain credentials: %v", err)
	}
	if out != testCompose {
		t.Fatalf("compose must be unchanged on error:\n%s", out)
	}
}

func TestPrepareStackWeightsIncompleteLeavesOnline(t *testing.T) {
	stub := &weightFetchStub{fetched: false}
	setFetchStub(t, stub)
	p := &transport.TaskPayload{Weights: []transport.WeightRef{{
		ArtifactRef: "harbor/g", RepoID: "o/n", Revision: "r", Files: []transport.WeightFile{{Path: "m.gguf"}},
	}}}
	out, err := prepareStackWeights(context.Background(), p, testCompose, nil, nil)
	if err != nil {
		t.Fatalf("partial fetch must not error (HF_HUB_OFFLINE stays off): %v", err)
	}
	if envOf(t, out)["vllm"][hfOfflineVar] == "1" {
		t.Fatalf("HF_HUB_OFFLINE must not be set when fetch is incomplete")
	}
}

func TestPrepareStackWeightsNoWeightsNoOp(t *testing.T) {
	stub := &weightFetchStub{}
	setFetchStub(t, stub)
	logf := func(...string) { t.Fatal("logf must not be called with no weights") }
	out, err := prepareStackWeights(context.Background(), &transport.TaskPayload{}, testCompose, nil, logf)
	if err != nil {
		t.Fatalf("prepareStackWeights: %v", err)
	}
	if stub.calls != 0 {
		t.Fatalf("fetch called %d times with no weights, want 0", stub.calls)
	}
	if out != testCompose {
		t.Fatalf("compose must be byte-for-byte unchanged:\n in=%s\nout=%s", testCompose, out)
	}
}

// --- migrate materializes weights too --------------------------------------

// migrateWeightsTaskPayload returns a migrate payload carrying one weight ref
// plus a single-service compose spec (the same shape a stack task carries).
func migrateWeightsTaskPayload(withWeights bool) *transport.Task {
	p := transport.TaskPayload{
		StackOp:            "migrate",
		StackProject:       "modstack7g1",
		MigrateFromProject: "modstack7g0",
		ComposeSpec:        testCompose,
	}
	if withWeights {
		p.Weights = []transport.WeightRef{{
			ArtifactRef: "harbor/granite",
			RepoID:      "ibm-granite/granite",
			Revision:    "rev1",
			Files:       []transport.WeightFile{{Path: "m.gguf", Size: 5}},
		}}
	}
	return &transport.Task{Payload: p}
}

// fakeMigrateExec fakes every `docker compose` shell-out the migrate path makes
// (pinStackSubnets / ensureExternalNetworks / freeze / snapshot / restore / up /
// heal / down) and records the compose file content captured at `up` time — the
// moment the target host actually reads the spec.
func fakeMigrateExec(t *testing.T, upCompose *string) func() {
	t.Helper()
	restore := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		full := name + " " + strings.Join(arg, " ")
		if strings.Contains(full, "up -d") {
			for i, a := range arg {
				if a == "-f" && i+1 < len(arg) {
					data, rerr := os.ReadFile(arg[i+1])
					if rerr != nil {
						t.Errorf("read compose file %s: %v", arg[i+1], rerr)
					} else {
						*upCompose = string(data)
					}
				}
			}
		}
		return exec.CommandContext(ctx, "true")
	})
	return restore
}

func TestMigrateMaterializesWeightsBeforeComposeWrite(t *testing.T) {
	stub := &weightFetchStub{fetched: true}
	setFetchStub(t, stub)
	var upCompose string
	restore := fakeMigrateExec(t, &upCompose)
	defer restore()

	e := &Executor{}
	_, err := e.runStackOp(context.Background(), migrateWeightsTaskPayload(true), nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("migrate with weights: %v", err)
	}
	if stub.calls != 1 {
		t.Fatalf("fetch called %d times, want 1", stub.calls)
	}
	if stub.lastCache != hfCachePath() {
		t.Fatalf("cache root = %q, want %q", stub.lastCache, hfCachePath())
	}
	if len(stub.lastRefs) != 1 || stub.lastRefs[0].RepoID != "ibm-granite/granite" {
		t.Fatalf("refs not mapped: %+v", stub.lastRefs)
	}
	if upCompose == "" {
		t.Fatal("compose up never saw a compose file")
	}
	assertHF1(t, envOf(t, upCompose), "vllm")
}

func TestMigrateWithoutWeightsSkipsFetch(t *testing.T) {
	stub := &weightFetchStub{fetched: true}
	setFetchStub(t, stub)
	var upCompose string
	restore := fakeMigrateExec(t, &upCompose)
	defer restore()

	e := &Executor{}
	_, err := e.runStackOp(context.Background(), migrateWeightsTaskPayload(false), nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("migrate without weights: %v", err)
	}
	if stub.calls != 0 {
		t.Fatalf("fetch called %d times with no weights, want 0", stub.calls)
	}
	if envOf(t, upCompose)["vllm"][hfOfflineVar] == "1" {
		t.Fatalf("HF_HUB_OFFLINE must not be set when the task carries no weights")
	}
}

func TestMigratePartialFetchStillProceeds(t *testing.T) {
	stub := &weightFetchStub{fetched: false}
	setFetchStub(t, stub)
	var upCompose string
	restore := fakeMigrateExec(t, &upCompose)
	defer restore()

	e := &Executor{}
	res, err := e.runStackOp(context.Background(), migrateWeightsTaskPayload(true), nil, time.Now(), Callbacks{})
	if err != nil {
		t.Fatalf("partial fetch must not fail the migrate: %v", err)
	}
	if res == nil {
		t.Fatal("expected a result for a proceeded migrate")
	}
	if stub.calls != 1 {
		t.Fatalf("fetch called %d times, want 1", stub.calls)
	}
	if envOf(t, upCompose)["vllm"][hfOfflineVar] == "1" {
		t.Fatalf("HF_HUB_OFFLINE must stay off on a partial fetch (stack stays online)")
	}
}
