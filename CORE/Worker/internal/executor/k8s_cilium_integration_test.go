//go:build k8sintegration

// GB.EGR.8a — live Cilium FQDN-egress enforcement test. Runs ONLY against a
// cluster where Cilium is installed as the CNI (run-integration.sh's
// MOD_IT_CILIUM=1 branch builds a kind cluster with disableDefaultCNI: true,
// runs `cilium install --wait`, then this suite). Submits two EgressAllowlist
// jobs (allowlist = ["example.com"]): example.com must succeed,
// www.google.com must be denied by the per-task CiliumNetworkPolicy.
//
// Skips (never fails) unless MOD_IT_CILIUM=1 is exported.
package executor

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"os"
	"testing"
)

const itCiliumImage = "curlimages/curl:8.8.1" // ENTRYPOINT is curl; Cmd are its args

func TestK8sIntegrationCilium(t *testing.T) {
	if os.Getenv("MOD_IT_CILIUM") != "1" {
		t.Skip("MOD_IT_CILIUM != 1 — the Cilium FQDN-egress suite only runs against the kind+Cilium cluster that run-integration.sh MOD_IT_CILIUM=1 builds")
	}

	cs, ns := itCluster(t)

	restCfg, err := loadK8sRestConfig("")
	if err != nil {
		t.Fatalf("loading rest config: %v", err)
	}
	present, err := ciliumCRDPresent(restCfg)
	if err != nil || !present {
		t.Fatalf("Cilium CRD %s not present (present=%v err=%v) — the cluster must run Cilium (run-integration.sh MOD_IT_CILIUM=1 installs it)", CiliumNetworkPolicyGVR.Resource, present, err)
	}
	dyn, err := newDynamicClientForConfig(restCfg)
	if err != nil {
		t.Fatalf("building dynamic client: %v", err)
	}

	b := NewK8sBackend(cs, ns)
	b.FQDNMode = FQDNModeCilium
	b.Dynamic = dyn

	suffix := func() string { x := make([]byte, 3); _, _ = rand.Read(x); return hex.EncodeToString(x) }
	submit := func(url string) *Handle {
		spec := &TaskSpec{
			Image:       itCiliumImage,
			Env:         map[string]string{"MOD_TASK_ID": "it-cilium-" + suffix()},
			Cmd:         []string{"-sS", "-m", "15", url}, // curl -sS -m 15 <url>
			Egress:      EgressAllowlist,
			EgressHosts: []string{"example.com"},
		}
		h, err := b.Submit(context.Background(), spec)
		if err != nil {
			t.Fatalf("Submit(%s): %v", url, err)
		}
		t.Cleanup(func() { _ = b.Cancel(context.Background(), h) })
		return h
	}

	t.Run("AllowlistedHostSucceeds", func(t *testing.T) {
		st := itWaitDone(t, b, submit("https://example.com"))
		if st.Phase != TaskSucceeded || st.ExitCode != 0 {
			t.Fatalf("want succeeded/0, got %+v", st)
		}
	})

	t.Run("NonAllowlistedHostDenied", func(t *testing.T) {
		st := itWaitDone(t, b, submit("https://www.google.com"))
		if st.Phase != TaskFailed || st.ExitCode == 0 {
			t.Fatalf("want failed/non-zero (egress denied by Cilium), got %+v", st)
		}
	})
}
