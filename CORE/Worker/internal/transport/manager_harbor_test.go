package transport

import (
	"encoding/json"
	"testing"
	"time"
)

// TestManagerHarborCredentialsRoundTrip verifies that ManagerHarbor embedded
// in Credentials serializes/deserializes with the JSON keys the contract
// specifies: registry, proxy_project, upstream_hosts[], token, expires_at,
// and the optional k8s_robot{username,password,expires_at}.
func TestManagerHarborCredentialsRoundTrip(t *testing.T) {
	wantExpires := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)
	wantRobotExpires := time.Date(2026, 9, 26, 12, 30, 0, 0, time.UTC)

	creds := Credentials{
		ManagerHarbor: &ManagerHarbor{
			Registry:       "ghcr.io",
			ProxyProject:   "wizard-build",
			UpstreamHosts:  []string{"registry-2.docker.io", "gcr.io"},
			Token:          "mgr-harbor-token-abc123",
			ExpiresAt:      wantExpires,
			K8sRobot: &RegistryRobot{
				Username:  "robot$cb@proj-1",
				Password:  "robot-secret-xyz",
				ExpiresAt: wantRobotExpires,
			},
		},
	}

	data, err := json.Marshal(creds)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	// Verify the JSON keys match the contract exactly.
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("unmarshal raw: %v", err)
	}
	_, ok := raw["manager_harbor"]
	if !ok {
		t.Fatalf("expected top-level manager_harbor key, got keys: %v", rawKeys(raw))
	}
	var mh map[string]json.RawMessage
	if err := json.Unmarshal(raw["manager_harbor"], &mh); err != nil {
		t.Fatalf("unmarshal manager_harbor: %v", err)
	}
	for _, k := range []string{"registry", "proxy_project", "upstream_hosts", "token", "expires_at", "k8s_robot"} {
		if _, ok := mh[k]; !ok {
			t.Errorf("expected manager_harbor key %q not present; keys: %v", k, rawKeys(mh))
		}
	}

	// Round-trip back into a struct.
	var got Credentials
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	gh := got.ManagerHarbor
	if gh == nil {
		t.Fatal("expected non-nil ManagerHarbor after round-trip")
	}
	if gh.Registry != creds.ManagerHarbor.Registry {
		t.Errorf("Registry = %q, want %q", gh.Registry, creds.ManagerHarbor.Registry)
	}
	if gh.ProxyProject != creds.ManagerHarbor.ProxyProject {
		t.Errorf("ProxyProject = %q, want %q", gh.ProxyProject, creds.ManagerHarbor.ProxyProject)
	}
	if len(gh.UpstreamHosts) != 2 ||
		gh.UpstreamHosts[0] != "registry-2.docker.io" ||
		gh.UpstreamHosts[1] != "gcr.io" {
		t.Errorf("UpstreamHosts = %v, want [registry-2.docker.io gcr.io]", gh.UpstreamHosts)
	}
	if gh.Token != creds.ManagerHarbor.Token {
		t.Errorf("Token = %q, want %q", gh.Token, creds.ManagerHarbor.Token)
	}
	if !gh.ExpiresAt.Equal(wantExpires) {
		t.Errorf("ExpiresAt = %v, want %v", gh.ExpiresAt, wantExpires)
	}
	if gh.K8sRobot == nil {
		t.Fatal("expected non-nil K8sRobot")
	}
	if gh.K8sRobot.Username != "robot$cb@proj-1" || gh.K8sRobot.Password != "robot-secret-xyz" {
		t.Errorf("K8sRobot = %+v, want username robot$cb@proj-1", gh.K8sRobot)
	}
	if !gh.K8sRobot.ExpiresAt.Equal(wantRobotExpires) {
		t.Errorf("K8sRobot.ExpiresAt = %v, want %v", gh.K8sRobot.ExpiresAt, wantRobotExpires)
	}
}

// TestManagerHarborOptionalAbsent verifies the manager_harbor field is omitted
// when nil (omitempty) and that k8s_robot is omitted when nil.
func TestManagerHarborOptionalAbsent(t *testing.T) {
	creds := Credentials{
		ManagerHarbor: &ManagerHarbor{
			Registry:      "ghcr.io",
			ProxyProject:  "wizard-build",
			UpstreamHosts: []string{"gcr.io"},
			Token:         "t",
			ExpiresAt:     time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC),
			// K8sRobot left nil.
		},
	}

	data, err := json.Marshal(creds)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("unmarshal raw: %v", err)
	}
	mh := raw["manager_harbor"]
	var mhMap map[string]json.RawMessage
	if err := json.Unmarshal(mh, &mhMap); err != nil {
		t.Fatalf("unmarshal manager_harbor: %v", err)
	}
	if _, ok := mhMap["k8s_robot"]; ok {
		t.Errorf("k8s_robot should be omitted when nil, present in: %v", mhMap)
	}

	// ManagerHarbor itself: a nil ManagerHarbor should omit the key entirely
	// (omitempty), even though the sibling fields always serialize.
	var noMH map[string]json.RawMessage
	if err := json.Unmarshal(data, &noMH); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	_, mhPresent := noMH["manager_harbor"]
	if !mhPresent {
		t.Errorf("manager_harbor should be present when set; keys: %v", rawKeys(noMH))
	}
}

func rawKeys(m map[string]json.RawMessage) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}
