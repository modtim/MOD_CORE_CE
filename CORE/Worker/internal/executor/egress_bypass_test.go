package executor

// delegated-stack-authoring §5.4/§5.6 — the worker refuses a spec that smuggles a
// service onto the reserved platform network, defeating the API's egress rewrite.

import "testing"

const platformNet = "mod_modtex-network"

func TestRefuseEgressBypass_ExternalPlatformNetworkAttached(t *testing.T) {
	// A hostile author declares the platform network as external and joins it.
	spec := `
services:
  app:
    image: alpine
    networks: [platform]
networks:
  platform:
    name: mod_modtex-network
    external: true
`
	if err := refuseEgressBypass(spec, platformNet); err == nil {
		t.Fatal("expected refusal: service attaches to reserved platform network via external alias")
	}
}

func TestRefuseEgressBypass_BareServiceNetworkByName(t *testing.T) {
	// No top-level declaration, but the service references the platform name directly.
	spec := `
services:
  app:
    image: alpine
    networks:
      mod_modtex-network: {}
`
	if err := refuseEgressBypass(spec, platformNet); err == nil {
		t.Fatal("expected refusal: bare service networks references the platform network name")
	}
}

func TestRefuseEgressBypass_SanctionedNetworksAllowed(t *testing.T) {
	// The shape the API rewrite produces: isolated + api-net, never the platform net.
	spec := `
services:
  db:
    image: postgres
    networks: [mod-stack-isolated]
  api-caller:
    image: alpine
    networks: [mod_stack-api-net]
networks:
  mod-stack-isolated:
    driver: bridge
    internal: true
  mod_stack-api-net:
    name: mod_stack-api-net
    external: true
`
	if err := refuseEgressBypass(spec, platformNet); err != nil {
		t.Fatalf("sanctioned spec should pass, got: %v", err)
	}
}

func TestRefuseEgressBypass_MeshSidecarCarriesNetwork(t *testing.T) {
	// A reachable service borrows its sidecar's netns; the sidecar (not the service)
	// carries the egress network. Neither touches the platform net → allowed.
	spec := `
services:
  ts-app:
    image: tailscale
    networks: [mod-stack-egress]
  app:
    image: alpine
    network_mode: "service:ts-app"
networks:
  mod-stack-egress:
    driver: bridge
`
	if err := refuseEgressBypass(spec, platformNet); err != nil {
		t.Fatalf("mesh-backed sanctioned spec should pass, got: %v", err)
	}
}

func TestRefuseEgressBypass_UnparseableRefused(t *testing.T) {
	// A spec we cannot parse is one we cannot vouch for — fail closed.
	if err := refuseEgressBypass("{ this: is: not: valid: yaml", platformNet); err == nil {
		t.Fatal("expected refusal on an unparseable compose spec")
	}
}
