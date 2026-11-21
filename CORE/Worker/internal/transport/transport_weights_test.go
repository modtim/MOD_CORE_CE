package transport

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestTaskPayloadWeightsRoundTrip(t *testing.T) {
	in := TaskPayload{
		StackOp: "deploy",
		Weights: []WeightRef{
			{
				ArtifactRef: "artifact-abc",
				RepoID:      "org/model",
				Revision:    "rev-1",
				Files: []WeightFile{
					{Path: "model.safetensors", SHA256: "deadbeef", Size: 12345},
					{Path: "config.json", SHA256: "cafebabe", Size: 42},
				},
			},
		},
	}
	b, err := json.Marshal(in)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out TaskPayload
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("round-trip mismatch:\n in=%+v\n out=%+v", in, out)
	}
	// The JSON shape must match the API-side Ref/File layout exactly.
	want := `{
		"weights": [
			{
				"artifact_ref": "artifact-abc",
				"repo_id": "org/model",
				"revision": "rev-1",
				"files": [
					{"path": "model.safetensors", "sha256": "deadbeef", "size": 12345},
					{"path": "config.json", "sha256": "cafebabe", "size": 42}
				]
			}
		]
	}`
	var got map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("unmarshal to map: %v", err)
	}
	var wantMap map[string]any
	if err := json.Unmarshal([]byte(want), &wantMap); err != nil {
		t.Fatalf("parse want: %v", err)
	}
	if !reflect.DeepEqual(got["weights"], wantMap["weights"]) {
		t.Fatalf("weights JSON shape mismatch:\n got=%v\n want=%v", got["weights"], wantMap["weights"])
	}
}

func TestTaskPayloadWeightsOmitted(t *testing.T) {
	b, err := json.Marshal(TaskPayload{StackOp: "deploy"})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if _, ok := m["weights"]; ok {
		t.Fatalf("weights key should be omitted when empty, got %v", m["weights"])
	}
}
