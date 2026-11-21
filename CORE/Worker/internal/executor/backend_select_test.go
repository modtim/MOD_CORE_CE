package executor

import (
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	dynamicfake "k8s.io/client-go/dynamic/fake"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/rest"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/config"
)

// GB.EGR.4 tests exercise applyEgressFQDNMode directly (rather than going
// through the full NewBackendFromConfig -> loadK8sClient -> real kubeconfig
// path) by stubbing the three seams it calls: loadK8sRestConfig,
// ciliumCRDPresent, newDynamicClientForConfig. This keeps them hermetic and
// fast — no real kubeconfig file, no network call — while still proving the
// auto/cilium/off decision logic and its wiring onto *K8sBackend.

// fqdnSeams saves/restores the three package-var seams applyEgressFQDNMode
// depends on, so tests can stub them without leaking overrides into other
// tests (this package's tests otherwise run against the real k8sclient
// implementations, e.g. TestBackendSelectK8sValidKubeconfig).
var fqdnSeamsMu sync.Mutex

func stubFQDNSeams(t *testing.T, restCfg func(string) (*rest.Config, error), crdPresent func(*rest.Config) (bool, error), dynClient func(*rest.Config) (dynamic.Interface, error)) {
	t.Helper()
	fqdnSeamsMu.Lock()
	origRest, origCRD, origDyn := loadK8sRestConfig, ciliumCRDPresent, newDynamicClientForConfig
	fqdnSeamsMu.Unlock()

	if restCfg != nil {
		loadK8sRestConfig = restCfg
	}
	if crdPresent != nil {
		ciliumCRDPresent = crdPresent
	}
	if dynClient != nil {
		newDynamicClientForConfig = dynClient
	}
	t.Cleanup(func() {
		fqdnSeamsMu.Lock()
		defer fqdnSeamsMu.Unlock()
		loadK8sRestConfig = origRest
		ciliumCRDPresent = origCRD
		newDynamicClientForConfig = origDyn
	})
}

func fakeDynamicClient() dynamic.Interface {
	scheme := runtime.NewScheme()
	listKinds := map[schema.GroupVersionResource]string{
		CiliumNetworkPolicyGVR: "CiliumNetworkPolicyList",
	}
	return dynamicfake.NewSimpleDynamicClientWithCustomListKinds(scheme, listKinds)
}

func newFQDNTestBackend() *K8sBackend {
	return NewK8sBackend(fake.NewSimpleClientset(), "tenant-fqdn")
}

// captureLogger returns a *slog.Logger and a func reporting how many WARN
// records it received (and their last message), so tests can assert
// GB.EGR.4's "log exactly one WARN" contract.
type warnCounter struct {
	mu    sync.Mutex
	count int
	last  string
}

func (w *warnCounter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	line := string(p)
	if strings.Contains(line, "level=WARN") {
		w.count++
		w.last = line
	}
	return len(p), nil
}

func captureLogger() (*slog.Logger, *warnCounter) {
	wc := &warnCounter{}
	return slog.New(slog.NewTextHandler(wc, nil)), wc
}

func TestSelectBackendEgressFQDNOffIsNoOp(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "off")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { t.Fatal("off mode must not load a rest config"); return nil, nil },
		func(*rest.Config) (bool, error) { t.Fatal("off mode must not probe for Cilium"); return false, nil },
		func(*rest.Config) (dynamic.Interface, error) {
			t.Fatal("off mode must not build a dynamic client")
			return nil, nil
		},
	)

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "", nil); err != nil {
		t.Fatalf("off mode must never error, got %v", err)
	}
	if b.FQDNMode != FQDNModeOff {
		t.Fatalf("FQDNMode = %q, want %q", b.FQDNMode, FQDNModeOff)
	}
	if b.Dynamic != nil {
		t.Fatalf("Dynamic = %v, want nil for off mode", b.Dynamic)
	}
}

func TestSelectBackendEgressFQDNCiliumExplicitCRDPresent(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "cilium")
	dyn := fakeDynamicClient()
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return true, nil },
		func(*rest.Config) (dynamic.Interface, error) { return dyn, nil },
	)

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "/some/kubeconfig", nil); err != nil {
		t.Fatalf("explicit cilium with the CRD present must not error, got %v", err)
	}
	if b.FQDNMode != FQDNModeCilium {
		t.Fatalf("FQDNMode = %q, want %q", b.FQDNMode, FQDNModeCilium)
	}
	if b.Dynamic != dyn {
		t.Fatal("Dynamic was not set to the constructed dynamic client")
	}
}

func TestSelectBackendEgressFQDNCiliumExplicitCRDMissingErrors(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "cilium")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return false, nil }, // CRD absent
		func(*rest.Config) (dynamic.Interface, error) {
			t.Fatal("must not build a dynamic client when the CRD is missing")
			return nil, nil
		},
	)

	b := newFQDNTestBackend()
	err := applyEgressFQDNMode(b, "/some/kubeconfig", nil)
	if err == nil {
		t.Fatal("explicit cilium with the CRD missing must error at startup (fail loud, not silently unenforced)")
	}
	if b.FQDNMode == FQDNModeCilium {
		t.Fatal("FQDNMode must not be left as cilium on a failed explicit request")
	}
}

func TestSelectBackendEgressFQDNCiliumExplicitDiscoveryErrorFails(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "cilium")
	probeErr := errors.New("connection refused")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return false, probeErr },
		nil,
	)

	b := newFQDNTestBackend()
	err := applyEgressFQDNMode(b, "/some/kubeconfig", nil)
	if err == nil || !errors.Is(err, probeErr) && !strings.Contains(err.Error(), probeErr.Error()) {
		t.Fatalf("explicit cilium with a discovery error must fail startup wrapping the error, got %v", err)
	}
}

func TestSelectBackendEgressFQDNAutoWithCiliumBehavesAsCilium(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "auto")
	dyn := fakeDynamicClient()
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return true, nil },
		func(*rest.Config) (dynamic.Interface, error) { return dyn, nil },
	)
	log, warns := captureLogger()

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "/some/kubeconfig", log); err != nil {
		t.Fatalf("auto with Cilium present must not error, got %v", err)
	}
	if b.FQDNMode != FQDNModeCilium || b.Dynamic != dyn {
		t.Fatalf("auto did not adopt cilium mode: FQDNMode=%q Dynamic=%v", b.FQDNMode, b.Dynamic)
	}
	if warns.count != 0 {
		t.Fatalf("auto-with-Cilium must not warn, got %d: %s", warns.count, warns.last)
	}
}

func TestSelectBackendEgressFQDNAutoWithoutCiliumFallsBackToOffWithOneWarn(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "auto")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return false, nil }, // no Cilium, no error
		func(*rest.Config) (dynamic.Interface, error) {
			t.Fatal("must not build a dynamic client when Cilium isn't present")
			return nil, nil
		},
	)
	log, warns := captureLogger()

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "/some/kubeconfig", log); err != nil {
		t.Fatalf("auto without Cilium must fail OPEN (no error), got %v", err)
	}
	if b.FQDNMode != FQDNModeOff {
		t.Fatalf("FQDNMode = %q, want %q", b.FQDNMode, FQDNModeOff)
	}
	if b.Dynamic != nil {
		t.Fatal("Dynamic must stay nil when falling back to off")
	}
	if warns.count != 1 {
		t.Fatalf("want exactly one WARN, got %d: %s", warns.count, warns.last)
	}
	if !strings.Contains(warns.last, "Cilium not detected") {
		t.Fatalf("WARN message = %q, want it to contain %q", warns.last, "Cilium not detected")
	}
}

func TestSelectBackendEgressFQDNAutoDiscoveryErrorFallsBackToOffWithOneWarn(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "auto")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return false, errors.New("dial tcp: connection refused") },
		nil,
	)
	log, warns := captureLogger()

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "/some/kubeconfig", log); err != nil {
		t.Fatalf("auto with a discovery error must fail OPEN (no error), got %v", err)
	}
	if b.FQDNMode != FQDNModeOff {
		t.Fatalf("FQDNMode = %q, want %q", b.FQDNMode, FQDNModeOff)
	}
	if warns.count != 1 {
		t.Fatalf("want exactly one WARN, got %d: %s", warns.count, warns.last)
	}
}

func TestSelectBackendEgressFQDNAutoRestConfigErrorFallsBackToOffWithOneWarn(t *testing.T) {
	// Even a totally unresolvable kubeconfig (can't even build a *rest.Config)
	// must fail open in auto mode, not fail startup — matching how
	// TestBackendSelectK8sValidKubeconfig's loopback kubeconfig behaves for a
	// worker that never set MOD_K8S_EGRESS_FQDN at all.
	t.Setenv(k8sEgressFQDNEnvVar, "auto")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return nil, errors.New("no kubeconfig") },
		func(*rest.Config) (bool, error) {
			t.Fatal("must not probe for Cilium when the rest config itself failed")
			return false, nil
		},
		nil,
	)
	log, warns := captureLogger()

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "", log); err != nil {
		t.Fatalf("auto with a rest-config error must fail OPEN (no error), got %v", err)
	}
	if b.FQDNMode != FQDNModeOff {
		t.Fatalf("FQDNMode = %q, want %q", b.FQDNMode, FQDNModeOff)
	}
	if warns.count != 1 {
		t.Fatalf("want exactly one WARN, got %d: %s", warns.count, warns.last)
	}
}

func TestSelectBackendEgressFQDNUnknownValueErrors(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "bogus")
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { t.Fatal("unknown mode must not touch any seam"); return nil, nil },
		func(*rest.Config) (bool, error) { t.Fatal("unknown mode must not touch any seam"); return false, nil },
		func(*rest.Config) (dynamic.Interface, error) {
			t.Fatal("unknown mode must not touch any seam")
			return nil, nil
		},
	)

	b := newFQDNTestBackend()
	err := applyEgressFQDNMode(b, "", nil)
	if err == nil {
		t.Fatal("an unrecognized MOD_K8S_EGRESS_FQDN value must error at startup")
	}
	if !strings.Contains(err.Error(), "bogus") {
		t.Fatalf("error %q should mention the offending value", err)
	}
}

func TestSelectBackendEgressFQDNDefaultIsAuto(t *testing.T) {
	// MOD_K8S_EGRESS_FQDN unset entirely must behave exactly like "auto".
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return false, nil },
		nil,
	)
	log, warns := captureLogger()

	b := newFQDNTestBackend()
	if err := applyEgressFQDNMode(b, "", log); err != nil {
		t.Fatalf("default (unset) mode must behave as auto and fail open, got %v", err)
	}
	if b.FQDNMode != FQDNModeOff {
		t.Fatalf("FQDNMode = %q, want %q (auto default, no Cilium)", b.FQDNMode, FQDNModeOff)
	}
	if warns.count != 1 {
		t.Fatalf("want exactly one WARN, got %d", warns.count)
	}
}

// TestSelectBackendEgressFQDNEndToEndViaNewBackendFromConfig exercises the
// real NewBackendFromConfig entry point (not just applyEgressFQDNMode) with
// all seams — including loadK8sClient — stubbed, proving the two are wired
// together correctly without touching a real cluster or kubeconfig file.
func TestSelectBackendEgressFQDNEndToEndViaNewBackendFromConfig(t *testing.T) {
	t.Setenv(k8sEgressFQDNEnvVar, "cilium")
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	dyn := fakeDynamicClient()
	stubFQDNSeams(t,
		func(string) (*rest.Config, error) { return &rest.Config{}, nil },
		func(*rest.Config) (bool, error) { return true, nil },
		func(*rest.Config) (dynamic.Interface, error) { return dyn, nil },
	)

	origLoadClient := loadK8sClient
	loadK8sClient = func(string) (kubernetes.Interface, error) { return fake.NewSimpleClientset(), nil }
	t.Cleanup(func() { loadK8sClient = origLoadClient })

	cfg := &config.Config{ExecutorBackend: config.BackendK8s, KubeconfigPath: "/irrelevant"}
	backend, err := NewBackendFromConfig(cfg, nil, nil)
	if err != nil {
		t.Fatalf("NewBackendFromConfig: %v", err)
	}
	kb, ok := backend.(*K8sBackend)
	if !ok {
		t.Fatalf("want *K8sBackend, got %T", backend)
	}
	if kb.FQDNMode != FQDNModeCilium || kb.Dynamic != dyn {
		t.Fatalf("FQDNMode/Dynamic not applied to the backend returned by NewBackendFromConfig: %q %v", kb.FQDNMode, kb.Dynamic)
	}
}
