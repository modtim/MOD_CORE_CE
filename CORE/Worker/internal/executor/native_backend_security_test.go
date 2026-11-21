package executor

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/tunables"
)

// GSEC FX.17 / R-069 — refuse tenant code on the native backend unless the
// worker tunable explicitly allows it; platform/system tasks (TenantID 0)
// always pass.

func tenantNativeSpec(taskID string, tenantID int) *TaskSpec {
	return &TaskSpec{
		TenantID:      tenantID,
		NativeCommand: "true",
		Env:           map[string]string{"MOD_TASK_ID": taskID},
	}
}

func TestNativeTenantGate_PlatformTaskAllowed(t *testing.T) {
	nb := NewNativeBackend(t.TempDir())
	h, err := nb.Submit(context.Background(), tenantNativeSpec("plat-1", 0))
	if err != nil {
		t.Fatalf("platform (TenantID=0) task must run on the native backend, got %v", err)
	}
	_ = nb.Cancel(context.Background(), h)
}

func TestNativeTenantGate_TenantRefusedByDefault(t *testing.T) {
	nb := NewNativeBackend(t.TempDir())
	_, err := nb.Submit(context.Background(), tenantNativeSpec("ten-1", 7))
	if !errors.Is(err, ErrNativeTenantRefused) {
		t.Fatalf("tenant task must be refused without the tunable, got %v", err)
	}
	entries, _ := filepath.Glob(filepath.Join(nb.ScratchRoot, "*"))
	if len(entries) != 0 {
		t.Errorf("a refused tenant task must not leave scratch dirs behind: %v", entries)
	}
}

func TestNativeTenantGate_TenantAllowedByTunable(t *testing.T) {
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeAllowUnsandboxedTenantCodeKey: true})
	nb := NewNativeBackend(t.TempDir())
	nb.SetTunables(tun)
	h, err := nb.Submit(context.Background(), tenantNativeSpec("ten-2", 42))
	if err != nil {
		t.Fatalf("tenant task must run when the tunable allows it, got %v", err)
	}
	_ = nb.Cancel(context.Background(), h)
}

func TestNativeTenantGate_TunableFalseStillRefused(t *testing.T) {
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeAllowUnsandboxedTenantCodeKey: false})
	nb := NewNativeBackend(t.TempDir())
	nb.SetTunables(tun)
	if _, err := nb.Submit(context.Background(), tenantNativeSpec("ten-3", 42)); !errors.Is(err, ErrNativeTenantRefused) {
		t.Fatalf("explicit-false tunable must still refuse tenant code, got %v", err)
	}
}

func TestNativeTenantGate_StringTunableValue(t *testing.T) {
	// The platform_settings registry may push the value as a string.
	tun := tunables.New()
	tun.Apply(map[string]interface{}{nativeAllowUnsandboxedTenantCodeKey: "true"})
	nb := NewNativeBackend(t.TempDir())
	nb.SetTunables(tun)
	h, err := nb.Submit(context.Background(), tenantNativeSpec("ten-4", 99))
	if err != nil {
		t.Fatalf("string 'true' tunable must allow tenant code, got %v", err)
	}
	_ = nb.Cancel(context.Background(), h)
}

func TestNativeTenantGate_DecisionTable(t *testing.T) {
	cases := []struct {
		name     string
		tenantID int
		tun      *tunables.Store
		refuse   bool
	}{
		{"platform task, no store", 0, nil, false},
		{"platform task, empty store", 0, tunables.New(), false},
		{"tenant task, no store", 5, nil, true},
		{"tenant task, empty store", 5, tunables.New(), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			nb := NewNativeBackend(t.TempDir())
			nb.SetTunables(tc.tun)
			err := nb.tenantCodeGate(tenantNativeSpec("x", tc.tenantID))
			if tc.refuse {
				if !errors.Is(err, ErrNativeTenantRefused) {
					t.Fatalf("expected ErrNativeTenantRefused, got %v", err)
				}
			} else if err != nil {
				t.Fatalf("expected nil, got %v", err)
			}
		})
	}
}

// refCountingStager fails the test if inputs are staged for a task the gate
// will refuse — a refused tenant task must not burn staging work or leave
// bytes in the scratch tree.
type refCountingStager struct {
	staged bool
}

func (s *refCountingStager) StageInputs(_ context.Context, _ *transport.Task, _ *transport.Credentials, _ string) error {
	s.staged = true
	return nil
}

func (s *refCountingStager) CollectOutputs(_ context.Context, _ *transport.Task, _ *transport.Credentials, _ string) ([]transport.OutputFile, error) {
	return nil, nil
}

func TestNativeTenantGate_SubmitStagedRefusesBeforeStaging(t *testing.T) {
	stager := &refCountingStager{}
	nb := NewNativeBackend(t.TempDir())
	nb.SetStager(stager)
	task := &transport.Task{ID: 100, TenantID: 12}
	_, err := nb.SubmitStaged(context.Background(), tenantNativeSpec("100", 0), task, nil)
	if !errors.Is(err, ErrNativeTenantRefused) {
		t.Fatalf("SubmitStaged must refuse tenant tasks (task.TenantID=12), got %v", err)
	}
	if stager.staged {
		t.Fatal("inputs must not be staged for a refused tenant task")
	}
}

func TestNativeTenantGate_SubmitStagedPlatformStillRuns(t *testing.T) {
	stager := &refCountingStager{}
	nb := NewNativeBackend(t.TempDir())
	nb.SetStager(stager)
	task := &transport.Task{ID: 101, TenantID: 0}
	h, err := nb.SubmitStaged(context.Background(), tenantNativeSpec("101", 0), task, nil)
	if err != nil {
		t.Fatalf("platform task must run through SubmitStaged, got %v", err)
	}
	_ = nb.Cancel(context.Background(), h)
	if !stager.staged {
		t.Error("inputs should have been staged for the allowed platform task")
	}
}
