package executor

// B86i unit tests for the co-located-API extra_hosts injection. The resolver is
// a parameter, so the decision is exercised without touching the host's DNS.

import (
	"bytes"
	"context"
	"log/slog"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"

	"gopkg.in/yaml.v3"
)

const base = `services:
  app-1:
    image: x
  api-1:
    image: y
`

func loopback(_ string) []string { return []string{"127.0.0.1"} }
func routable(_ string) []string { return []string{"203.0.113.9"} }
func none(_ string) []string     { return []string{} }

// lookupMap builds a per-host resolver for tests that need to distinguish
// multiple candidate hosts (e.g. the apiURL host vs an env-derived host).
func lookupMap(m map[string][]string) func(string) []string {
	return func(h string) []string { return m[h] }
}

// servicesOf returns the sorted service names an injection list touched.
func servicesOf(injections []apiHostInjection) []string {
	var out []string
	for _, inj := range injections {
		out = append(out, inj.Services...)
	}
	return out
}

func hostsOf(y string) map[string][]string {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(y), &root); err != nil {
		return nil
	}
	out := map[string][]string{}
	svcs := mapGet(root.Content[0], "services")
	if svcs == nil {
		return out
	}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		eh := mapGet(svcs.Content[i+1], "extra_hosts")
		var vals []string
		if eh != nil && eh.Kind == yaml.SequenceNode {
			for _, it := range eh.Content {
				vals = append(vals, it.Value)
			}
		}
		out[svcs.Content[i].Value] = vals
	}
	return out
}

// loopback → injected on every service
func TestLoopbackInjectsEveryService(t *testing.T) {
	out, injections, err := injectAPIHostYAML(base, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(servicesOf(injections)) != 2 {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	for _, svc := range []string{"app-1", "api-1"} {
		if len(h[svc]) != 1 || h[svc][0] != "dev-api.example.org:host-gateway" {
			t.Fatalf("%s extra_hosts=%v", svc, h[svc])
		}
	}
}

// non-loopback → untouched
func TestRoutableHostUntouched(t *testing.T) {
	out, injections, err := injectAPIHostYAML(base, "api.example.org", routable)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != base {
		t.Fatalf("routable host must be a no-op: injections=%v", injections)
	}
}

// empty hostname → no-op
func TestEmptyHostNoop(t *testing.T) {
	out, injections, err := injectAPIHostYAML(base, "  ", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != base {
		t.Fatalf("empty host must be a no-op: injections=%v", injections)
	}
}

// unresolvable host → untouched (nothing to prove)
func TestUnresolvableUntouched(t *testing.T) {
	out, injections, _ := injectAPIHostYAML(base, "dev-api.example.org", none)
	if len(injections) != 0 || out != base {
		t.Fatalf("unresolvable host must be a no-op: injections=%v", injections)
	}
}

// existing user entry for that hostname → never overridden
func TestUserEntryPreserved(t *testing.T) {
	userSpec := `services:
  app-1:
    image: x
    extra_hosts:
      - "dev-api.example.org:10.0.0.5"
`
	out, injections, err := injectAPIHostYAML(userSpec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != userSpec {
		t.Fatalf("user entry must survive untouched: injections=%v", injections)
	}
}

// advisory allowlist gets the API host appended when missing
func TestAllowlistAppend(t *testing.T) {
	specMap := `services:
  app-1:
    image: x
    environment:
      MOD_ALLOWED_EGRESS_HOSTS: "peer-tail.example.net"
  seq-1:
    image: y
    environment:
      - "MOD_ALLOWED_EGRESS_HOSTS=peer"
      - "OTHER=1"
  bare-1:
    image: z
`
	out, injections, err := injectAPIHostYAML(specMap, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(servicesOf(injections)) != 3 {
		t.Fatalf("injections=%v", injections)
	}
	if !strings.Contains(out, "peer-tail.example.net dev-api.example.org") {
		t.Fatalf("mapping env must gain the host: %s", out)
	}
	if !strings.Contains(out, "MOD_ALLOWED_EGRESS_HOSTS=peer dev-api.example.org") {
		t.Fatalf("sequence env must gain the host: %s", out)
	}
	// A service with no advisory env gets no env injected (absence = no list).
	if strings.Contains(out, "bare-1:\n    image: z\n    environment:") {
		t.Fatalf("must not invent an allowlist for bare-1: %s", out)
	}
}

// B86i-d — a comma-joined MOD_ALLOWED_EGRESS_HOSTS (the ONLY shape the API
// ever renders — persistent_stacks/_shared.py:2893 `",".join(hosts)`, same
// convention as netpol.go:342) must be recognised as already containing the
// host, and a genuinely-missing host must be appended with a comma, not a
// space — stack 114's `api` service shape exactly.
func TestAllowlistAppendCommaSeparated(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      MOD_ALLOWED_EGRESS_HOSTS: api.sandbox.ebay.com,upload.ebay.com,dev-api.example.org
`
	out, injections, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(servicesOf(injections)) != 1 {
		t.Fatalf("injections=%v", injections)
	}
	// The host is already present (comma-separated) — the value must survive
	// byte-for-byte, with no re-appended duplicate.
	if !strings.Contains(out, "MOD_ALLOWED_EGRESS_HOSTS: api.sandbox.ebay.com,upload.ebay.com,dev-api.example.org\n") {
		t.Fatalf("already-comma-joined host must not be duplicated: %s", out)
	}
	if strings.Contains(out, "dev-api.example.org dev-api.example.org") ||
		strings.Contains(out, "dev-api.example.org,dev-api.example.org") {
		t.Fatalf("host must not be duplicated: %s", out)
	}

	// A genuinely-new host must be appended with a COMMA, matching the only
	// separator this env's value is ever rendered with.
	out2, _, err := injectAPIHostYAML(spec, "peer-api.example.net", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if !strings.Contains(out2, "dev-api.example.org,peer-api.example.net") {
		t.Fatalf("new host must be comma-appended: %s", out2)
	}
}

// env-derived URL host loopback while the worker's own apiURL host is
// non-loopback (the B86i live finding: apiURL host "api" is docker DNS
// routable, but the app's own CONSIGN_MOD_API_URL host leaks to loopback).
func TestEnvURLHostLoopbackInjected(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      CONSIGN_MOD_API_URL: "https://dev-api.example.org"
`
	lookup := lookupMap(map[string][]string{
		"api":                 {"198.51.100.17"},
		"dev-api.example.org": {"127.0.0.1"},
	})
	out, injections, err := injectAPIHostYAML(spec, "api", lookup)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "dev-api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	if len(h["app-1"]) != 1 || h["app-1"][0] != "dev-api.example.org:host-gateway" {
		t.Fatalf("app-1 extra_hosts=%v", h["app-1"])
	}
}

// env-derived URL host that resolves non-loopback → untouched
func TestEnvURLHostRoutableUntouched(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      CONSIGN_MOD_API_URL: "https://peer-api.example.net"
`
	lookup := lookupMap(map[string][]string{
		"api":                  {"198.51.100.17"},
		"peer-api.example.net": {"203.0.113.5"},
	})
	out, injections, err := injectAPIHostYAML(spec, "api", lookup)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != spec {
		t.Fatalf("routable env host must be a no-op: injections=%v", injections)
	}
}

// list-shape `environment` with a loopback URL value
func TestEnvURLListShapeLoopbackInjected(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      - "CONSIGN_MOD_API_URL=https://dev-api.example.org/v1"
      - "OTHER=1"
`
	out, injections, err := injectAPIHostYAML(spec, "", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "dev-api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	if len(h["app-1"]) != 1 || h["app-1"][0] != "dev-api.example.org:host-gateway" {
		t.Fatalf("app-1 extra_hosts=%v", h["app-1"])
	}
}

// multiple distinct loopback hosts across services → both injected, each
// service only gets the host(s) that apply to it (user entries respected).
func TestMultipleLoopbackHostsInjected(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      CONSIGN_MOD_API_URL: "https://dev-api.example.org"
  app-2:
    image: y
    environment:
      OTHER_API_URL: "https://peer-api.example.net"
`
	lookup := lookupMap(map[string][]string{
		"dev-api.example.org":  {"127.0.0.1"},
		"peer-api.example.net": {"127.0.0.1"},
	})
	out, injections, err := injectAPIHostYAML(spec, "", lookup)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 2 {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	// Every service gets BOTH co-located hosts injected (the app in either
	// service could reach either platform URL).
	for _, svc := range []string{"app-1", "app-2"} {
		if len(h[svc]) != 2 {
			t.Fatalf("%s extra_hosts=%v", svc, h[svc])
		}
	}
}

// a user extra_hosts entry for an env-derived host is preserved exactly like
// one for the apiURL host.
func TestEnvURLHostUserEntryPreserved(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      CONSIGN_MOD_API_URL: "https://dev-api.example.org"
    extra_hosts:
      - "dev-api.example.org:10.0.0.5"
`
	out, injections, err := injectAPIHostYAML(spec, "", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != spec {
		t.Fatalf("user entry must survive untouched: injections=%v", injections)
	}
}

// IP-literal and "localhost" candidate hosts are never injected, even when
// the resolver would call them loopback.
func TestIPLiteralAndLocalhostSkipped(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    environment:
      A_URL: "http://127.0.0.1:9000"
      B_URL: "http://localhost:9000"
`
	out, injections, err := injectAPIHostYAML(spec, "", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != spec {
		t.Fatalf("IP literal/localhost candidates must be a no-op: injections=%v", injections)
	}
}

// B86i-c — stack 114 (modstack114g0-api-1): a service sharing config via a
// YAML merge key (`<<: *anchor`), the shape a generated/templated compose
// uses to avoid repeating a platform env block across services, hid the
// platform URL from candidate detection entirely (yaml.v3 never expands merge
// keys when unmarshaling into a raw *yaml.Node — only when decoding into a
// struct/map). Redeploy left modstack114g0-api-1 with ExtraHosts=[] and
// no injection log, even though CONSIGN_MOD_API_URL resolves to loopback.
func TestMergedServiceEnvURLHostLoopbackInjected(t *testing.T) {
	spec := `x-app-env: &app-env
  environment:
    CONSIGN_MOD_API_URL: "https://dev-api.example.org"
services:
  api-1:
    image: x
    <<: *app-env
`
	out, injections, err := injectAPIHostYAML(spec, "", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "dev-api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	if len(h["api-1"]) != 1 || h["api-1"][0] != "dev-api.example.org:host-gateway" {
		t.Fatalf("api-1 extra_hosts=%v", h["api-1"])
	}
}

// a merge-provided environment combined with an explicit override on the
// service: the explicit value wins (standard YAML merge-key semantics).
func TestMergedEnvExplicitOverrideWins(t *testing.T) {
	spec := `x-app-env: &app-env
  environment:
    CONSIGN_MOD_API_URL: "https://dev-api.example.org"
services:
  api-1:
    image: x
    <<: *app-env
    environment:
      CONSIGN_MOD_API_URL: "https://peer-api.example.net"
`
	lookup := lookupMap(map[string][]string{
		"dev-api.example.org":  {"127.0.0.1"},
		"peer-api.example.net": {"127.0.0.1"},
	})
	_, injections, err := injectAPIHostYAML(spec, "", lookup)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "peer-api.example.net" {
		t.Fatalf("explicit environment: must win over the merged-in one: injections=%v", injections)
	}
}

// a merge-provided extra_hosts pin is still respected (never double-pinned).
func TestMergedExtraHostsUserEntryPreserved(t *testing.T) {
	spec := `x-pin: &pin
  extra_hosts:
    - "dev-api.example.org:10.0.0.5"
services:
  api-1:
    image: x
    <<: *pin
    environment:
      CONSIGN_MOD_API_URL: "https://dev-api.example.org"
`
	out, injections, err := injectAPIHostYAML(spec, "", loopback)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 0 || out != spec {
		t.Fatalf("merged user entry must survive untouched: injections=%v", injections)
	}
}

// URL → hostname extraction
func TestApiHostFromURL(t *testing.T) {
	cases := map[string]string{
		"https://dev-api.example.org/v1":   "dev-api.example.org",
		"dev-api.example.org":              "dev-api.example.org",
		"http://api:8080/v1":               "api",
		"https://dev-api.example.org:8443": "dev-api.example.org",
		"":                                 "",
	}
	for in, want := range cases {
		if got := apiHostFromURL(in); got != want {
			t.Fatalf("apiHostFromURL(%q)=%q want %q", in, got, want)
		}
	}
}

// loopback detection
// B86i (stack 114 repro) — the exact rendered shape a `hosts`-posture stack
// service carries by the time it reaches injectStackAPIHost: `_apply_egress_networks`
// has already (1) attached the service to the `mod-stack-egress` network and
// (2) appended a COMMA-joined MOD_ALLOWED_EGRESS_HOSTS (api/_shared.py:2893
// `",".join(...)`, NOT space-separated) alongside the app's own
// CONSIGN_MOD_API_URL. This must still trigger extra_hosts injection for the
// loopback-resolving platform host.
func TestStack114ShapeLoopbackInjected(t *testing.T) {
	spec := `services:
  db:
    image: postgres:17
    environment:
      POSTGRES_DB: consign
      POSTGRES_USER: consign
      POSTGRES_PASSWORD: secret
    networks:
      mod-stack-egress: {}
  api:
    image: consign-api:latest
    expose:
      - "8000"
    depends_on:
      db:
        condition: service_healthy
    environment:
      CONSIGN_EBAY_ENV: sandbox
      CONSIGN_MOD_API_KEY: secretkey
      CONSIGN_MOD_API_URL: https://dev-api.example.org
      CONSIGN_MOD_CA_FILE: /srv/mod-ca/mod-local-ca.crt
      CONSIGN_DATABASE_URL: postgresql+psycopg://consign:secret@db:5432/consign
      CONSIGN_EBAY_MARKETPLACE: EBAY_US
      MOD_ALLOWED_EGRESS_HOSTS: api.sandbox.ebay.com,api.ebay.com,upload.sandbox.ebay.com,upload.ebay.com,dev-api.example.org
    networks:
      mod-stack-egress:
        aliases:
          - api
`
	lookup := lookupMap(map[string][]string{
		"api":                 {}, // worker's own apiURL host — unresolvable from inside the worker, not loopback
		"dev-api.example.org": {"127.0.0.1"},
	})
	out, injections, err := injectAPIHostYAML(spec, "api", lookup)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "dev-api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	for _, svc := range []string{"db", "api"} {
		if len(h[svc]) != 1 || h[svc][0] != "dev-api.example.org:host-gateway" {
			t.Fatalf("%s extra_hosts=%v", svc, h[svc])
		}
	}
}

// B86i-e — a compose YAML that fails to parse at the api-host injection step
// must reach `docker logs my_mod_worker` (e.log), not only cb.OnLogBatch.
// For a DirectTransport (embedded) worker — my_mod_worker's own shape —
// OnLogBatch writes straight to Redis with a 24h TTL and never touches the
// worker's own stdout (worker_tasks.py stream_task_log), so a silent parse
// failure here was indistinguishable, from the host, from "nothing needed
// injecting": zero log line either way.
func TestInjectStackAPIHostLogsParseErrorToWorkerLog(t *testing.T) {
	var buf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&buf, nil))}
	// `environment: [` with no closing — a hand-authored compose typo, the
	// cheapest reliable way to force injectAPIHostYAML's yaml.Unmarshal to
	// fail without depending on any particular real-world payload shape.
	broken := "services:\n  app-1:\n    environment: [\n"
	out := e.injectStackAPIHost(context.Background(), Callbacks{}, "http://api:80", broken)
	if out != broken {
		t.Fatalf("a parse error must ship the spec unchanged: %q", out)
	}
	if !strings.Contains(buf.String(), "api-host injection skipped") {
		t.Fatalf("parse error must reach the worker's own log, got: %q", buf.String())
	}
}

// B86i-f — resolveAPIHostAddrs must retry THROUGH a single NXDOMAIN, not
// trust it as definitive: Docker Desktop/WSL2's embedded DNS can answer
// NXDOMAIN on the very first query right after a fresh network attach, then
// answer correctly (loopback) a few hundred ms later — see stackapihost.go's
// B86i-f comment. A fake resolver reproduces exactly that shape.
func TestResolveAPIHostAddrsRetriesThroughTransientNXDOMAIN(t *testing.T) {
	orig := lookupIPAddr
	defer func() { lookupIPAddr = orig }()
	var calls int32
	lookupIPAddr = func(ctx context.Context, host string) ([]net.IPAddr, error) {
		if atomic.AddInt32(&calls, 1) == 1 {
			return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
		}
		return []net.IPAddr{{IP: net.ParseIP("127.0.0.1")}}, nil
	}
	var buf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&buf, nil))}
	addrs := e.resolveAPIHostAddrs(context.Background(), "dev-api.example.org")
	if len(addrs) != 1 || addrs[0] != "127.0.0.1" {
		t.Fatalf("expected the second attempt's loopback answer, got %v (log: %s)", addrs, buf.String())
	}
	if calls != 2 {
		t.Fatalf("expected exactly 2 lookup attempts, got %d", calls)
	}
}

// B86i-f — a host that genuinely never resolves must still end up with no
// injection (unchanged behavior), but the conclusion must now reach
// `docker logs my_mod_worker` at Debug — the overwhelmingly common
// not-co-located case must not flood Warn/Info.
func TestResolveAPIHostAddrsLogsDebugOnGenuineNotFound(t *testing.T) {
	orig := lookupIPAddr
	defer func() { lookupIPAddr = orig }()
	lookupIPAddr = func(ctx context.Context, host string) ([]net.IPAddr, error) {
		return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
	}
	var buf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))}
	addrs := e.resolveAPIHostAddrs(context.Background(), "not-co-located.example.com")
	if len(addrs) != 0 {
		t.Fatalf("expected no addrs for a genuine NXDOMAIN, got %v", addrs)
	}
	if !strings.Contains(buf.String(), "no such host after retries") {
		t.Fatalf("genuine negative must still log at Debug, got: %q", buf.String())
	}
	if strings.Contains(buf.String(), "level=WARN") {
		t.Fatalf("genuine negative must not log at Warn (routine case): %q", buf.String())
	}
}

// B86i-f — "nothing needed injecting" must reach the worker's own log at
// Debug too, covering every silent early return inside injectAPIHostYAML
// (not just a YAML parse error, which already logged).
func TestInjectStackAPIHostLogsDebugWhenNothingToInject(t *testing.T) {
	var buf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))}
	spec := `services:
  app-1:
    image: x
    environment:
      SOME_URL: https://routable.example.com
`
	orig := lookupIPAddr
	defer func() { lookupIPAddr = orig }()
	lookupIPAddr = func(ctx context.Context, host string) ([]net.IPAddr, error) {
		return []net.IPAddr{{IP: net.ParseIP("203.0.113.9")}}, nil
	}
	out := e.injectStackAPIHost(context.Background(), Callbacks{}, "http://api:80", spec)
	if out != spec {
		t.Fatalf("a routable host must ship the spec unchanged: %q", out)
	}
	if !strings.Contains(buf.String(), "nothing to inject") {
		t.Fatalf("a no-op injection must still log at Debug, got: %q", buf.String())
	}
}

// B86i-f — regression against the real stack 114 compose bytes (captured as
// testdata_stack114.yaml, x-mod blocks, secret placeholders like
// <DB_PASSWORD>, and all — nothing is pre-stripped before this function sees
// it). Every service (db, api, web) must receive the host-gateway pin for
// dev-api.example.org once the resolver reports loopback, including db
// and web which never reference the host in their own `environment` —
// matching the loop in injectAPIHostYAML that applies every co-located host
// to every service, not just the one(s) that named it.
func TestStack114FixtureLoopbackInjected(t *testing.T) {
	raw, err := os.ReadFile("testdata_stack114.yaml")
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	lookup := lookupMap(map[string][]string{
		"dev-api.example.org": {"127.0.0.1"},
	})
	out, injections, err := injectAPIHostYAML(string(raw), "", lookup)
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "dev-api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	for _, svc := range []string{"db", "api", "web"} {
		if len(h[svc]) != 1 || h[svc][0] != "dev-api.example.org:host-gateway" {
			t.Fatalf("%s extra_hosts=%v (full output: %s)", svc, h[svc], out)
		}
	}
}

func TestIsLoopbackAddr(t *testing.T) {
	yes := []string{"127.0.0.1", "127.0.0.53", "::1", "0.0.0.0"}
	no := []string{"203.0.113.9", "203.0.113.7", "10.0.0.5", "garbage"}
	for _, s := range yes {
		if !isLoopbackAddr(s) {
			t.Fatalf("%s must be loopback", s)
		}
	}
	for _, s := range no {
		if isLoopbackAddr(s) {
			t.Fatalf("%s must not be loopback", s)
		}
	}
}

// B86i-i — the platform's own public API hostname (PROTOCOL/API_SUBDOMAIN/
// BASE_DOMAIN from the worker's bootstrap env) must join the candidate set even
// when NO service carries a URL-shaped env value: on hosts whose worker apiURL
// is the internal manager name ("http://api", host-local) the public host is the
// only hostname that leaks to loopback via the host's /etc/hosts, so every
// service needs the host-gateway pin.
func TestPublicAPIHostNoEnvURLsInjectedEverywhere(t *testing.T) {
	spec := `services:
  llm-1:
    image: modstack1g0-llm-1
  ollama-1:
    image: modstack34g0-ollama-1
`
	lookup := lookupMap(map[string][]string{
		"api":             {}, // the worker's own apiURL host: unresolvable inside the worker
		"api.example.org": {"127.0.0.1"},
	})
	out, injections, err := injectAPIHostYAML(spec, "api", lookup,
		publicAPIHost("https", "api", "example.org"))
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	for _, svc := range []string{"llm-1", "ollama-1"} {
		if len(h[svc]) != 1 || h[svc][0] != "api.example.org:host-gateway" {
			t.Fatalf("%s extra_hosts=%v", svc, h[svc])
		}
	}
}

// B86i-i — a public host the author already pinned in extra_hosts is never
// overridden (their mapping wins); the other services still get the pin.
func TestPublicAPIHostUserPinnedUntouched(t *testing.T) {
	spec := `services:
  app-1:
    image: x
    extra_hosts:
      - "api.example.org:203.0.113.9"
  other-1:
    image: y
`
	lookup := lookupMap(map[string][]string{"api.example.org": {"127.0.0.1"}})
	out, injections, err := injectAPIHostYAML(spec, "", lookup,
		publicAPIHost("https", "api", "example.org"))
	if err != nil {
		t.Fatalf("inject: %v", err)
	}
	if len(injections) != 1 || injections[0].Host != "api.example.org" {
		t.Fatalf("injections=%v", injections)
	}
	h := hostsOf(out)
	if len(h["app-1"]) != 1 || h["app-1"][0] != "api.example.org:203.0.113.9" {
		t.Fatalf("app-1 extra_hosts=%v (author pin must survive)", h["app-1"])
	}
	if len(h["other-1"]) != 1 || h["other-1"][0] != "api.example.org:host-gateway" {
		t.Fatalf("other-1 extra_hosts=%v", h["other-1"])
	}
}

// B86i-i — the public host must contribute NOTHING when any of the three
// bootstrap parts is empty (or when the assembled host fails the shape guard),
// and the deploy must still emit the B86i-f Debug no-op line.
func TestPublicAPIHostMissingEnvPartNoInjection(t *testing.T) {
	if publicAPIHost("https", "api", "example.org") != "api.example.org" {
		t.Fatalf("golden shape must assemble")
	}
	if publicAPIHost("https:", "api", "example.org") != "api.example.org" {
		t.Fatalf("a PROTOCOL written 'https:' must still assemble")
	}
	for _, parts := range [][3]string{
		{"https", "api", ""}, {"", "api", "example.com"}, {"https", "", "example.com"},
		{"https", "api", "localhost"}, {"https", "api", "127.0.0.1"},
		{"https", "api", "evil.com/redirect"},
	} {
		if got := publicAPIHost(parts[0], parts[1], parts[2]); got != "" {
			t.Fatalf("publicAPIHost(%q,%q,%q)=%q must be empty", parts[0], parts[1], parts[2], got)
		}
	}
	// the env reader is exactly the pure core over the three bootstrap vars
	if got, want := publicAPIHostFromEnv(), publicAPIHost(os.Getenv("PROTOCOL"),
		os.Getenv("API_SUBDOMAIN"), os.Getenv("BASE_DOMAIN")); got != want {
		t.Fatalf("publicAPIHostFromEnv()=%q want %q", got, want)
	}
	if publicAPIHostFromEnv() != "" {
		// the test process carries a full bootstrap env — the unchanged-spec
		// assertion below would not hold, so only the wiring above is checked.
		return
	}
	var buf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))}
	spec := `services:
  app-1:
    image: x
`
	out := e.injectStackAPIHost(context.Background(), Callbacks{}, "http://api", spec)
	if out != spec {
		t.Fatalf("no candidate host means the spec ships unchanged: %q", out)
	}
	if !strings.Contains(buf.String(), "nothing to inject") {
		t.Fatalf("the no-op must still log at Debug, got: %q", buf.String())
	}
}

// --- Q14=A CA-trust wrapper tests (978a) ---

// entrypointScriptOf returns the `sh -c <script>` body the wrapper installed
// on svc, or "" if the service's entrypoint isn't the ["sh","-c",script] shape.
func entrypointScriptOf(y, svcName string) string {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(y), &root); err != nil || len(root.Content) == 0 {
		return ""
	}
	svcs := mapGet(root.Content[0], "services")
	if svcs == nil {
		return ""
	}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		if svcs.Content[i].Value != svcName {
			continue
		}
		ep := mapGet(svcs.Content[i+1], "entrypoint")
		if ep == nil || ep.Kind != yaml.SequenceNode || len(ep.Content) != 3 {
			return ""
		}
		if ep.Content[0].Value != "sh" || ep.Content[1].Value != "-c" {
			return ""
		}
		return ep.Content[2].Value
	}
	return ""
}

// commandOf returns the compose `command` list values for svcName, or nil.
func commandOf(y, svcName string) []string {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(y), &root); err != nil || len(root.Content) == 0 {
		return nil
	}
	svcs := mapGet(root.Content[0], "services")
	if svcs == nil {
		return nil
	}
	for i := 0; i+1 < len(svcs.Content); i += 2 {
		if svcs.Content[i].Value != svcName {
			continue
		}
		cmd := mapGet(svcs.Content[i+1], "command")
		if cmd == nil || cmd.Kind != yaml.SequenceNode {
			return nil
		}
		var out []string
		for _, it := range cmd.Content {
			out = append(out, it.Value)
		}
		return out
	}
	return nil
}

// exec-form entrypoint+command: args (including one with a space and a quote)
// are preserved exactly in the rendered `command:` list, and the wrapper
// script execs via "$0" "$@" rather than re-embedding the args as text.
func TestCAWrapExecFormPreservesArgsExactly(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: ["python3", "-u"]
    command: ["/app/run.py", "--name", "o'Brien thing", "--path", "/a b/c"]
`
	out, injections, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(servicesOf(injections)) != 1 {
		t.Fatalf("expected svc1 touched, got %v", injections)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper, got entrypoint shape missing; out=%s", out)
	}
	if !strings.Contains(script, `exec "$0" "$@"`) {
		t.Fatalf("exec-form wrapper must re-exec via $0/$@, got: %q", script)
	}
	want := []string{"python3", "-u", "/app/run.py", "--name", "o'Brien thing", "--path", "/a b/c"}
	got := commandOf(out, "svc1")
	if len(got) != len(want) {
		t.Fatalf("command=%v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("command[%d]=%q want %q (arg with space/quote must survive verbatim)", i, got[i], want[i])
		}
	}
}

// shell-form entrypoint (scalar) is embedded verbatim after "exec ", not
// restructured into an argv list.
func TestCAWrapShellFormEntrypointEmbeddedVerbatim(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: "/bin/sh -c 'start.sh'"
`
	out, _, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper; out=%s", out)
	}
	if !strings.Contains(script, `exec /bin/sh -c 'start.sh' "$@"`) {
		t.Fatalf("shell-form entrypoint must be embedded verbatim, got: %q", script)
	}
}

// a service with neither entrypoint nor command is left alone: no CA fetch,
// no entrypoint added at all.
func TestCAWrapNoEntrypointOrCommandSkipped(t *testing.T) {
	spec := `services:
  svc1:
    image: x
`
	out, injections, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(servicesOf(injections)) != 1 {
		t.Fatalf("expected svc1 still touched for the loopback host entry, got %v", injections)
	}
	if script := entrypointScriptOf(out, "svc1"); script != "" {
		t.Fatalf("service with no entrypoint/command must not get a CA wrapper, got script: %q", script)
	}
}

// an author-set SSL_CERT_FILE is respected: the wrapper does not export over
// it, but still exports the vars the author did NOT set (REQUESTS_CA_BUNDLE,
// NODE_EXTRA_CA_CERTS, CURL_CA_BUNDLE). Also covers Consign's own
// CONSIGN_MOD_CA_FILE-style workaround continuing to work unmolested: the
// wrapper never touches any env key other than the four it manages.
func TestCAWrapAuthorSetEnvRespected(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: ["/app/run"]
    environment:
      SSL_CERT_FILE: /custom/path.pem
`
	out, _, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper; out=%s", out)
	}
	if strings.Contains(script, "export SSL_CERT_FILE=") {
		t.Fatalf("author-set SSL_CERT_FILE must not be overridden, got: %q", script)
	}
	if !strings.Contains(script, "export REQUESTS_CA_BUNDLE="+modCABundle) {
		t.Fatalf("REQUESTS_CA_BUNDLE (not author-set) must still be exported, got: %q", script)
	}
	if !strings.Contains(script, "export NODE_EXTRA_CA_CERTS="+modCAFile) {
		t.Fatalf("NODE_EXTRA_CA_CERTS (not author-set) must still be exported, got: %q", script)
	}
	if !strings.Contains(script, "export CURL_CA_BUNDLE="+modCABundle) {
		t.Fatalf("CURL_CA_BUNDLE (not author-set) must still be exported, got: %q", script)
	}
}

// an author-set CURL_CA_BUNDLE (e.g. Consign's own CONSIGN_MOD_CA_FILE-style
// workaround pointed at it) is respected: the wrapper never overrides it, but
// still exports the other three vars the author did not set.
func TestCAWrapAuthorSetCurlBundleRespected(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: ["/app/run"]
    environment:
      CURL_CA_BUNDLE: /custom/curl-bundle.pem
`
	out, _, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper; out=%s", out)
	}
	if strings.Contains(script, "export CURL_CA_BUNDLE=") {
		t.Fatalf("author-set CURL_CA_BUNDLE must not be overridden, got: %q", script)
	}
	if !strings.Contains(script, "export SSL_CERT_FILE="+modCABundle) {
		t.Fatalf("SSL_CERT_FILE (not author-set) must still be exported, got: %q", script)
	}
}

// the rendered wrapper builds a merged bundle (system roots + the fetched MOD
// CA) rather than pointing SSL_CERT_FILE/REQUESTS_CA_BUNDLE at the MOD CA
// alone, and only exports them once both the fetch and the bundle succeeded.
func TestCAWrapScriptContainsMergedBundleLogic(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: ["/app/run"]
`
	out, _, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper; out=%s", out)
	}
	for _, want := range []string{
		"command -v wget >/dev/null 2>&1; then wget -q -T 10 -O " + modCAFile,
		"elif command -v curl >/dev/null 2>&1; then curl -fsS -m 10 -o " + modCAFile,
		"http://dev-api.example.org/mod-local-ca.crt",
		"|| rm -f " + modCAFile,
		"for f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem",
		"cp \"$$f\" " + modCABundle,
		"cat " + modCAFile + " >> " + modCABundle,
		"[ -s " + modCAFile + " ] && [ -s " + modCABundle + " ]",
		"export SSL_CERT_FILE=" + modCABundle,
		"export REQUESTS_CA_BUNDLE=" + modCABundle,
		"export NODE_EXTRA_CA_CERTS=" + modCAFile,
		"export CURL_CA_BUNDLE=" + modCABundle,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("wrapper script missing %q, got: %q", want, script)
		}
	}
	if strings.Contains(script, "SSL_CERT_FILE="+modCAFile+";") || strings.Contains(script, "SSL_CERT_FILE="+modCAFile+" ") {
		t.Fatalf("SSL_CERT_FILE must never point at the MOD CA alone, got: %q", script)
	}
}

// 1656: the fetch must be capability-probed — slim/unprivileged images ship
// wget, curl, or neither; a bare `wget ...` on an image without wget logs
// "wget: not found" (exit 127) on every start. The probe must come before
// any wget invocation, and the curl branch must exist as the fallback.
func TestCAWrapFetchProbesWgetBeforeUsingIt(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: ["/app/run"]
`
	out, _, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper; out=%s", out)
	}
	wgetProbe := strings.Index(script, "command -v wget")
	wgetCall := strings.Index(script, "wget -q -T 10")
	if wgetProbe < 0 || wgetCall < 0 || wgetProbe > wgetCall {
		t.Fatalf("wget must be probed (command -v wget) before it is invoked, got: %q", script)
	}
	if !strings.Contains(script, "elif command -v curl") ||
		!strings.Contains(script, "curl -fsS -m 10 -o "+modCAFile) {
		t.Fatalf("curl fallback branch missing, got: %q", script)
	}
}

// 1656: /mod-ca at filesystem root is not writable by non-root image users
// (nginxinc/nginx-unprivileged runs as uid 101) -> "mkdir: cannot create
// directory '/mod-ca': Permission denied" on every start. The CA dir lives
// under /tmp, mirroring the mesh sidecars' /tmp/mesh-ca precedent.
func TestCAWrapCADirUnderTmp(t *testing.T) {
	if !strings.HasPrefix(modCADir, "/tmp/") {
		t.Fatalf("modCADir must live under /tmp, got %q", modCADir)
	}
	if modCAFile != modCADir+"/mod-local-ca.crt" || modCABundle != modCADir+"/bundle.pem" {
		t.Fatalf("CA paths must be rooted at modCADir, got %q %q %q", modCADir, modCAFile, modCABundle)
	}
	spec := `services:
  svc1:
    image: x
    entrypoint: ["/app/run"]
`
	out, _, err := injectAPIHostYAML(spec, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	script := entrypointScriptOf(out, "svc1")
	if script == "" {
		t.Fatalf("expected sh -c wrapper; out=%s", out)
	}
	if strings.Contains(script, "mkdir -p /mod-ca") || strings.Contains(script, "mkdir -p /mod-ca/") {
		t.Fatalf("wrapper must not mkdir a root-level /mod-ca, got: %q", script)
	}
	if !strings.Contains(script, "mkdir -p "+modCADir) {
		t.Fatalf("wrapper must mkdir the /tmp CA dir, got: %q", script)
	}
}

// an unsafe host (fails caHostSafe) gets no CA wrap at all, even though the
// loopback extra_hosts entry can still legitimately apply.
func TestCAWrapUnsafeHostSkipped(t *testing.T) {
	if caHostSafe("evil; rm -rf /") {
		t.Fatalf("caHostSafe must reject shell metacharacters")
	}
	if caHostSafe("") {
		t.Fatalf("caHostSafe must reject empty host")
	}
}

// Q14=A no-op: when the API host does not resolve to loopback on this worker
// (not co-located, i.e. no local CA to fetch), neither extra_hosts nor the
// CA wrapper is injected into any service — a public-CA environment sees a
// byte-identical compose render.
func TestCAWrapNotInjectedWhenHostNotLoopback(t *testing.T) {
	spec := `services:
  svc1:
    image: x
    entrypoint: ["/app/run"]
`
	out, injections, err := injectAPIHostYAML(spec, "dev-api.example.org", routable)
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if len(injections) != 0 {
		t.Fatalf("expected no injections for a non-loopback host, got: %v", injections)
	}
	if out != spec {
		t.Fatalf("expected unmodified compose, got: %q", out)
	}
}

// Stack 22 outage 10-17: a netns joiner (switchyard behind its tsegress
// sidecar) got extra_hosts and docker refused the whole compose up with
// "conflicting options: custom host-to-IP mapping and the network mode".
func TestInjectAPIHost_SkipsNetnsJoiners(t *testing.T) {
	base := `services:
  app:
    image: x
    network_mode: "service:egress"
  egress:
    image: y
  other:
    image: z
    network_mode: container:abc
`
	out, injections, err := injectAPIHostYAML(base, "dev-api.example.org", loopback)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if len(injections) != 1 {
		t.Fatalf("want 1 injection, got %+v", injections)
	}
	var doc map[string]map[string]map[string]interface{}
	if err := yaml.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatalf("parse out: %v", err)
	}
	for _, name := range []string{"app", "other"} {
		if _, ok := doc["services"][name]["extra_hosts"]; ok {
			t.Fatalf("%s shares a netns and must not get extra_hosts; out=%s", name, out)
		}
	}
	if _, ok := doc["services"]["egress"]["extra_hosts"]; !ok {
		t.Fatalf("netns owner must get extra_hosts; out=%s", out)
	}
}
