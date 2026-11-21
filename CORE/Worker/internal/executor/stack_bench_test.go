// Tests for the stack capacity bench (27.6.s1). The gateway is an httptest
// server that streams SSE chat deltas with scripted delays — the same seam
// discipline gatewayscrape_test.go uses (gatewayHTTPDo is a func var), plus
// benchTargets, which stands "this stack's gateway listens here" without a
// daemon. Every assertion is on timing the fake server was TOLD to produce, so
// these tests fail when the arithmetic is wrong rather than when a server is
// slow: a real stack's rate belongs in a heartbeat, never in a test's
// expectation.

package executor

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const benchModelsJSON = `{"object":"list","data":[{"id":"bench-model","object":"model"}]}`

// pad pushes each SSE chunk past the server's write buffer, so the fake really
// does deliver one delta at a time — which is the only thing that makes a TTFT
// measurement testable at all. Real gateways chunk-flush on their own.
func benchPad() string { return strings.Repeat("p", 8*1024) }

func benchRoleLine() string {
	return "data: " + `{"choices":[{"delta":{"role":"assistant"},"pad":"` + benchPad() + `"}]}` + "\n\n"
}

func benchDeltaLine() string {
	return "data: " + `{"choices":[{"delta":{"content":"tok"},"pad":"` + benchPad() + `"}]}` + "\n\n"
}

type benchReq struct {
	Messages []benchReqMsg `json:"messages"`
}

type benchReqMsg struct {
	Content string `json:"content"`
}

// benchFake is the scripted gateway. cold/warm are the time to the first
// content delta for an unseen/repeat prompt — the prefix cache the serving
// stacks ship by default, simulated rather than assumed. perToken is the
// decode step, so a request's wall time is first + n*perToken and every number
// the bench reports is checkable against it. hang marks a prompt the server
// takes and never answers, which is what a stack over its context limit does.
type benchFake struct {
	mu                   sync.Mutex
	seen                 map[string]bool
	hang                 map[string]bool
	cold, warm, perToken time.Duration
	hangFor              time.Duration
	maxDeltas            int
	served               int
}

func (f *benchFake) serve(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/v1/models" {
		w.Write([]byte(benchModelsJSON))
		return
	}
	body, _ := io.ReadAll(r.Body)
	req := benchReq{}
	if err := json.Unmarshal([]byte(string(body)), &req); err != nil {
		return
	}
	prompt := ""
	if len(req.Messages) > 0 {
		prompt = req.Messages[0].Content
	}
	hangs, first := false, f.cold
	f.mu.Lock()
	if f.hang[prompt] {
		hangs = true
	}
	if f.seen[prompt] {
		first = f.warm // prefix cache hit: only the tail is re-prefilled
	}
	f.seen[prompt] = true
	f.served++
	f.mu.Unlock()
	if hangs {
		time.Sleep(f.hangFor) // takes the prompt, never answers: the timeout case
		return
	}
	// A role-only chunk first: it is a real server's shape and it must NOT
	// count as a first token, or TTFT here would be a lie on every gateway.
	w.Write([]byte(benchRoleLine()))
	time.Sleep(first)
	for i := 0; i < f.maxDeltas; i++ {
		w.Write([]byte(benchDeltaLine()))
		time.Sleep(f.perToken)
	}
	w.Write([]byte("data: [DONE]\n\n"))
}

func newFake(cold, warm, perToken time.Duration, maxDeltas int) *benchFake {
	return &benchFake{cold: cold, warm: warm, perToken: perToken, maxDeltas: maxDeltas,
		seen: map[string]bool{}, hang: map[string]bool{}}
}

func serveFake(f *benchFake) *httptest.Server {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.serve(w, r)
	}))
	return srv
}

// hostPort splits an httptest URL into the host:port pair benchTarget wants.
func hostPort(srv *httptest.Server) (string, uint16) {
	u := strings.TrimPrefix(srv.URL, "http://")
	i := strings.LastIndex(u, ":")
	n, _ := strconv.Atoi(u[i+1:])
	return u[:i], uint16(n)
}

// pointAt is the target seam: "this project's gateway listens on this httptest
// server", with no daemon and no container. Tests restore the default with
// t.Cleanup so a stubbed target cannot leak into the next test.
var benchTargetsDefault = benchTargets

func pointAt(srv *httptest.Server) {
	host, port := hostPort(srv)
	benchTargets = func(e *Executor, ctx context.Context, project string) []benchTarget {
		return []benchTarget{benchTarget{host: host, port: port}}
	}
}

func noTargets() {
	benchTargets = func(e *Executor, ctx context.Context, project string) []benchTarget {
		return nil
	}
}

func benchPayload(env map[string]string) transport.TaskPayload {
	return transport.TaskPayload{StackOp: "bench", StackProject: "modstack1g0",
		ContainerEnv: env}
}

// benchDoc is the report the manager parses. Typed rather than a loose map so
// a renamed key in stack_bench.go is a compile error here, not a test that
// quietly passes on an empty document.
type benchDoc struct {
	TTFT     map[string]float64 `json:"ttft_ms"`
	TPS      map[string]float64 `json:"decode_tps"`
	CacheHit float64            `json:"cache_hit_ttft_ms"`
	Model    string             `json:"model"`
	Errors   []string           `json:"errors"`
}

func parseBench(t *testing.T, tail string) benchDoc {
	begin := strings.Index(tail, benchSentinelBegin)
	end := strings.Index(tail, benchSentinelEnd)
	if begin < 0 || end < 0 || end <= begin {
		t.Fatal("bench report missing its sentinels")
	}
	body := strings.TrimSpace(tail[begin+len(benchSentinelBegin) : end])
	doc := benchDoc{}
	if err := json.Unmarshal([]byte(body), &doc); err != nil {
		t.Fatal(err)
	}
	return doc
}

func sameInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := 0; i < len(a); i++ {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func benchErrors(doc benchDoc) string { return strings.Join(doc.Errors, "|") }

func hasErrorContaining(doc benchDoc, needle string) bool {
	for _, e := range doc.Errors {
		if strings.Contains(e, needle) {
			return true
		}
	}
	return false
}

func benchEnv(pairs ...string) map[string]string {
	out := map[string]string{}
	for i := 0; i+1 < len(pairs); i += 2 {
		out[pairs[i]] = pairs[i+1]
	}
	return out
}

// runBench points the bench at one scripted gateway and returns the parsed
// report plus the wall time the sweep took, which is what the concurrency
// assertions need — a rate alone cannot prove requests overlapped.
func runBench(t *testing.T, f *benchFake, env map[string]string) (benchDoc, time.Duration, string) {
	old := benchTargets
	srv := serveFake(f)
	t.Cleanup(func() { benchTargets = old; srv.Close() })
	pointAt(srv)
	started := time.Now()
	r, err := (&Executor{}).benchStackOp(context.Background(), benchPayload(env), started)
	if err != nil {
		t.Fatal(err)
	}
	return parseBench(t, r.LogTail), time.Since(started), r.LogTail
}

func TestBenchConfig_Defaults(t *testing.T) {
	cfg := benchConfig(benchPayload(map[string]string{}))
	if !sameInts(cfg.Sizes, benchDefaultPromptSizes()) {
		t.Fatal("default sizes must be 8k/40k/80k/160k")
	}
	if !sameInts(cfg.Concurrency, benchDefaultConcurrencies()) {
		t.Fatal("default concurrencies must be 1/2/4/8")
	}
	if cfg.MaxTokens != benchMaxTokens || cfg.TimeoutS != benchRequestTimeoutS {
		t.Fatal("defaults for max tokens and per-request timeout must apply")
	}
}

func TestBenchConfig_Overrides(t *testing.T) {
	cfg := benchConfig(benchPayload(benchEnv("BENCH_PROMPT_SIZES", "8000, 40000, junk, -5",
		"BENCH_CONCURRENCIES", "1,4", "BENCH_MAX_PROMPT_TOKENS", "32768")))
	if !sameInts(cfg.Sizes, []int{8000, 40000}) {
		t.Fatal("garbage/non-positive sizes must be dropped, not trusted")
	}
	if !sameInts(cfg.Concurrency, []int{1, 4}) {
		t.Fatal("concurrencies must come from the payload when given")
	}
	if cfg.MaxPromptTokens != 32768 {
		t.Fatal("the stack's context limit must reach the bench")
	}
}

func TestBench_TTFTIsComputed(t *testing.T) {
	// cold=300ms, 4 deltas at 50ms: the first content delta cannot arrive
	// before 300ms, and the role-only chunk that precedes it must not be
	// counted as a first token.
	f := newFake(300*time.Millisecond, 30*time.Millisecond, 50*time.Millisecond, 4)
	doc, _, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000", "BENCH_CONCURRENCIES", "1"))
	got := doc.TTFT["8000"]
	if got <= 0.0 {
		t.Fatal("ttft_ms:8000 missing — a serving gateway must produce a number")
	}
	if got < 300.0 {
		t.Fatal("TTFT under the scripted cold start: the role-only chunk was counted")
	}
	if got > 300.0+4*50.0+500.0 {
		t.Fatal("TTFT wildly over the scripted delay — it timed the whole stream, not the first token")
	}
	if doc.Model != "bench-model" {
		t.Fatal("the bench must report the model the gateway actually served")
	}
}

func TestBench_CacheHitTTFT(t *testing.T) {
	// Same prompt twice, back to back: the second call re-prefills only the
	// tail, so the reported cache-hit TTFT is the warm number, not the cold.
	f := newFake(400*time.Millisecond, 40*time.Millisecond, 20*time.Millisecond, 2)
	doc, _, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000", "BENCH_CONCURRENCIES", "1"))
	if doc.CacheHit <= 0.0 {
		t.Fatal("cache_hit_ttft_ms missing")
	}
	if doc.CacheHit >= doc.TTFT["8000"] {
		t.Fatal("cache-hit TTFT must be the repeat call, which the scripted cache makes cheaper")
	}
	if doc.CacheHit < 40.0 {
		t.Fatal("cache-hit TTFT under the scripted warm start")
	}
}

func TestBench_SkipsSizesOverContextLimit(t *testing.T) {
	f := newFake(10*time.Millisecond, 10*time.Millisecond, 10*time.Millisecond, 1)
	doc, _, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000, 64",
		"BENCH_MAX_PROMPT_TOKENS", "1000"))
	if !hasErrorContaining(doc, "skipped over context limit") {
		t.Fatal("a size over the stack's declared context must be recorded, not attempted")
	}
	if doc.TTFT["8000"] > 0.0 {
		t.Fatal("the over-limit size must not be measured")
	}
	if doc.TTFT["64"] <= 0.0 {
		t.Fatal("the in-limit size must still be measured")
	}
}

func TestBench_TimeoutsLandInErrors(t *testing.T) {
	// The gateway takes the big prompt and never answers. The bench must
	// record it and keep going — a stack that cannot take 160k is a fact.
	f := newFake(10*time.Millisecond, 10*time.Millisecond, 10*time.Millisecond, 1)
	f.hangFor = 3 * time.Second
	f.hang[benchPrompt(8000)] = true
	doc, _, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000, 64",
		"BENCH_CONCURRENCIES", "1", "BENCH_REQUEST_TIMEOUT_S", "1"))
	if !hasErrorContaining(doc, "ttft:8000") {
		t.Fatal("the hung size must appear in errors")
	}
	if !hasErrorContaining(doc, "timeout") {
		t.Fatal("the reason must be the timeout, not a silent gap")
	}
	if doc.TTFT["8000"] > 0.0 {
		t.Fatal("a timed-out size must not carry a number")
	}
	if doc.TTFT["64"] <= 0.0 {
		t.Fatal("one hung size must not sink the rest of the sweep")
	}
}

func TestBench_ConcurrencyRunsInParallel(t *testing.T) {
	// The scripted step is 100ms x 5 deltas + 50ms cold = ~550ms per request.
	// At concurrency 4 a sequential bench would take ~2.2s; the wall time here
	// is the whole point of the assertion — a "throughput" number measured one
	// request at a time is the gateway's single-slot rate wearing a batched
	// stack's label, which is the lie that made the fleet's capacity tables
	// wrong.
	f := newFake(50*time.Millisecond, 50*time.Millisecond, 100*time.Millisecond, 5)
	// The TTFT sweep is switched off (every requested size is over the
	// declared context limit) so the wall time below is the decode round
	// plus the probe and /v1/models discovery that pick the gateway.
	doc, wall, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000",
		"BENCH_MAX_PROMPT_TOKENS", "100", "BENCH_CONCURRENCIES", "4"))
	single := 50*time.Millisecond + 5*100*time.Millisecond
	// The wall bound is a coarse sanity net only: runBench's wall also carries
	// the pre-sweep probe and the model discovery, so it is 3 sequential
	// requests, not 2, before a round stops being plausibly parallel.
	if wall > 3*single {
		t.Fatal("concurrency 4 took longer than 3 sequential requests — the round was not parallel")
	}
	rate := doc.TPS["4"]
	if rate <= 0.0 {
		t.Fatal("decode_tps:4 missing")
	}
	// The arithmetic proof of overlap, which is the assertion that actually
	// matters: the scripted single-slot rate is 5 deltas over one request's
	// wall (~550ms) = ~9.1 tok/s. Four requests run one after another sit
	// AT that rate — a batched stack's label on a single slot — so the 4-way
	// rate must clear it by half again to have had any overlap at all.
	singleRate := 5.0 / (float64(single.Microseconds()) / 1000000.0)
	if rate < 1.5*singleRate {
		t.Fatal("concurrency 4 decode rate at the single-slot rate — the requests did not overlap")
	}
	// 4 requests x 5 deltas over ~one request's wall time: ~36 tok/s scripted.
	if rate < 4.0 {
		t.Fatal("throughput under the scripted parallel round — deltas were dropped")
	}
}

func TestBench_DecodeRateIsComputed(t *testing.T) {
	// concurrency 1 is the floor: 5 deltas over ~550ms ≈ 9 tok/s. A rate that
	// divides by the wrong clock unit lands here an order of magnitude off,
	// which is exactly what a pricing page would then quote.
	f := newFake(50*time.Millisecond, 50*time.Millisecond, 100*time.Millisecond, 5)
	doc, _, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "64", "BENCH_CONCURRENCIES", "1"))
	rate := doc.TPS["1"]
	if rate <= 0.0 {
		t.Fatal("decode_tps:1 missing")
	}
	if rate < 5.0 || rate > 40.0 {
		t.Fatal("decode rate outside the scripted window — the arithmetic is wrong")
	}
}

func TestBench_ReportShape(t *testing.T) {
	f := newFake(20*time.Millisecond, 10*time.Millisecond, 20*time.Millisecond, 2)
	doc, _, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000", "BENCH_CONCURRENCIES", "1,2"))
	if len(doc.TTFT) == 0 || len(doc.TPS) == 0 {
		t.Fatal("a clean sweep must carry both measurement maps")
	}
	if doc.Model == "" {
		t.Fatal("model must be reported")
	}
	if doc.Errors == nil {
		t.Fatal("errors must always be present, empty on a clean sweep")
	}
	if doc.CacheHit <= 0.0 {
		t.Fatal("a repeated prompt must report the cache-hit TTFT")
	}
}

func TestBench_NoGatewayIsNotAFailure(t *testing.T) {
	// A stack with no gateway container here is a fact about the host, not a
	// broken bench: the task completes, the report says why nothing was measured.
	old := benchTargets
	t.Cleanup(func() { benchTargets = old })
	noTargets()
	r, err := (&Executor{}).benchStackOp(context.Background(),
		benchPayload(map[string]string{}), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	doc := parseBench(t, r.LogTail)
	if !hasErrorContaining(doc, "no running gateway container") {
		t.Fatal("the reason must be recorded")
	}
	if len(doc.TTFT) > 0 || len(doc.TPS) > 0 {
		t.Fatal("nothing was measured, so nothing may be reported")
	}
}

func TestBench_NeverLogsPromptOrAuth(t *testing.T) {
	// The report is the only thing that crosses the wire. A prompt body in it
	// would put synthesized filler in the same field a customer's prompt
	// would land in, and an auth header in it would put the stack's key in
	// every log the manager keeps.
	f := newFake(10*time.Millisecond, 10*time.Millisecond, 10*time.Millisecond, 1)
	_, _, reportTail := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000", "BENCH_CONCURRENCIES", "1"))
	secret := benchPrompt(8000)
	if strings.Contains(reportTail, secret) {
		t.Fatal("prompt filler leaked into the report")
	}
	if strings.Contains(strings.ToLower(reportTail), "authorization") ||
		strings.Contains(strings.ToLower(reportTail), "bearer") {
		t.Fatal("an auth header reached the report")
	}
}

func TestBenchConfig_BudgetIsAnOpParam(t *testing.T) {
	// The sweep's total time budget is an op param (the manager asks a cheap
	// question without paying for the whole matrix), and its default is the
	// standing 900s — a stack op must never be able to run for hours.
	cfg := benchConfig(benchPayload(benchEnv("BENCH_TOTAL_BUDGET_S", "7")))
	if cfg.BudgetS != 7 {
		t.Fatal("BENCH_TOTAL_BUDGET_S must set the sweep budget")
	}
	dflt := benchConfig(benchPayload(map[string]string{}))
	if dflt.BudgetS != benchPassBudgetS {
		t.Fatal("the default budget must be benchPassBudgetS")
	}
	neg := benchConfig(benchPayload(benchEnv("BENCH_TOTAL_BUDGET_S", "0")))
	if neg.BudgetS != benchPassBudgetS {
		t.Fatal("a zero budget must fall back to the default, never a zero-length sweep")
	}
}

func TestBench_BudgetCutsTheSweep(t *testing.T) {
	// A tight budget must cut the sweep, not turn every unattempted size into
	// its own timeout error: the manager's admission feed reads missing keys
	// plus ONE cut line, and the task still finishes inside the budget.
	f := newFake(300*time.Millisecond, 10*time.Millisecond, 10*time.Millisecond, 1)
	doc, wall, _ := runBench(t, f, benchEnv("BENCH_PROMPT_SIZES", "8000, 40000, 80000, 160000",
		"BENCH_CONCURRENCIES", "1,2,4,8", "BENCH_TOTAL_BUDGET_S", "1"))
	if !hasErrorContaining(doc, "cut by budget") {
		t.Fatal("a cut sweep must say it was cut")
	}
	ttftErrs := 0
	for _, e := range doc.Errors {
		if strings.Contains(e, "ttft:") || strings.Contains(e, "decode:") {
			ttftErrs++
		}
	}
	if ttftErrs > 1 {
		t.Fatal("the cut must stop the sweep; unattempted sizes get no error line each")
	}
	if len(doc.TPS) != 0 {
		t.Fatal("the TTFT sweep alone exceeds this budget: no decode round may run")
	}
	if wall > 5*time.Second {
		t.Fatal("the budget must bound the whole op, not just the sweep loops")
	}
}

// 1543: a Strata coder-lane stack has no sgl-model-gateway container at all —
// only its own app container, directly publishing its ingress port (the
// shape the local coder-lane stacks on the dev fleet take — no gateway
// hop, the app container owns the ingress port). benchResolveTargets must
// fall back to that container's own published port rather than reporting
// zero
// targets, which was indistinguishable from "stack is down".
func TestBenchResolveTargets_StrataFallback_NoGatewayContainer(t *testing.T) {
	containers := []types.Container{
		{
			ID:     "strata0",
			Image:  "ghcr.io/modtim/strata-cuda:coder-iq1_m-0.1.40",
			Labels: map[string]string{composeProjectLabel: "modstack111g0"},
			Ports:  []types.Port{{PrivatePort: 8080, PublicPort: 18080}},
		},
	}
	targets := benchResolveTargets(containers, "modstack111g0", func(id string) string { return "" })
	if len(targets) == 0 {
		t.Fatal("expected a fallback target for the Strata ingress container, got none")
	}
	for _, tg := range targets {
		if tg.host == "" || tg.port == 0 {
			t.Fatalf("got an unusable target: %+v", tg)
		}
	}
	if got, want := targets[0].chatURL(), "http://127.0.0.1:18080/v1/chat/completions"; got != want {
		t.Fatalf("chatURL = %q, want %q", got, want)
	}
}

// A project with no gateway container AND no container with any published
// port (or more than one — ambiguous) still reports "no target", same as
// before: the fallback must not invent a dial target out of nothing.
func TestBenchResolveTargets_NoPublishedPort_NoTargets(t *testing.T) {
	containers := []types.Container{
		{ID: "a", Image: "some/internal-only:latest",
			Labels: map[string]string{composeProjectLabel: "modstack999g0"}},
	}
	if targets := benchResolveTargets(containers, "modstack999g0", func(string) string { return "" }); len(targets) != 0 {
		t.Fatalf("expected no targets, got %v", targets)
	}
}

// The gr-llm-serving path is unchanged: a gateway-image container still wins
// over the fallback even when another container in the project also
// publishes a port.
func TestBenchResolveTargets_GatewayStillPreferred(t *testing.T) {
	containers := []types.Container{
		{ID: "gw", Image: "lmsysorg/sgl-model-gateway:v0.3.2",
			Labels: map[string]string{composeProjectLabel: "modstack1g0"},
			Ports:  []types.Port{{PrivatePort: 8080, PublicPort: 28080}}},
		{ID: "sidecar", Image: "some/other:latest",
			Labels: map[string]string{composeProjectLabel: "modstack1g0"},
			Ports:  []types.Port{{PrivatePort: 9000, PublicPort: 29000}}},
	}
	targets := benchResolveTargets(containers, "modstack1g0", func(string) string { return "" })
	if len(targets) == 0 {
		t.Fatal("expected a gateway target")
	}
	if targets[0].port != 28080 {
		t.Fatalf("expected the gateway's own port 28080, got %d", targets[0].port)
	}
}

// benchReasoningLine mimics a Strata/llama.cpp reasoning model's SSE delta:
// chain-of-thought tokens stream under reasoning_content, never content, for
// the whole decode budget — the real capture from 1582/1583.
func benchReasoningLine() string {
	return "data: " + `{"choices":[{"delta":{"reasoning_content":"tok"},"pad":"` + benchPad() + `"}]}` + "\n\n"
}

func TestBenchHTTPDo_ReasoningContentCountsAsDeltas(t *testing.T) {
	// A fake SSE stream whose deltas are 100% reasoning_content (content is
	// never set) must still produce a non-zero Deltas count and a real TTFT —
	// the whole point of 1582/1583: reasoning tokens are real decode work.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(benchRoleLine()))
		for i := 0; i < 4; i++ {
			w.Write([]byte(benchReasoningLine()))
			w.(http.Flusher).Flush()
		}
		w.Write([]byte("data: [DONE]\n\n"))
	}))
	defer srv.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	got, err := benchHTTPDo(ctx, srv.URL, `{}`)
	if err != nil {
		t.Fatalf("benchHTTPDo: %v", err)
	}
	if got.Deltas == 0 {
		t.Fatal("reasoning_content-only deltas were not counted — \"no deltas\" regression")
	}
	if got.TTFTMS < 0 {
		t.Fatal("TTFT must be set by the first reasoning_content delta, not left at -1")
	}
}
