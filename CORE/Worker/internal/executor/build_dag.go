package executor

// GSEC FX.16 R-023 — worker-side follow-up to the API's per-stage
// build-credential scoping (CORE/API/node_builder/build_dag/submit.py).

// buildDAGStages is the set of STAGE env values the API's build-DAG
// submit emits (the name_to_stage map in
// CORE/API/node_builder/build_dag/submit.py). A task whose
// container_env carries one of these runs the build-task image and is
// the stage that owns its credentials:
//
//   - API access: every stage calls back into the API (fetch_build_spec
//     / report_back), so every stage receives the worker's scoped,
//     per-task build token as API_KEY — never a long-lived key.
//   - Registry creds: they arrive via claim-time secret_refs (platform
//     Harbor PULL refs for the docker-build stages, a 1-day repo-scoped
//     push robot for the push/smoke/sample-test stages), so the
//     durable tenant robot from the /credentials grant must NOT be
//     blanket-injected for these tasks.
//
// The wizard-generate node (Containerizer) has NO STAGE env and is
// deliberately excluded: it writes artifacts for the API's
// ingest-artifacts stage to POST and never calls back into the API.
var buildDAGStages = map[string]bool{
	"build_variant":           true,
	"smoke_variant":           true,
	"push_variant":            true,
	"sample_test":             true,
	"sample_test_variant":     true,
	"register_node":           true,
	"test_and_repair_variant": true,
	"ingest_artifacts":        true,
}

// IsBuildDAGStage reports whether the task's container_env carries a
// known build-DAG STAGE. Fail-safe: a missing or unknown STAGE is NOT
// treated as a build-DAG stage — a future or renamed stage gets no
// API-key/token injection and no blanket Harbor-cred injection until
// it is added here (and to the API's submit map).
func IsBuildDAGStage(containerEnv map[string]string) bool {
	stage, ok := containerEnv["STAGE"]
	if !ok || stage == "" {
		return false
	}
	return buildDAGStages[stage]
}
