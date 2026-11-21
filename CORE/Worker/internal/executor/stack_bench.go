// 27.6.s1 — stack capacity bench (launch-completion-megaplan 27.6).
//
// A stack's manager knows what it DEPLOYED, never what it SERVES. The gateway
// scrape (group R 27.5c) answers "is it up and how is it loaded"; nothing here
// answers "how fast can it answer, and how big a prompt can it take" — which is
// exactly the number an operator needs before choosing a stack for a node, and
// the number a plan/pricing page needs to stop guessing. Measured on the dev
// fleet, the only capacity figure anyone had was the model card's marketing
// rate; the real served rate on the Dell/GX10 boxes differed by more than 2x
// once the gateway and batching layer was in front.
//
// So the worker runs the measurement itself, against the stack's own gateway
// port, dialled exactly the way 27.5c already dials it (the container's bridge
// IP first, the published port second):
//
//   * TTFT at 8k/40k/80k/160k-token prompts — prefill cost is what makes a long
//     context feel slow, and it is not visible anywhere else.
//   * decode tok/s at concurrency 1/2/4/8 — the throughput the gateway actually
//     buys from batching, which a single-request rate cannot show.
//   * cache-hit TTFT — the same prompt twice, second call: the prefix cache the
//     serving stacks ship by default is worth real money on repeated prompts,
//     and nothing else here proves it is ON.
//
// The prompt text is synthesized (never a customer document), so the bench
// cannot leak or read anything it was not given. Nothing here logs a prompt
// body or an auth header: the only strings that leave are numbers, the model
// id, and short error labels.

package executor

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// benchSentinel brackets the JSON the manager parses out of the task's log
// tail — same discipline as formatStackHostPortSentinel in stackhostports.go:
// the stack-op Result has no structured field, so the report rides the tail.
const benchSentinelBegin = "=====MOD_STACK_BENCH_BEGIN====="
const benchSentinelEnd = "=====MOD_STACK_BENCH_END====="

// Defaults. Every one is overridable from the task payload (see benchConfig)
// so the manager can ask a cheap question (one size) without paying for the
// whole sweep, and so a stack with a small context can be benched at all.
const (
	benchRequestTimeoutS = 120     // per request; a wedged server times out, it never hangs the task
	benchPassBudgetS     = 900     // the whole sweep
	benchMaxTokens       = 64      // decode tokens per request: enough to time, cheap to serve
	benchMaxDeltas       = 1 << 12 // SSE chunks read per request before the stream is cut
)

func benchDefaultPromptSizes() []int   { return []int{8000, 40000, 80000, 160000} }
func benchDefaultConcurrencies() []int { return []int{1, 2, 4, 8} }

// BenchConfig is one bench sweep's knobs, resolved from the task payload.
type BenchConfig struct {
	Sizes           []int    // prompt sizes in (approx) tokens
	Concurrency     []int    // concurrency levels to sweep
	MaxTokens       int      // completion tokens per request
	TimeoutS        int      // per-request timeout, seconds
	MaxPromptTokens int      // the stack's context limit, 0 = unknown
	Models          []string // model ids to try; empty means "discover"
	BudgetS         int      // total wall-clock budget for the whole sweep, 0 = default
}

// benchParseInt reads a payload number that arrived as a string or a number
// (JSON has no int/float distinction and the API has stamped both shapes).
// 0 means absent, -1 means unparsable — callers treat both as "use default".
func benchParseInt(s string) int {
	v := strings.TrimSpace(s)
	if v == "" {
		return 0
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return -1
	}
	return n
}

// benchParseList reads a comma/space separated list of positive ints ("8000,
// 40000"). Anything unparsable or non-positive is dropped, so a half-configured
// sweep still measures something rather than dividing by a zero.
func benchParseList(s string) []int {
	out := []int{}
	for _, tok := range strings.FieldsFunc(s, func(c rune) bool {
		return c == ',' || c == ' ' || c == '\t' || c == '\n'
	}) {
		n, err := strconv.Atoi(strings.TrimSpace(tok))
		if err == nil && n > 0 {
			out = append(out, n)
		}
	}
	return out
}

// benchParseStrings is the same for a comma separated name list.
func benchParseStrings(s string) []string {
	out := []string{}
	for _, tok := range strings.FieldsFunc(s, func(c rune) bool { return c == ',' }) {
		if v := strings.TrimSpace(tok); v != "" {
			out = append(out, v)
		}
	}
	return out
}

// benchConfig resolves the sweep's knobs. The API stamps its platform-settings
// registry into the task's container_env, which is how every worker tunable
// reaches the worker — no new env vars are invented here.
func benchConfig(p transport.TaskPayload) BenchConfig {
	cfg := BenchConfig{
		Sizes:           benchParseList(p.ContainerEnv["BENCH_PROMPT_SIZES"]),
		Concurrency:     benchParseList(p.ContainerEnv["BENCH_CONCURRENCIES"]),
		MaxTokens:       benchParseInt(p.ContainerEnv["BENCH_MAX_TOKENS"]),
		TimeoutS:        benchParseInt(p.ContainerEnv["BENCH_REQUEST_TIMEOUT_S"]),
		MaxPromptTokens: benchParseInt(p.ContainerEnv["BENCH_MAX_PROMPT_TOKENS"]),
		Models:          benchParseStrings(p.ContainerEnv["BENCH_MODELS"]),
		BudgetS:         benchParseInt(p.ContainerEnv["BENCH_TOTAL_BUDGET_S"]),
	}
	if len(cfg.Sizes) == 0 {
		cfg.Sizes = benchDefaultPromptSizes()
	}
	if len(cfg.Concurrency) == 0 {
		cfg.Concurrency = benchDefaultConcurrencies()
	}
	if cfg.MaxTokens <= 0 {
		cfg.MaxTokens = benchMaxTokens
	}
	if cfg.TimeoutS <= 0 {
		cfg.TimeoutS = benchRequestTimeoutS
	}
	if cfg.BudgetS <= 0 {
		cfg.BudgetS = benchPassBudgetS
	}
	return cfg
}

// benchTarget is one host:port pair the OpenAI-compatible surface might live on.
// It is 27.5c's gatewayCandidate shape with the chat endpoint added: the SGLang
// model gateway serves /v1/chat/completions on the SAME port it answers /workers
// on, so "which port do I dial" has one answer in both files.
type benchTarget struct {
	host string
	port uint16
}

func (t benchTarget) chatURL() string {
	return "http://" + net.JoinHostPort(t.host, strconv.Itoa(int(t.port))) + "/v1/chat/completions"
}

func (t benchTarget) modelsURL() string {
	return "http://" + net.JoinHostPort(t.host, strconv.Itoa(int(t.port))) + "/v1/models"
}

// benchPrompt synthesizes a prompt of ~nTokens tokens. chars/4 is the standing
// approximation for a BPE vocab (the same one the API's context estimators use);
// the bench never needs a real tokenizer, it needs a size the server will
// actually prefill. The text is fixed filler — no customer document is read, and
// nothing here can be a prompt the tenant cares about.
func benchPrompt(nTokens int) string {
	const filler = "the stack serves tokens at a measured rate and nothing in this sentence is customer text "
	want := nTokens * 4
	return strings.Repeat(filler, want/len(filler)+1)[:want]
}

// benchStreamResult is one streamed chat completion's timing. Deltas counts the
// content chunks the server sent, which for these servers is one per decoded
// token — the only token count a streaming response makes observable without
// asking for a usage block the OpenAI contract does not require mid-stream.
type benchStreamResult struct {
	TTFTMS  float64 // -1 until the first content delta arrives
	Deltas  int
	Elapsed time.Duration
	Err     string // short label ("timeout", "http 500", "no deltas") — never a body
}

// benchChunk is the slice of one SSE chat delta this bench reads.
type benchChunk struct {
	Choices []benchChoiceChunk `json:"choices"`
}

type benchChoiceChunk struct {
	Delta benchDeltaChunk `json:"delta"`
}

type benchDeltaChunk struct {
	Content string `json:"content"`
	// ReasoningContent carries chain-of-thought tokens from reasoning models
	// (Strata/llama.cpp qwen3.8 and similar). Those models stream their
	// chain-of-thought here and only emit Content once reasoning finishes,
	// which for the bench's small max_tokens budget may never happen. The
	// tokens are still real GPU decode work the caller pays wall-clock for,
	// so they count the same as Content toward TTFT/decode-rate below.
	ReasoningContent string `json:"reasoning_content"`
}

// benchHTTPDo is the HTTP seam (same shape as gatewayscrape.go's gatewayHTTPDo)
// so a test can stand a fake streaming server without a daemon. It POSTs a chat
// body, reads the SSE stream, and returns time-to-first-content-delta plus the
// delta count. The deadline is carried by ctx, so a wedged server is a timeout,
// never a hang.
var benchHTTPDo = func(ctx context.Context, url, body string) (benchStreamResult, error) {
	client := &http.Client{}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader([]byte(body)))
	if err != nil {
		return benchStreamResult{TTFTMS: -1}, err
	}
	req.Header.Set("Content-Type", "application/json")
	started := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		return benchStreamResult{TTFTMS: -1}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return benchStreamResult{TTFTMS: -1}, fmt.Errorf("http %d", resp.StatusCode)
	}
	out := benchStreamResult{TTFTMS: -1, Elapsed: time.Since(started)}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		line := sc.Text()
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(line[5:])
		if payload == "[DONE]" {
			break
		}
		chunk := benchChunk{}
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			continue
		}
		for _, ch := range chunk.Choices {
			// A delta counts if EITHER field is non-empty: gr-llm-serving gateway
			// stacks only ever set Content (unchanged behavior), while Strata
			// reasoning models stream real decoded tokens under
			// ReasoningContent instead. Keep them as two distinct fields; just
			// count both toward the same TTFT/Deltas numbers.
			if ch.Delta.Content == "" && ch.Delta.ReasoningContent == "" {
				continue
			}
			if out.TTFTMS < 0 {
				out.TTFTMS = float64(time.Since(started).Microseconds()) / 1000.0
			}
			out.Deltas++
		}
		if out.Deltas >= benchMaxDeltas {
			break
		}
	}
	out.Elapsed = time.Since(started)
	if out.TTFTMS < 0 && ctx.Err() != nil {
		// The deadline expired before a single content delta arrived. Saying so is
		// what turns "the stack cannot take this prompt" into a recorded fact
		// instead of a missing key nobody can tell a timeout from.
		return out, fmt.Errorf("timeout")
	}
	return out, nil
}

// benchChatBody is the request wire for one measurement. `stream` is what makes
// TTFT observable at all — a non-streaming server hands back the whole answer at
// once and every TTFT here would be the total. max_tokens is pinned small so a
// decode sweep costs seconds, not hours, on a 160k-token prompt.
func benchChatBody(model, prompt string, maxTokens int) string {
	msg := map[string]string{"role": "user", "content": prompt}
	body := map[string]interface{}{
		"model":       model,
		"messages":    []map[string]string{msg},
		"stream":      true,
		"max_tokens":  maxTokens,
		"temperature": 0.0,
	}
	s, err := json.Marshal(body)
	if err != nil {
		return ""
	}
	return string(s)
}

// benchOneRequest issues one chat completion and times it. `ctx` carries the
// per-request deadline; a deadline hit is reported as an error label, it never
// propagates as a task failure (one size being too big for a stack is a fact
// about the stack, not a broken bench).
func benchOneRequest(ctx context.Context, timeoutS int, url, model, prompt string, maxTokens int) benchStreamResult {
	body := benchChatBody(model, prompt, maxTokens)
	if body == "" {
		return benchStreamResult{Err: "encode"}
	}
	reqCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutS)*time.Second)
	defer cancel()
	got, err := benchHTTPDo(reqCtx, url, body)
	if err != nil {
		// The deadline is what makes "this stack cannot take this prompt" a
		// recorded fact rather than a missing key, so it is checked before the
		// transport's own words: a socket error on a deadline hit does not always
		// say "timeout".
		label := "timeout"
		raw := fmt.Sprintf("%v", err)
		if reqCtx.Err() == nil && !strings.Contains(strings.ToLower(raw), "timeout") {
			label = raw
		}
		return benchStreamResult{TTFTMS: -1, Err: label}
	}
	if got.Deltas == 0 {
		got.Err = "no deltas"
	}
	return got
}

// benchParallel runs n identical chat completions at once and returns the
// aggregate: total decoded deltas over wall time. This is the only honest way
// to read a gateway's batching — sequential requests measure one slot, the
// gateway's whole reason for existing is that concurrent slots share prefill
// and interleave decode.
func benchParallel(ctx context.Context, timeoutS int, url, model, prompt string, maxTokens, n int) (deltas int, elapsed time.Duration, errs []string) {
	got := make([]benchStreamResult, n)
	var (
		wg sync.WaitGroup
	)
	started := time.Now()
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			got[i] = benchOneRequest(ctx, timeoutS, url, model, prompt, maxTokens)
		}(i)
	}
	wg.Wait()
	elapsed = time.Since(started)
	for _, g := range got {
		if g.Err != "" {
			errs = append(errs, g.Err)
		}
		deltas += g.Deltas
	}
	return deltas, elapsed, errs
}

// benchRate is the decode throughput of one parallel round: deltas over wall
// time. A round that produced nothing has no rate — reporting 0.0 would read as
// "this stack is slow", which is a lie; the caller records the error instead and
// leaves the key out, the same "no measurement is not zero" discipline stackobserve.go
// applies to VramGB.
func benchRate(deltas int, elapsed time.Duration) float64 {
	ms := float64(elapsed.Microseconds()) / 1000.0
	if deltas <= 0 || ms <= 0.0 {
		return -1.0
	}
	return float64(deltas) / (ms / 1000.0)
}

// benchStackTargets resolves the host:port pairs to dial for a project's
// gateway, by way of 27.5c's resolver: the container's bridge IP first, the
// published port second. One definition of "where the gateway listens", two
// consumers of it — a stack whose gateway moved ports cannot be scraped by one
// file and missed by the other.
// benchTargets is the target-resolution seam (same reason execCommandContext
// and gatewayHTTPDo are func vars): a test stands a fake gateway on an httptest
// port and says "that is where this stack's gateway listens", with no daemon.
var benchStackTargetsDefault = func(e *Executor, ctx context.Context, project string) []benchTarget {
	return e.benchStackTargets(ctx, project)
}

var benchTargets = benchStackTargetsDefault

// benchStackTargets resolves the host:port pairs to dial for a project's
// gateway, by way of 27.5c's resolver: the container's bridge IP first, the
// published port second. One definition of "where the gateway listens", two
// consumers of it — a stack whose gateway moved ports cannot be scraped by one
// file and missed by the other.
func (e *Executor) benchStackTargets(ctx context.Context, project string) []benchTarget {
	if e.docker == nil {
		return nil // no daemon wired (a hand-built test Executor): nothing to dial
	}
	listCtx, cancel := context.WithTimeout(ctx, stackListBudget)
	defer cancel()
	containers, err := e.docker.ContainerList(listCtx, container.ListOptions{
		Filters: filters.NewArgs(
			filters.Arg("label", composeProjectLabel),
			filters.Arg("status", "running"),
		),
	})
	if err != nil {
		return nil
	}
	return benchResolveTargets(containers, project, func(id string) string {
		return e.gatewayContainerIP(ctx, id)
	})
}

// benchResolveTargets is the pure target-selection half of benchStackTargets,
// pulled out so it can be exercised with a stub container list and no docker
// daemon: give it exactly what ContainerList would have returned and get back
// exactly what the real resolver would dial.
//
// gr-llm-serving stacks (fronted by the SGLang model gateway) resolve exactly
// as before: the dedicated gateway container, dialed the 27.5c way. Bug 1543 —
// a Strata (or any other) coder-lane stack has no such container at all, only
// its own app container(s) directly, so that path always returned zero
// targets, indistinguishable from "stack is down". The fallback (no gateway
// image match): MOD's own compose layer (API persistent_stacks/_shared.py's
// `_apply_egress_networks` / `_inject_ingress_forwarder`) only ever publishes
// a host port for the stack's single declared ingress service — every other
// service in the project stays expose-only, internal — so "the one running
// container in this project with a published port" IS that ingress target,
// with no extra metadata needed from the manager. A project with zero or more
// than one such container is ambiguous (or genuinely has nothing to dial) and
// reports the same "no running gateway container" miss as before.
func benchResolveTargets(containers []types.Container, project string, containerIP func(id string) string) []benchTarget {
	var gateway types.Container
	var published []types.Container
	for _, c := range containers {
		if strings.TrimSpace(c.Labels[composeProjectLabel]) != project {
			continue
		}
		if isGatewayImage(c.Image) && gateway.ID == "" {
			gateway = c
		}
		for _, p := range c.Ports {
			if p.PublicPort > 0 {
				published = append(published, c)
				break
			}
		}
	}
	if gateway.ID != "" {
		out := []benchTarget{}
		for _, cand := range gatewayCandidates(gateway, containerIP(gateway.ID)) {
			if cand.workersPort == 0 {
				continue
			}
			out = append(out, benchTarget{host: cand.host, port: cand.workersPort})
		}
		return out
	}
	if len(published) != 1 {
		return nil
	}
	return benchIngressCandidates(published[0], containerIP(published[0].ID))
}

// benchDiscoverModel asks the gateway which model it is actually serving. The
// bench cannot time "the stack" without naming what answered, and a stack can
// be re-pointed at a different checkpoint without anyone redeploying it — the
// manager's own model field would then be stale, and every capacity number
// filed under it would be wrong. Empty means "the gateway has no /v1/models".
func benchDiscoverModel(ctx context.Context, url string) string {
	body, err := gatewayHTTPDo(ctx, url)
	if err != nil {
		return ""
	}
	doc := benchModelList{}
	if err := json.Unmarshal([]byte(body), &doc); err != nil {
		return ""
	}
	for _, m := range doc.Data {
		if id := strings.TrimSpace(m.ID); id != "" {
			return id
		}
	}
	return ""
}

type benchModelList struct {
	Data []benchModelEntry `json:"data"`
}

type benchModelEntry struct {
	ID     string `json:"id"`
	Object string `json:"object"`
}

// benchStackOp is the "bench" stack op. It never fails the task for a stack
// that answers badly — a stack that cannot take a 160k prompt is a fact about
// the stack, and the manager wants that fact, not a red task row it can no
// longer read. Only "there is no gateway here to dial" is a real failure, and
// even that is reported as an empty sweep with the reason in errors so the
// reconciler's lifecycle step still sees a completed task.
func (e *Executor) benchStackOp(ctx context.Context, p transport.TaskPayload, started time.Time) (*Result, error) {
	cfg := benchConfig(p)
	project := strings.TrimSpace(p.StackProject)
	passCtx, cancel := context.WithTimeout(ctx, time.Duration(cfg.BudgetS)*time.Second)
	defer cancel()

	ttft := map[string]float64{}
	tps := map[string]float64{}
	errs := []string{}
	model := ""

	targets := []benchTarget{}
	if project == "" {
		errs = append(errs, "missing stack_project")
	} else {
		targets = benchTargets(e, passCtx, project)
		if len(targets) == 0 {
			errs = append(errs, "no running gateway container for "+project+" on this host")
		}
	}

	// The first target that answers at all is the one measured; the rest are
	// candidates for the same gateway, not extra servers to average over.
	var chatURL string
	for _, t := range targets {
		probe := benchOneRequest(passCtx, cfg.TimeoutS, t.chatURL(), benchDefaultModel, benchPrompt(8), 1)
		if probe.Err == "" || probe.Deltas > 0 {
			chatURL = t.chatURL()
			break
		}
	}
	if chatURL == "" && len(targets) > 0 {
		chatURL = targets[0].chatURL() // one candidate, nothing better to pick
	}

	if chatURL != "" {
		if got := benchDiscoverModel(passCtx, targets[0].modelsURL()); got != "" {
			model = got
		}
		if len(cfg.Models) > 0 {
			model = cfg.Models[0] // the manager named it; the gateway's own list is a fallback
		}
		if model == "" {
			model = benchDefaultModel
			errs = append(errs, "model not discoverable, benched as "+benchDefaultModel)
		}
	}

	if chatURL == "" {
		return e.benchReport(started, model, ttft, tps, -1.0, errs), nil
	}

	// --- TTFT sweep. Sequential on purpose: concurrent big prompts would
	// measure the gateway's batching while pretending to measure prefill.
	cacheHit := -1.0
	cut := false
	for _, size := range cfg.Sizes {
		if passCtx.Err() != nil {
			// The sweep ran out of budget: say so ONCE and stop, rather than
			// spending every remaining size on its own timeout error.
			cut = true
			break
		}
		if cfg.MaxPromptTokens > 0 && size > cfg.MaxPromptTokens {
			errs = append(errs, fmt.Sprintf("ttft:%d skipped over context limit %d", size, cfg.MaxPromptTokens))
			continue
		}
		key := strconv.Itoa(size)
		prompt := benchPrompt(size)
		got := benchOneRequest(passCtx, cfg.TimeoutS, chatURL, model, prompt, cfg.MaxTokens)
		if got.TTFTMS < 0 {
			errs = append(errs, fmt.Sprintf("ttft:%d %s", size, orLabel(got.Err, "no first token")))
			continue
		}
		ttft[key] = got.TTFTMS

		// Cache-hit: the SAME prompt again, immediately. A serving stack with its
		// prefix cache on re-prefills only the tail, so the second TTFT is the
		// number that matters for a RAG-style node that replays a document.
		again := benchOneRequest(passCtx, cfg.TimeoutS, chatURL, model, prompt, cfg.MaxTokens)
		if again.TTFTMS >= 0 && cacheHit < 0 {
			cacheHit = again.TTFTMS
		}
	}

	// --- decode sweep. Same short prompt at every concurrency: what is being
	// measured is slot throughput, not prefill, and a big prompt at 8-way
	// concurrency would spend the whole budget inside the model's own warmup.
	decodePrompt := benchPrompt(64)
	for _, conc := range cfg.Concurrency {
		if cut || passCtx.Err() != nil {
			// Same budget discipline as the TTFT sweep: a cut sweep reports what it
			// measured plus one "cut" line, never a wall of per-concurrency timeouts.
			cut = true
			break
		}
		if conc <= 0 {
			continue
		}
		deltas, elapsed, roundErrs := benchParallel(passCtx, cfg.TimeoutS,
			chatURL, model, decodePrompt, cfg.MaxTokens, conc)
		rate := benchRate(deltas, elapsed)
		if rate < 0.0 {
			errs = append(errs, fmt.Sprintf("decode:%d %s", conc, orLabel(strings.Join(roundErrs, "; "), "no deltas")))
			continue
		}
		tps[strconv.Itoa(conc)] = rate
	}
	if cut {
		// One line, not one per unattempted size: the manager's admission feed
		// reads "these keys are missing because the sweep was cut", and a stack
		// that is merely slow under a tight budget still reports real numbers.
		errs = append(errs, "sweep cut by budget")
	}

	return e.benchReport(started, model, ttft, tps, cacheHit, errs), nil
}

func orLabel(s, fallback string) string {
	v := strings.TrimSpace(s)
	if v == "" {
		return fallback
	}
	return v
}

const benchDefaultModel = "default"

// benchReport renders the sweep into the task's log tail, bracketed by the
// sentinel the manager parses. The payload is exactly:
//
//	{"ttft_ms":{"8000":123.4,...},"decode_tps":{"1":31.2,...},
//	 "cache_hit_ttft_ms":45.6,"model":"...","errors":[...]}
//
// Keys are omitted, never zero-filled: a size that timed out has no number, and
// a 0.0 in a capacity table reads as "instant", which is the opposite of the
// truth. Errors carry short labels only — a size, a concurrency, a transport
// reason. Never a prompt body, never a header.
func (e *Executor) benchReport(started time.Time, model string, ttft, tps map[string]float64,
	cacheHit float64, errs []string) *Result {
	doc := map[string]interface{}{}
	if len(ttft) > 0 {
		doc["ttft_ms"] = ttft
	}
	if len(tps) > 0 {
		doc["decode_tps"] = tps
	}
	if cacheHit >= 0.0 {
		doc["cache_hit_ttft_ms"] = cacheHit
	}
	if model != "" {
		doc["model"] = model
	}
	doc["errors"] = errs
	payload, err := json.Marshal(doc)
	if err != nil {
		payload = []byte("{}")
	}
	return &Result{
		LogTail:  benchSentinelBegin + "\n" + string(payload) + "\n" + benchSentinelEnd,
		Duration: time.Since(started),
	}
}
