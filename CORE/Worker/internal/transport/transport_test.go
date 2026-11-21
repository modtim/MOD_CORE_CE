package transport

import (
	"encoding/json"
	"testing"
)

func TestTaskPayload_JSONRoundTrip(t *testing.T) {
	original := TaskPayload{
		ProcessingImage:    "albedo:latest",
		SourceFileLocation: "abc123def456",
		FileExtension:      "png",
		MinioBucket:        "tenant-1-files",
		NodeKind:              "albedo",
		DisplayName:            "Albedo Generator",
		GPURequired:        true,
		ContainerTimeout:   3600,
		GPUVariants: map[string]string{
			"cuda": "albedo:latest",
			"rocm": "albedo:latest-rocm",
			"cpu":  "albedo:latest-cpu",
		},
		SecretRefs: []SecretRef{
			{SecretName: "api_key", EnvVar: "API_KEY"},
		},
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}

	var decoded TaskPayload
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}

	if decoded.ProcessingImage != original.ProcessingImage {
		t.Errorf("ProcessingImage: got %s, want %s", decoded.ProcessingImage, original.ProcessingImage)
	}
	if decoded.NodeKind != original.NodeKind {
		t.Errorf("NodeKind: got %s, want %s", decoded.NodeKind, original.NodeKind)
	}
	if decoded.GPURequired != original.GPURequired {
		t.Errorf("GPURequired: got %v, want %v", decoded.GPURequired, original.GPURequired)
	}
	if len(decoded.GPUVariants) != 3 {
		t.Errorf("GPUVariants: got %d entries, want 3", len(decoded.GPUVariants))
	}
	if decoded.GPUVariants["rocm"] != "albedo:latest-rocm" {
		t.Errorf("GPUVariants[rocm]: got %s, want albedo:latest-rocm", decoded.GPUVariants["rocm"])
	}
	if len(decoded.SecretRefs) != 1 {
		t.Errorf("SecretRefs: got %d, want 1", len(decoded.SecretRefs))
	}
	if decoded.SecretRefs[0].EnvVar != "API_KEY" {
		t.Errorf("SecretRefs[0].EnvVar: got %s, want API_KEY", decoded.SecretRefs[0].EnvVar)
	}
}

func TestTaskPayload_GPUVariantsOmittedWhenNil(t *testing.T) {
	payload := TaskPayload{
		ProcessingImage: "test:latest",
		NodeKind:           "test",
	}

	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}

	// gpu_variants should not appear in JSON when nil (omitempty)
	var raw map[string]interface{}
	json.Unmarshal(data, &raw)
	if _, exists := raw["gpu_variants"]; exists {
		t.Error("gpu_variants should be omitted when nil")
	}
}

func TestTaskPayload_GPUVariantsEmptyMap(t *testing.T) {
	payload := TaskPayload{
		ProcessingImage: "test:latest",
		GPUVariants:     map[string]string{},
	}

	data, _ := json.Marshal(payload)
	var raw map[string]interface{}
	json.Unmarshal(data, &raw)

	// Empty map should still be omitted (omitempty treats empty map as zero value)
	// Actually in Go, empty map IS serialized. Let's verify behavior.
	if variants, exists := raw["gpu_variants"]; exists {
		m, ok := variants.(map[string]interface{})
		if !ok || len(m) != 0 {
			t.Errorf("expected empty map, got %v", variants)
		}
	}
}

func TestTaskPayload_SecretRefsOmittedWhenNil(t *testing.T) {
	payload := TaskPayload{
		ProcessingImage: "test:latest",
	}

	data, _ := json.Marshal(payload)
	var raw map[string]interface{}
	json.Unmarshal(data, &raw)

	if _, exists := raw["secret_refs"]; exists {
		t.Error("secret_refs should be omitted when nil")
	}
}

func TestTaskPayload_CmdRoundTrip(t *testing.T) {
	original := TaskPayload{
		ProcessingImage: "ghcr.io/modtim/node:latest-intel",
		Cmd:             []string{"python", "/opt/mod_smoke/probe.py"},
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}

	var decoded TaskPayload
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}

	if len(decoded.Cmd) != 2 {
		t.Fatalf("Cmd: got %d entries, want 2", len(decoded.Cmd))
	}
	if decoded.Cmd[0] != "python" || decoded.Cmd[1] != "/opt/mod_smoke/probe.py" {
		t.Errorf("Cmd content mismatch: %v", decoded.Cmd)
	}
}

func TestTaskPayload_CmdOmittedWhenNil(t *testing.T) {
	payload := TaskPayload{ProcessingImage: "test:latest"}

	data, _ := json.Marshal(payload)
	var raw map[string]interface{}
	json.Unmarshal(data, &raw)

	if _, exists := raw["cmd"]; exists {
		t.Error("cmd should be omitted when nil (user workflow tasks must not carry it)")
	}
}

func TestTaskPayload_DeserializeFromAPI(t *testing.T) {
	// Simulate JSON from the API's task_payload
	apiJSON := `{
		"processing_image": "ghcr.io/modtim/albedo:latest",
		"source_file_location": "deadbeef1234567890",
		"file_extension": "jpg",
		"minio_bucket": "tenant-42-files",
		"node_kind": "albedo",
		"gpu_required": true,
		"container_timeout": 7200,
		"gpu_variants": {
			"cuda": "ghcr.io/modtim/albedo:latest",
			"rocm": "ghcr.io/modtim/albedo:latest-rocm"
		}
	}`

	var payload TaskPayload
	if err := json.Unmarshal([]byte(apiJSON), &payload); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}

	if payload.ProcessingImage != "ghcr.io/modtim/albedo:latest" {
		t.Errorf("ProcessingImage wrong: %s", payload.ProcessingImage)
	}
	if payload.ContainerTimeout != 7200 {
		t.Errorf("ContainerTimeout: got %d, want 7200", payload.ContainerTimeout)
	}
	if payload.GPUVariants["rocm"] != "ghcr.io/modtim/albedo:latest-rocm" {
		t.Errorf("GPUVariants[rocm] wrong: %s", payload.GPUVariants["rocm"])
	}
}

func TestTaskPayloadEgressModeRoundTrip(t *testing.T) {
	// 1) Marshal/unmarshal a payload with all 3 egress-mode fields set → equal.
	original := TaskPayload{
		ProcessingImage:     "ghcr.io/modtim/albedo:latest",
		EgressMode:          "mediated",
		EgressUpstream:      "10.0.1.5:8443",
		EgressUpstreamToken: "tok_m_abc123",
		NodeKind:            "albedo",
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}

	var decoded TaskPayload
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}

	if decoded.EgressMode != "mediated" {
		t.Errorf("EgressMode: got %q, want %q", decoded.EgressMode, "mediated")
	}
	if decoded.EgressUpstream != "10.0.1.5:8443" {
		t.Errorf("EgressUpstream: got %q, want %q", decoded.EgressUpstream, "10.0.1.5:8443")
	}
	if decoded.EgressUpstreamToken != "tok_m_abc123" {
		t.Errorf("EgressUpstreamToken: got %q, want %q", decoded.EgressUpstreamToken, "tok_m_abc123")
	}

	// 2) Verify re-marshalled JSON contains the values.
	data2, err := json.Marshal(decoded)
	if err != nil {
		t.Fatalf("re-marshal error: %v", err)
	}
	if string(data) != string(data2) {
		t.Errorf("re-marshalled JSON differs:\n  first:  %s\n  second: %s", string(data), string(data2))
	}

	// 3) A payload with zero egress-mode fields → empty strings on decode,
	//    and omitempty keeps them out of the JSON.
	plain := TaskPayload{
		ProcessingImage:    "alpine:latest",
		NodeKind:           "probe",
	}

	plainData, err := json.Marshal(plain)
	if err != nil {
		t.Fatalf("marshal plain error: %v", err)
	}

	var plainMap map[string]interface{}
	if err := json.Unmarshal(plainData, &plainMap); err != nil {
		t.Fatalf("unmarshal plain error: %v", err)
	}

	for _, f := range []string{"egress_mode", "egress_upstream", "egress_upstream_token"} {
		if _, exists := plainMap[f]; exists {
			t.Errorf("field %q should be omitted when empty (omitempty), but was present", f)
		}
	}

	// 4) Unmarshal JSON that omits the egress-mode keys → empty strings.
	apiJSON := `{
		"processing_image": "test:v1",
		"node_kind": "test"
	}`
	var fromAPI TaskPayload
	if err := json.Unmarshal([]byte(apiJSON), &fromAPI); err != nil {
		t.Fatalf("unmarshal from API error: %v", err)
	}
	if fromAPI.EgressMode != "" {
		t.Errorf("EgressMode absent in JSON: got %q, want empty", fromAPI.EgressMode)
	}
	if fromAPI.EgressUpstream != "" {
		t.Errorf("EgressUpstream absent in JSON: got %q, want empty", fromAPI.EgressUpstream)
	}
	if fromAPI.EgressUpstreamToken != "" {
		t.Errorf("EgressUpstreamToken absent in JSON: got %q, want empty", fromAPI.EgressUpstreamToken)
	}
}
