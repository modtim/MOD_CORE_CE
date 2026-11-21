package executor

import "testing"

// GSEC FX.16 R-023 — stage gate table: every known build-DAG STAGE
// (the API's submit.py name_to_stage map) is recognised; a missing or
// unknown STAGE fails safe to "not a build-DAG stage", so no
// API-key/token or blanket Harbor-cred injection happens for it.
func TestIsBuildDAGStage(t *testing.T) {
	cases := []struct {
		name string
		env  map[string]string
		want bool
	}{
		{"build_variant", map[string]string{"STAGE": "build_variant"}, true},
		{"smoke_variant", map[string]string{"STAGE": "smoke_variant"}, true},
		{"push_variant", map[string]string{"STAGE": "push_variant"}, true},
		{"sample_test", map[string]string{"STAGE": "sample_test"}, true},
		{"sample_test_variant", map[string]string{"STAGE": "sample_test_variant"}, true},
		{"register_node", map[string]string{"STAGE": "register_node"}, true},
		{"test_and_repair_variant", map[string]string{"STAGE": "test_and_repair_variant"}, true},
		{"ingest_artifacts", map[string]string{"STAGE": "ingest_artifacts"}, true},
		{"stage with sibling env", map[string]string{"STAGE": "push_variant", "BUILD_SPEC_ID": "42", "API_URL": "http://api:80"}, true},
		{"nil map", nil, false},
		{"missing STAGE", map[string]string{"BUILD_SPEC_ID": "42"}, false},
		{"empty STAGE", map[string]string{"STAGE": ""}, false},
		{"unknown STAGE", map[string]string{"STAGE": "not-a-stage"}, false},
		{"generate node (no STAGE env)", map[string]string{"API_URL": "http://api:80", "BUILD_SPEC_ID": "42"}, false},
		{"weights-mirror internal stages", map[string]string{"STAGE": "push"}, false},
		{"case-sensitive", map[string]string{"STAGE": "Build_Variant"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := IsBuildDAGStage(tc.env); got != tc.want {
				t.Fatalf("IsBuildDAGStage(%v) = %v, want %v", tc.env, got, tc.want)
			}
		})
	}
}
