package executor

import (
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// Golden strings captured from the pre-W17 code (executor.go uploadSidecar
// and k8s_staging.go buildUploadCmd produced byte-identical scripts). Any
// change here changes what both the Docker and K8s upload sidecars run.
const (
	goldenUploadPlain = `CAP=0 && if [ "$CAP" -gt 0 ]; then   total=0;   for f in /data/output/*; do     [ -f "$f" ] || continue;     sz=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f");     total=$((total + sz));   done;   if [ "$total" -gt "$CAP" ]; then     echo "SANDBOX_OUTPUT_TOO_LARGE total=$total cap=$CAP" >&2;     exit 64;   fi; fi && for file in /data/output/*; do   [ -f "$file" ] || continue;   hash=$(sha256sum "$file" | cut -d' ' -f1);   fname=$(basename "$file");   ext="${fname##*.}";   size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file");   mc cp "$file" myminio/tenant-1-files/$hash &&   echo "UPLOAD,$hash,$fname,$size,$ext"; done`

	goldenUploadWizard = `CAP=5242880 && if [ "$CAP" -gt 0 ]; then   total=0;   for f in /data/output/*; do     [ -f "$f" ] || continue;     sz=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f");     total=$((total + sz));   done;   if [ "$total" -gt "$CAP" ]; then     echo "SANDBOX_OUTPUT_TOO_LARGE total=$total cap=$CAP" >&2;     exit 64;   fi; fi && for file in /data/output/*; do   [ -f "$file" ] || continue;   hash=$(sha256sum "$file" | cut -d' ' -f1);   fname=$(basename "$file");   ext="${fname##*.}";   size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file");   mc cp "$file" myminio/tenant-7-files/_wizard_test_runs/s1/2/$hash &&   echo "UPLOAD,_wizard_test_runs/s1/2/$hash,$fname,$size,$ext"; done`
)

var goldenUploadCases = []struct {
	name string
	task *transport.Task
	want string
}{
	{"plain", &transport.Task{Payload: transport.TaskPayload{MinioBucket: "tenant-1-files"}}, goldenUploadPlain},
	{"wizard", &transport.Task{Payload: transport.TaskPayload{
		MinioBucket: "tenant-7-files", Purpose: "wizard_test_run",
		OutputPrefix: "_wizard_test_runs/s1/2/", SandboxMaxOutputMB: 5,
	}}, goldenUploadWizard},
}

func TestUploadScriptGolden_K8s(t *testing.T) {
	for _, c := range goldenUploadCases {
		if got := buildUploadCmd(c.task); got != c.want {
			t.Errorf("%s: k8s upload script drifted\n got: %s\nwant: %s", c.name, got, c.want)
		}
	}
}

// Docker path: uploadSidecar runs buildUploadScript(uploadScriptParamsFromTask(task)).
func TestUploadScriptGolden_Docker(t *testing.T) {
	for _, c := range goldenUploadCases {
		if got := buildUploadScript(uploadScriptParamsFromTask(c.task)); got != c.want {
			t.Errorf("%s: docker upload script drifted\n got: %s\nwant: %s", c.name, got, c.want)
		}
	}
}

func TestUploadScriptParams_PrefixOnlyForWizard(t *testing.T) {
	p := uploadScriptParamsFromTask(&transport.Task{Payload: transport.TaskPayload{
		MinioBucket: "b", OutputPrefix: "leak/", SandboxMaxOutputMB: 9,
	}})
	if p.Prefix != "" || p.CapBytes != 0 {
		t.Fatalf("non-wizard task must ignore OutputPrefix/cap, got %+v", p)
	}
	p = uploadScriptParamsFromTask(&transport.Task{Payload: transport.TaskPayload{MinioBucket: "b", Purpose: "wizard_test_run"}})
	if p.Prefix != "" || p.CapBytes != 100*1024*1024 {
		t.Fatalf("wizard default cap 100MB, empty prefix; got %+v", p)
	}
}
