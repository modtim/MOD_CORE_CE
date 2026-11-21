package heartbeat

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// GSEC Q21.8: the optional heartbeat `vault` object and the degraded-boot
// warning on the `warnings` field. Reuses newTestHeartbeat from tunables_test.

func TestSendAPI_VaultDeliveryDispatched(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"vault": map[string]interface{}{
				"status":                      "per_worker",
				"role_id":                     "worker-7",
				"secret_id":                   "new-sid",
				"prev_secret_id_valid_until":  "2026-11-01T00:00:00Z",
			},
		})
	}))
	defer srv.Close()

	h := newTestHeartbeat(srv)
	var got *VaultDelivery
	h.OnVaultDelivery(func(d *VaultDelivery) { got = d })

	if _, _, err := h.sendAPI(context.Background(), map[string]interface{}{"worker_name": "test-worker"}); err != nil {
		t.Fatalf("sendAPI: %v", err)
	}
	if got == nil {
		t.Fatal("OnVaultDelivery must receive the delivered object")
	}
	if got.SecretID != "new-sid" || got.PrevSecretIDValidUntil == "" {
		t.Errorf("delivery = %#v, want secret_id + prev validity carried through", got)
	}
}

func TestSendAPI_AbsentVaultDispatchesNil(t *testing.T) {
	// Setting off (masked-status-only or older manager): no `vault` key at all.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"status": "ok"})
	}))
	defer srv.Close()

	h := newTestHeartbeat(srv)
	called := false
	var got *VaultDelivery
	h.OnVaultDelivery(func(d *VaultDelivery) { called = true; got = d })

	if _, _, err := h.sendAPI(context.Background(), map[string]interface{}{"worker_name": "test-worker"}); err != nil {
		t.Fatalf("sendAPI: %v", err)
	}
	if !called || got != nil {
		t.Errorf("absent vault object must dispatch nil, called=%v got=%#v", called, got)
	}
}

func TestApplyWarningsCarriesVaultWarning(t *testing.T) {
	h := &Heartbeat{log: discardLog()}
	h.OnVaultWarnings(func() string { return "vault: login failed, running degraded" })

	data := map[string]interface{}{}
	h.applyWarnings(data)
	w, ok := data["warnings"].([]string)
	if !ok || len(w) != 1 || w[0] != "vault: login failed, running degraded" {
		t.Errorf("warnings = %#v, want the vault warning", data["warnings"])
	}
}

func TestApplyWarningsMergesCredsAndVault(t *testing.T) {
	h := &Heartbeat{log: discardLog()}
	h.OnCredsWarnings(func() []string { return []string{"creds: task 5 refresh failed"} })
	h.OnVaultWarnings(func() string { return "vault: degraded" })

	data := map[string]interface{}{}
	h.applyWarnings(data)
	w, ok := data["warnings"].([]string)
	if !ok || len(w) != 2 || w[0] != "creds: task 5 refresh failed" || w[1] != "vault: degraded" {
		t.Errorf("warnings = %#v, want creds then vault", data["warnings"])
	}
}

func TestApplyWarningsOmitsFieldWhenVaultHealthy(t *testing.T) {
	h := &Heartbeat{log: discardLog()}
	h.OnVaultWarnings(func() string { return "" })

	data := map[string]interface{}{}
	h.applyWarnings(data)
	if _, present := data["warnings"]; present {
		t.Errorf("healthy worker must not create a warnings field, got %#v", data["warnings"])
	}
}
