package executor

import (
	"fmt"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
)

// egressRules is a small helper: the parsed spec.egress slice as
// []map[string]interface{}, for assertions that don't care about ordering
// beyond "the DNS rule comes first".
func egressRules(t *testing.T, obj *unstructured.Unstructured) []interface{} {
	t.Helper()
	rules, found, err := unstructured.NestedSlice(obj.Object, "spec", "egress")
	if err != nil || !found {
		t.Fatalf("spec.egress: found=%v err=%v", found, err)
	}
	return rules
}

// atPath walks a mix of map keys (string) and slice indices (int) through
// plain unstructured content (map[string]interface{} / []interface{}), the
// only shapes BuildFQDNPolicy is allowed to produce. unstructured.NestedX
// helpers can't do this themselves — every segment of their path is a map
// key, so they can't step into a []interface{} by position.
func atPath(t *testing.T, root interface{}, path ...interface{}) interface{} {
	t.Helper()
	cur := root
	for _, p := range path {
		switch key := p.(type) {
		case string:
			m, ok := cur.(map[string]interface{})
			if !ok {
				t.Fatalf("path %v: expected map at %q, got %T (%v)", path, key, cur, cur)
			}
			cur = m[key]
		case int:
			s, ok := cur.([]interface{})
			if !ok {
				t.Fatalf("path %v: expected slice at index %d, got %T (%v)", path, key, cur, cur)
			}
			if key < 0 || key >= len(s) {
				t.Fatalf("path %v: index %d out of range (len %d)", path, key, len(s))
			}
			cur = s[key]
		default:
			t.Fatalf("path %v: unsupported key type %T", path, p)
		}
	}
	return cur
}

func TestBuildFQDNPolicyRequiresNamespaceAndJobName(t *testing.T) {
	if _, err := BuildFQDNPolicy("", "job", types.UID("u"), []string{"example.com"}, ""); err == nil {
		t.Error("want error for empty namespace")
	}
	if _, err := BuildFQDNPolicy("ns", "", types.UID("u"), []string{"example.com"}, ""); err == nil {
		t.Error("want error for empty job name")
	}
}

func TestBuildFQDNPolicyExactHost(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "mod-task-abc123", types.UID("uid-1"), []string{"api.example.com"}, "")
	if err != nil {
		t.Fatal(err)
	}
	if obj.GetAPIVersion() != "cilium.io/v2" || obj.GetKind() != "CiliumNetworkPolicy" {
		t.Fatalf("apiVersion/kind = %s/%s", obj.GetAPIVersion(), obj.GetKind())
	}
	if obj.GetNamespace() != "mod-tasks" || obj.GetName() != "mod-task-abc123-fqdn" {
		t.Fatalf("namespace/name = %s/%s", obj.GetNamespace(), obj.GetName())
	}
	sel, found, err := unstructured.NestedString(obj.Object, "spec", "endpointSelector", "matchLabels", LabelJob)
	if err != nil || !found || sel != "mod-task-abc123" {
		t.Fatalf("endpointSelector matchLabels[%s] = %q found=%v err=%v", LabelJob, sel, found, err)
	}

	rules := egressRules(t, obj)
	if len(rules) != 2 {
		t.Fatalf("egress rules = %d, want 2 (dns + 1 host)", len(rules))
	}
	fqdnRule := rules[1].(map[string]interface{})
	name, _ := atPath(t, fqdnRule, "toFQDNs", 0, "matchName").(string)
	if name != "api.example.com" {
		t.Fatalf("toFQDNs[0].matchName = %q", name)
	}
	ports, ok := atPath(t, fqdnRule, "toPorts", 0, "ports").([]interface{})
	if !ok || len(ports) != 2 {
		t.Fatalf("toPorts = %v", ports)
	}
	wantPorts := map[string]bool{"80": true, "443": true}
	for _, p := range ports {
		pm := p.(map[string]interface{})
		if !wantPorts[pm["port"].(string)] || pm["protocol"] != "TCP" {
			t.Errorf("unexpected default port entry %v", pm)
		}
	}
}

func TestBuildFQDNPolicyWildcard(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"*.example.com"}, "")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	fqdnRule := rules[1].(map[string]interface{})
	pattern, _ := atPath(t, fqdnRule, "toFQDNs", 0, "matchPattern").(string)
	if pattern != "*.example.com" {
		t.Fatalf("toFQDNs[0].matchPattern = %q", pattern)
	}
}

func TestBuildFQDNPolicyExplicitPort(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"api.example.com:8443"}, "")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	fqdnRule := rules[1].(map[string]interface{})
	ports, ok := atPath(t, fqdnRule, "toPorts", 0, "ports").([]interface{})
	if !ok || len(ports) != 1 {
		t.Fatalf("toPorts = %v", ports)
	}
	pm := ports[0].(map[string]interface{})
	if pm["port"] != "8443" || pm["protocol"] != "TCP" {
		t.Fatalf("port entry = %v, want 8443/TCP", pm)
	}
}

func TestBuildFQDNPolicyIPLiteral(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"10.1.2.3"}, "")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	ipRule := rules[1].(map[string]interface{})
	cidrs, found, err := unstructured.NestedStringSlice(ipRule, "toCIDR")
	if err != nil || !found || len(cidrs) != 1 || cidrs[0] != "10.1.2.3/32" {
		t.Fatalf("toCIDR = %v found=%v err=%v", cidrs, found, err)
	}
	if _, present := ipRule["toFQDNs"]; present {
		t.Error("an IP literal must not also get a toFQDNs entry")
	}
}

func TestBuildFQDNPolicyCIDRLiteral(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"10.0.0.0/8"}, "")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	ipRule := rules[1].(map[string]interface{})
	cidrs, found, err := unstructured.NestedStringSlice(ipRule, "toCIDR")
	if err != nil || !found || len(cidrs) != 1 || cidrs[0] != "10.0.0.0/8" {
		t.Fatalf("toCIDR = %v found=%v err=%v", cidrs, found, err)
	}
}

func TestBuildFQDNPolicyInvalidEntries(t *testing.T) {
	cases := [][]string{
		{"*"},
		{""},
		{"not a host!"},
	}
	for _, hosts := range cases {
		if _, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), hosts, ""); err == nil {
			t.Errorf("hosts=%v: want error", hosts)
		}
	}
}

func TestBuildFQDNPolicyNoUsableHostsErrors(t *testing.T) {
	// All-empty/whitespace entries parse to zero rules (egress.Parse skips
	// them) — that must fail closed rather than emit a DNS-only policy that
	// looks like an allowlist but permits nothing (or worse, is a caller bug
	// masquerading as an intentional empty allowlist).
	if _, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"", "   "}, ""); err == nil {
		t.Error("want error when the allowlist parses to zero rules")
	}
}

func TestBuildFQDNPolicyOwnerReference(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "mod-task-xyz", types.UID("job-uid-789"), []string{"example.com"}, "")
	if err != nil {
		t.Fatal(err)
	}
	refs := obj.GetOwnerReferences()
	if len(refs) != 1 {
		t.Fatalf("ownerReferences = %d, want 1", len(refs))
	}
	ref := refs[0]
	if ref.Kind != "Job" || ref.APIVersion != "batch/v1" || ref.Name != "mod-task-xyz" || string(ref.UID) != "job-uid-789" {
		t.Fatalf("ownerReference = %+v", ref)
	}
	if ref.Controller == nil || !*ref.Controller {
		t.Error("ownerReference.Controller must be true")
	}
	if ref.BlockOwnerDeletion == nil || !*ref.BlockOwnerDeletion {
		t.Error("ownerReference.BlockOwnerDeletion must be true")
	}
}

func TestBuildFQDNPolicyDNSRuleFirstAndUsesNamespace(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"example.com"}, "custom-dns-ns")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	dnsRule := rules[0].(map[string]interface{})
	ns, _ := atPath(t, dnsRule, "toEndpoints", 0, "matchLabels", "io.kubernetes.pod.namespace").(string)
	if ns != "custom-dns-ns" {
		t.Fatalf("dns rule namespace = %q", ns)
	}
	app, _ := atPath(t, dnsRule, "toEndpoints", 0, "matchLabels", "k8s-app").(string)
	if app != "kube-dns" {
		t.Fatalf("dns rule k8s-app = %q", app)
	}
	pattern, _ := atPath(t, dnsRule, "toPorts", 0, "rules", "dns", 0, "matchPattern").(string)
	if pattern != "*" {
		t.Fatalf("dns rule L7 matchPattern = %q", pattern)
	}
}

func TestBuildFQDNPolicyDefaultDNSNamespace(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"example.com"}, "")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	dnsRule := rules[0].(map[string]interface{})
	ns, _ := atPath(t, dnsRule, "toEndpoints", 0, "matchLabels", "io.kubernetes.pod.namespace").(string)
	if ns != DefaultFQDNDNSNamespace {
		t.Fatalf("default dns namespace = %q, want %q", ns, DefaultFQDNDNSNamespace)
	}
}

func TestBuildFQDNPolicyMultipleHostsOnePerEgressEntry(t *testing.T) {
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"),
		[]string{"api.example.com", "*.cdn.example.com", "10.1.2.3:9000"}, "")
	if err != nil {
		t.Fatal(err)
	}
	rules := egressRules(t, obj)
	if len(rules) != 4 { // dns + 3 allowlist entries
		t.Fatalf("egress rules = %d, want 4", len(rules))
	}
}

func TestBuildFQDNPolicyRoundTripsThroughUnstructuredConverter(t *testing.T) {
	// Guards against accidentally putting a non-JSON-safe Go type (e.g. a
	// typed egress.Kind, netip.Prefix, or types.UID) directly into the
	// unstructured map, which would panic or silently corrupt when the
	// dynamic client marshals it (client-go requires pure
	// map[string]interface{}/[]interface{}/string/int64/bool/float64/nil).
	obj, err := BuildFQDNPolicy("mod-tasks", "job1", types.UID("u"), []string{"example.com", "10.0.0.0/8"}, "")
	if err != nil {
		t.Fatal(err)
	}
	if err := unstructuredIsJSONSafe(obj.Object); err != nil {
		t.Fatalf("not JSON-safe: %v", err)
	}
}

// unstructuredIsJSONSafe recursively checks that v only contains the types
// unstructured content is allowed to hold.
func unstructuredIsJSONSafe(v interface{}) error {
	switch val := v.(type) {
	case nil, bool, string, int64, float64:
		return nil
	case map[string]interface{}:
		for k, e := range val {
			if err := unstructuredIsJSONSafe(e); err != nil {
				return fmt.Errorf("key %q: %w", k, err)
			}
		}
		return nil
	case []interface{}:
		for i, e := range val {
			if err := unstructuredIsJSONSafe(e); err != nil {
				return fmt.Errorf("index %d: %w", i, err)
			}
		}
		return nil
	default:
		return fmt.Errorf("unsupported type %T in unstructured content", v)
	}
}
