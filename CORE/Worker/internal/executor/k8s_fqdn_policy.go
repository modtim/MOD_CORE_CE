package executor

// k8s-reduction-engine GB.EGR.2: renders the per-task Cilium
// CiliumNetworkPolicy (cilium.io/v2, `toFQDNs`) that lets an EgressAllowlist
// task pod (GB.EGR.1) reach exactly its declared hosts.
//
// Standard networking.k8s.io/v1 NetworkPolicy cannot match FQDNs, only
// IP/CIDR — that's why the static chart policies (BuildJob's
// mod.tech/egress-mode label) only get the task onto the user-egress
// network; this CNP is what actually narrows it down to the allowlist, and
// only exists when the cluster runs Cilium (K8sBackend.FQDNMode == "cilium",
// selected by MOD_K8S_EGRESS_FQDN in backend_select.go, GB.EGR.4).
//
// The grammar (exact host, "*.suffix" wildcard, host:port, IP/CIDR literal)
// is the shared internal/egress package (GB.EGR.D.1), so this and the Docker
// egress proxy (GB.EGR.D.2) enforce identically-parsed allowlists.
import (
	"fmt"
	"strconv"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/egress"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
)

// CiliumNetworkPolicyGVR identifies the Cilium CRD this policy is applied
// through (K8sBackend.Submit's Dynamic.Resource(...) call) and the RBAC
// resource name the Helm chart renders (GB.EGR.5).
var CiliumNetworkPolicyGVR = schema.GroupVersionResource{
	Group:    "cilium.io",
	Version:  "v2",
	Resource: "ciliumnetworkpolicies",
}

// DefaultFQDNDNSNamespace is used when BuildFQDNPolicy's caller has no
// operator override for where kube-dns lives.
const DefaultFQDNDNSNamespace = "kube-system"

// fqdnPolicyNameSuffix keeps the CNP name derived from, and trivially
// associated with, the Job it guards (jobNameFor already keeps job names
// short enough that this suffix stays within object-name limits).
const fqdnPolicyNameSuffix = "-fqdn"

const dnsPort = "53"

// BuildFQDNPolicy renders the CNP for one task Job: DNS to kube-dns (a `dns`
// L7 rule with `matchPattern: "*"`, which Cilium's toFQDNs machinery
// requires in order to resolve names at all), then one egress rule per
// allowlist entry — `toFQDNs` (matchName for an exact host, matchPattern for
// a "*.suffix" wildcard) or `toCIDR` for an IP/CIDR literal — scoped to that
// entry's own ports (host:port, default 80+443). It parses hosts with
// egress.Parse, so malformed entries (including a bare "*") fail closed with
// an error instead of producing an unexpectedly-open or empty policy.
//
// Pure function: it returns an object, it does not call the API. The
// caller (K8sBackend.Submit) creates it via Dynamic.Resource(...), with an
// ownerReference to the Job (jobUID) so it is garbage-collected with it.
func BuildFQDNPolicy(ns, jobName string, jobUID types.UID, hosts []string, dnsNamespace string) (*unstructured.Unstructured, error) {
	if ns == "" {
		return nil, fmt.Errorf("k8s fqdn policy: namespace is required")
	}
	if jobName == "" {
		return nil, fmt.Errorf("k8s fqdn policy: job name is required")
	}
	allow, err := egress.Parse(hosts)
	if err != nil {
		return nil, fmt.Errorf("k8s fqdn policy: %w", err)
	}
	if allow.Len() == 0 {
		return nil, fmt.Errorf("k8s fqdn policy: no allowlist hosts to enforce")
	}
	if dnsNamespace == "" {
		dnsNamespace = DefaultFQDNDNSNamespace
	}

	egressRules := make([]interface{}, 0, allow.Len()+1)
	egressRules = append(egressRules, dnsEgressRule(dnsNamespace))
	for _, r := range allow.Rules() {
		rule, err := fqdnEgressRule(r)
		if err != nil {
			return nil, err
		}
		egressRules = append(egressRules, rule)
	}

	obj := &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": CiliumNetworkPolicyGVR.Group + "/" + CiliumNetworkPolicyGVR.Version,
		"kind":       "CiliumNetworkPolicy",
		"metadata": map[string]interface{}{
			"name":      jobName + fqdnPolicyNameSuffix,
			"namespace": ns,
			"labels": map[string]interface{}{
				LabelPartOf: LabelPartOfValue,
				LabelJob:    jobName,
			},
			"ownerReferences": []interface{}{
				map[string]interface{}{
					"apiVersion":         "batch/v1",
					"kind":               "Job",
					"name":               jobName,
					"uid":                string(jobUID),
					"controller":         true,
					"blockOwnerDeletion": true,
				},
			},
		},
		"spec": map[string]interface{}{
			"endpointSelector": map[string]interface{}{
				"matchLabels": map[string]interface{}{
					LabelJob: jobName,
				},
			},
			"egress": egressRules,
		},
	}}
	return obj, nil
}

// dnsEgressRule lets the pod resolve names via kube-dns. `rules.dns` with a
// bare wildcard matchPattern is required by Cilium's FQDN implementation
// (it snoops DNS responses to learn IPs for the toFQDNs rules below) — it is
// NOT a "resolve anything" hole, since a resolved name still has to match a
// toFQDNs entry before its IP is allowed through egress.
func dnsEgressRule(dnsNamespace string) map[string]interface{} {
	return map[string]interface{}{
		"toEndpoints": []interface{}{
			map[string]interface{}{
				"matchLabels": map[string]interface{}{
					"k8s-app":                     "kube-dns",
					"io.kubernetes.pod.namespace": dnsNamespace,
				},
			},
		},
		"toPorts": []interface{}{
			map[string]interface{}{
				"ports": []interface{}{
					map[string]interface{}{"port": dnsPort, "protocol": "UDP"},
					map[string]interface{}{"port": dnsPort, "protocol": "TCP"},
				},
				"rules": map[string]interface{}{
					"dns": []interface{}{
						map[string]interface{}{"matchPattern": "*"},
					},
				},
			},
		},
	}
}

// fqdnEgressRule renders one allowlist rule's egress entry, scoped to that
// rule's own ports so a "host:8443" entry can't be used to reach the same
// host on 443/80.
func fqdnEgressRule(r egress.Rule) (map[string]interface{}, error) {
	ports := make([]interface{}, 0, len(r.Ports()))
	for _, p := range r.Ports() {
		ports = append(ports, map[string]interface{}{
			"port":     strconv.Itoa(p),
			"protocol": "TCP",
		})
	}
	rule := map[string]interface{}{
		"toPorts": []interface{}{
			map[string]interface{}{"ports": ports},
		},
	}
	switch r.Kind {
	case egress.KindHost:
		rule["toFQDNs"] = []interface{}{
			map[string]interface{}{"matchName": r.Host},
		}
	case egress.KindWildcard:
		rule["toFQDNs"] = []interface{}{
			map[string]interface{}{"matchPattern": "*." + r.Host},
		}
	case egress.KindPrefix:
		rule["toCIDR"] = []interface{}{r.Prefix.String()}
	default:
		return nil, fmt.Errorf("k8s fqdn policy: unhandled allowlist rule kind %v (%q)", r.Kind, r.Raw)
	}
	return rule, nil
}
