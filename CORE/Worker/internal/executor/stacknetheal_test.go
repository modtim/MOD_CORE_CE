package executor

import (
	"context"
	"fmt"
	"reflect"
	"strings"
	"testing"
)

// The stack-14 shape the API renders: an isolated service plus its ingress
// forwarder on isolated + egress, a one-shot on egress.
const healSpec = `
services:
  weights:
    image: fetcher
    networks: [mod-stack-egress]
  llama-server:
    image: llama
    networks: [mod-stack-isolated]
  ingress-llama-server:
    image: fwd
    ports: ["8000:8000"]
    networks: [mod-stack-isolated, mod-stack-egress]
networks:
  mod-stack-isolated: {driver: bridge, internal: true}
  mod-stack-egress: {driver: bridge}
`

func netNames(p serviceNetPlan) []string {
	var out []string
	for _, n := range p.Nets {
		out = append(out, n.Network)
	}
	return out
}

func TestExpectedStackNetworks_ListForm(t *testing.T) {
	plans, err := expectedStackNetworks(healSpec, "modstack14g0")
	if err != nil {
		t.Fatal(err)
	}
	got := map[string][]string{}
	ports := map[string]bool{}
	for _, p := range plans {
		got[p.Service] = netNames(p)
		ports[p.Service] = p.HasPorts
	}
	want := map[string][]string{
		"weights":              {"modstack14g0_mod-stack-egress"},
		"llama-server":         {"modstack14g0_mod-stack-isolated"},
		"ingress-llama-server": {"modstack14g0_mod-stack-egress", "modstack14g0_mod-stack-isolated"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	if !ports["ingress-llama-server"] || ports["llama-server"] {
		t.Fatalf("HasPorts wrong: %v", ports)
	}
}

func TestExpectedStackNetworks_MapNamedExternalDefaultAndNetworkMode(t *testing.T) {
	spec := `
services:
  nim:
    image: nim
    networks:
      mod_stack-api-net:
        aliases: [NIM-nim]
      named: {}
      plain:
  sidecar-served:
    image: app
    network_mode: "service:ts-app"
  bare:
    image: x
networks:
  mod_stack-api-net: {name: mod_stack-api-net, external: true}
  named: {name: custom-net}
  plain: {}
  legacy: {external: {name: legacy-real}}
`
	plans, err := expectedStackNetworks(spec, "ModStack22G1")
	if err != nil {
		t.Fatal(err)
	}
	by := map[string]serviceNetPlan{}
	for _, p := range plans {
		by[p.Service] = p
	}
	if _, ok := by["sidecar-served"]; ok {
		t.Fatal("network_mode service must be skipped")
	}
	if got := netNames(by["bare"]); !reflect.DeepEqual(got, []string{"modstack22g1_default"}) {
		t.Fatalf("bare: %v", got)
	}
	nim := by["nim"]
	want := []expectedAttach{
		{Network: "mod_stack-api-net", Aliases: []string{"nim", "NIM-nim"}},
		{Network: "custom-net", Aliases: []string{"nim"}},
		{Network: "modstack22g1_plain", Aliases: []string{"nim"}},
	}
	if !reflect.DeepEqual(nim.Nets, want) {
		t.Fatalf("nim: %+v", nim.Nets)
	}
}

func TestExpectedStackNetworks_LegacyExternal(t *testing.T) {
	spec := `
services:
  a: {image: x, networks: [legacy, ext]}
networks:
  legacy: {external: {name: legacy-real}}
  ext: {external: true}
`
	plans, err := expectedStackNetworks(spec, "p")
	if err != nil {
		t.Fatal(err)
	}
	if got := netNames(plans[0]); !reflect.DeepEqual(got, []string{"ext", "legacy-real"}) {
		t.Fatalf("got %v", got)
	}
}

func TestMissingNetworks(t *testing.T) {
	exp := []expectedAttach{{Network: "p_iso"}, {Network: "p_egress"}}
	if m := missingNetworks(exp, []string{"p_iso"}); len(m) != 1 || m[0].Network != "p_egress" {
		t.Fatalf("got %+v", m)
	}
	if m := missingNetworks(exp, []string{"p_egress", "p_iso", "extra"}); len(m) != 0 {
		t.Fatalf("got %+v", m)
	}
}

func TestParseInspectNetworks(t *testing.T) {
	name, running, attached, err := parseInspectNetworks(
		`/modstack14g0-ingress-llama-server-1|true|{"modstack14g0_mod-stack-isolated":{"NetworkID":"abc123","Aliases":["ingress-llama-server"]}}` + "\n")
	if err != nil || name != "modstack14g0-ingress-llama-server-1" || !running {
		t.Fatalf("%q %v %v", name, running, err)
	}
	got, ok := attached["modstack14g0_mod-stack-isolated"]
	if !ok || got.NetworkID != "abc123" || !reflect.DeepEqual(got.Aliases, []string{"ingress-llama-server"}) {
		t.Fatalf("attached = %+v", attached)
	}
}

func TestCtrNetAttachHasAlias(t *testing.T) {
	a := ctrNetAttach{Aliases: []string{"llama-server"}}
	if !a.hasAlias("llama-server") || a.hasAlias("nope") {
		t.Fatalf("Aliases lookup wrong: %+v", a)
	}
	// Some Docker versions report aliases only via DNSNames.
	d := ctrNetAttach{DNSNames: []string{"llama-server", "abc123def456"}}
	if !d.hasAlias("llama-server") || d.hasAlias("nope") {
		t.Fatalf("DNSNames lookup wrong: %+v", d)
	}
}

// stacksubnet.go's redeploy fix stops compose from recreating an already-
// pinned network, but a network can still be recreated for other reasons
// (Docker/compose-version quirks, a manual `docker network rm`, ...): this is
// what has to catch that after the fact. A stale endpoint still names the
// right network, so the old name-only missingNetworks check saw nothing to
// repair — Docker's embedded DNS on the recreated network had simply never
// heard of the container's service name.
func TestStaleOrMissing(t *testing.T) {
	expected := []expectedAttach{
		{Network: "p_iso", Aliases: []string{"svc"}},
		{Network: "p_egress", Aliases: []string{"svc", "edge"}},
	}
	liveIDs := map[string]string{"p_iso": "new-id", "p_egress": "egress-id"}

	healthy := map[string]ctrNetAttach{
		"p_iso":    {NetworkID: "new-id", Aliases: []string{"svc"}},
		"p_egress": {NetworkID: "egress-id", Aliases: []string{"svc", "edge"}},
	}
	if got := staleOrMissing(expected, healthy, liveIDs); len(got) != 0 {
		t.Fatalf("healthy container flagged: %+v", got)
	}

	t.Run("stale network id", func(t *testing.T) {
		attached := map[string]ctrNetAttach{
			"p_iso":    {NetworkID: "old-id", Aliases: []string{"svc"}}, // network was deleted+recreated
			"p_egress": healthy["p_egress"],
		}
		got := staleOrMissing(expected, attached, liveIDs)
		if len(got) != 1 || got[0].Network != "p_iso" || got[0].Reason != "stale network id" || !got[0].WasConnected {
			t.Fatalf("got %+v", got)
		}
	})

	t.Run("missing alias", func(t *testing.T) {
		attached := map[string]ctrNetAttach{
			"p_iso":    healthy["p_iso"],
			"p_egress": {NetworkID: "egress-id", Aliases: []string{"svc"}}, // "edge" never applied
		}
		got := staleOrMissing(expected, attached, liveIDs)
		if len(got) != 1 || got[0].Network != "p_egress" || got[0].Reason != "missing alias" || !got[0].WasConnected {
			t.Fatalf("got %+v", got)
		}
	})

	t.Run("not attached at all", func(t *testing.T) {
		attached := map[string]ctrNetAttach{"p_iso": healthy["p_iso"]}
		got := staleOrMissing(expected, attached, liveIDs)
		if len(got) != 1 || got[0].Network != "p_egress" || got[0].Reason != "missing" || got[0].WasConnected {
			t.Fatalf("got %+v", got)
		}
	})

	t.Run("unknown live ID skips the ID check but still checks aliases", func(t *testing.T) {
		// dockerNetworkIDs is best-effort; when it could not resolve a
		// network's live ID at all, a mismatch must not be manufactured.
		attached := map[string]ctrNetAttach{
			"p_iso":    {NetworkID: "whatever", Aliases: []string{"svc"}},
			"p_egress": healthy["p_egress"],
		}
		got := staleOrMissing(expected, attached, map[string]string{})
		if len(got) != 0 {
			t.Fatalf("got %+v", got)
		}
	})
}

// The AlphaBox failure end-to-end against a fake daemon: the forwarder is on the
// isolated net only → egress gets connected (with the service alias) and the
// container restarted; healthy containers are left alone.
func TestHealStackNetworks_ReconnectsAndRestarts(t *testing.T) {
	var calls []string
	orig := dockerCLI
	defer func() { dockerCLI = orig }()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		calls = append(calls, strings.Join(args, " "))
		switch args[0] {
		case "ps":
			svc := strings.TrimPrefix(args[len(args)-1], "label=com.docker.compose.service=")
			return "id-" + svc + "\n", nil
		case "network":
			if args[1] == "inspect" {
				// dockerNetworkIDs's batched lookup — best-effort, unresolved
				// here is fine: it just means the ID-staleness check is inert
				// and only the alias check (below) matters for this test.
				return "", nil
			}
			return "", nil
		case "inspect":
			switch args[len(args)-1] {
			case "id-ingress-llama-server":
				// Only on isolated, with the right alias — egress is the one
				// missing entirely.
				return `/modstack14g0-ingress-llama-server-1|true|{"modstack14g0_mod-stack-isolated":{"Aliases":["ingress-llama-server"]}}`, nil
			case "id-llama-server":
				return `/modstack14g0-llama-server-1|true|{"modstack14g0_mod-stack-isolated":{"Aliases":["llama-server"]}}`, nil
			case "id-weights":
				return `/modstack14g0-weights-1|false|{"modstack14g0_mod-stack-egress":{"Aliases":["weights"]}}`, nil
			}
		case "restart":
			return "", nil
		}
		return "", fmt.Errorf("unexpected %v", args)
	}
	var streamed []string
	lines := (&Executor{}).healStackNetworks(context.Background(),
		Callbacks{OnLogBatch: func(l []string) { streamed = append(streamed, l...) }},
		"modstack14g0", healSpec)

	var mutating []string
	for _, c := range calls {
		if strings.HasPrefix(c, "network connect") || strings.HasPrefix(c, "network disconnect") || strings.HasPrefix(c, "restart") {
			mutating = append(mutating, c)
		}
	}
	want := []string{
		"network connect --alias ingress-llama-server modstack14g0_mod-stack-egress id-ingress-llama-server",
		"restart id-ingress-llama-server",
	}
	if !reflect.DeepEqual(mutating, want) {
		t.Fatalf("mutating calls %v, want %v", mutating, want)
	}
	if len(lines) != 2 || !strings.Contains(lines[0], "was missing on modstack14g0_mod-stack-egress; repaired") {
		t.Fatalf("lines %v", lines)
	}
	if !reflect.DeepEqual(streamed, lines) {
		t.Fatalf("streamed %v", streamed)
	}
}

// The healthy-network-name-but-wrong-ID case (see stacksubnet.go's redeploy
// fix): a network that was deleted and recreated keeps its name but the
// container's endpoint still points at the old, gone network. The old
// missing-by-name check never caught this; it must now disconnect the stale
// endpoint before reconnecting so the container actually lands on the live
// network with its alias.
func TestHealStackNetworks_StaleNetworkIDDisconnectsThenReconnects(t *testing.T) {
	var calls []string
	orig := dockerCLI
	defer func() { dockerCLI = orig }()
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		calls = append(calls, strings.Join(args, " "))
		switch args[0] {
		case "ps":
			svc := strings.TrimPrefix(args[len(args)-1], "label=com.docker.compose.service=")
			return "id-" + svc + "\n", nil
		case "network":
			if args[1] == "inspect" {
				return "modstack14g0_mod-stack-isolated|live-iso\nmodstack14g0_mod-stack-egress|live-egress\n", nil
			}
			return "", nil
		case "inspect":
			switch args[len(args)-1] {
			case "id-ingress-llama-server":
				return `/modstack14g0-ingress-llama-server-1|true|` +
					`{"modstack14g0_mod-stack-isolated":{"NetworkID":"live-iso","Aliases":["ingress-llama-server"]},` +
					`"modstack14g0_mod-stack-egress":{"NetworkID":"OLD-DELETED-NETWORK","Aliases":["ingress-llama-server"]}}`, nil
			case "id-llama-server":
				return `/modstack14g0-llama-server-1|true|{"modstack14g0_mod-stack-isolated":{"NetworkID":"live-iso","Aliases":["llama-server"]}}`, nil
			case "id-weights":
				return `/modstack14g0-weights-1|false|{"modstack14g0_mod-stack-egress":{"NetworkID":"live-egress","Aliases":["weights"]}}`, nil
			}
		case "restart":
			return "", nil
		}
		return "", fmt.Errorf("unexpected %v", args)
	}
	lines := (&Executor{}).healStackNetworks(context.Background(), Callbacks{}, "modstack14g0", healSpec)

	var mutating []string
	for _, c := range calls {
		if strings.HasPrefix(c, "network connect") || strings.HasPrefix(c, "network disconnect") || strings.HasPrefix(c, "restart") {
			mutating = append(mutating, c)
		}
	}
	want := []string{
		"network disconnect -f modstack14g0_mod-stack-egress id-ingress-llama-server",
		"network connect --alias ingress-llama-server modstack14g0_mod-stack-egress id-ingress-llama-server",
		"restart id-ingress-llama-server",
	}
	if !reflect.DeepEqual(mutating, want) {
		t.Fatalf("mutating calls %v, want %v", mutating, want)
	}
	if len(lines) != 2 || !strings.Contains(lines[0], "was stale network id on modstack14g0_mod-stack-egress; repaired") {
		t.Fatalf("lines %v", lines)
	}
}

// A failed connect is logged, never fatal, and does not trigger a restart.
func TestHealStackNetworks_ConnectFailureIsLoggedNotFatal(t *testing.T) {
	orig := dockerCLI
	defer func() { dockerCLI = orig }()
	restarted := false
	dockerCLI = func(_ context.Context, args ...string) (string, error) {
		switch args[0] {
		case "ps":
			return "abc\n", nil
		case "inspect":
			return `/c|true|{}`, nil
		case "network":
			return "boom", fmt.Errorf("exit 1")
		case "restart":
			restarted = true
		}
		return "", nil
	}
	spec := "services:\n  s: {image: x, ports: [\"1:1\"], networks: [n]}\nnetworks:\n  n: {}\n"
	lines := (&Executor{}).healStackNetworks(context.Background(), Callbacks{}, "p", spec)
	if restarted {
		t.Fatal("must not restart when nothing was reconnected")
	}
	if len(lines) != 1 || !strings.Contains(lines[0], "reconnect FAILED") {
		t.Fatalf("lines %v", lines)
	}
}
