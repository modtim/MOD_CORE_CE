package executor

// EXPR 9.3 slice 3. The reaper's resume stamp must reach the node container as
// env, and the property every existing node depends on is that a task with NO
// stamp gets byte-identical env to the pre-EXPR world. Pure and table-driven:
// resumeCheckpointEnv touches nothing but its argument, so no Docker, no daemon.

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestResumeCheckpointEnvNoStampIsNil(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	cases := []transport.TaskPayload{
		transport.TaskPayload{},
		transport.TaskPayload{ResumeCheckpointID: 0},
		// A negative id is a corrupt stamp, not a resume point. Half-stamping a
		// node that has no idea what it is resuming is worse than stamping
		// nothing, so the whole block is dropped.
		transport.TaskPayload{ResumeCheckpointID: -7, ResumeCheckpointLabel: "epoch-3"},
	}
	for i := 0; i < len(cases); i++ {
		got := e.resumeCheckpointEnv(cases[i])
		if got != nil {
			t.Errorf("unstamped task %d produced env %v; a node that never asked for a resume point must see the env it saw before EXPR 9.3", i, got)
		}
	}
	if logBuf.String() != "" {
		t.Error("an unstamped task logged something; the stamp path must be silent when there is nothing to stamp")
	}
}

func TestResumeCheckpointEnvIDOnly(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	got := e.resumeCheckpointEnv(transport.TaskPayload{ResumeCheckpointID: 42})
	if len(got) != 1 || got[0] != "MOD_RESUME_CHECKPOINT_ID=42" {
		t.Errorf("id-only stamp produced %v; expected exactly MOD_RESUME_CHECKPOINT_ID=42", got)
	}
	if logBuf.String() != "" {
		t.Error("a stamp with no payload logged something; nothing was skipped here")
	}
}

func TestResumeCheckpointEnvLabelAndProgress(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	// 0.0 is a REAL progress value: the node restarted at the very beginning of
	// a step it had already checkpointed. It must not be swallowed by a truthy
	// check, which is why the field is a pointer.
	zero := 0.0
	got := e.resumeCheckpointEnv(transport.TaskPayload{
		ResumeCheckpointID:       42,
		ResumeCheckpointLabel:    "epoch-7",
		ResumeCheckpointProgress: &zero,
	})
	want := []string{"MOD_RESUME_CHECKPOINT_ID=42", "MOD_RESUME_CHECKPOINT_LABEL=epoch-7", "MOD_RESUME_CHECKPOINT_PROGRESS=0"}
	if len(got) != len(want) {
		t.Errorf("stamp produced %d entries, expected %d: %v", len(got), len(want), got)
		return
	}
	for i := 0; i < len(want); i++ {
		if got[i] != want[i] {
			t.Errorf("entry %d is %s, expected %s", i, got[i], want[i])
		}
	}
}
func TestResumeCheckpointEnvPayloadCompacted(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	got := e.resumeCheckpointEnv(transport.TaskPayload{
		ResumeCheckpointID:      9,
		ResumeCheckpointPayload: []byte("{\n  \"step\": 3,\n  \"latents\": [1, 2]\n}\n"),
	})
	want := "MOD_RESUME_CHECKPOINT={\"step\":3,\"latents\":[1,2]}"
	if len(got) != 2 || got[0] != "MOD_RESUME_CHECKPOINT_ID=9" || got[1] != want {
		t.Errorf("pretty-printed payload produced %v; expected it forwarded compacted so the kernel copies no whitespace for nothing", got)
	}
	if logBuf.String() != "" {
		t.Error("a valid in-cap payload logged something; nothing was skipped here")
	}
}

// A malformed blob is not a resume point, so the node starts from its own
// beginning - what it would do with no stamp at all. The skip must be VISIBLE
// (an operator chasing a six-hour job that restarted wants to know the resume
// point was thrown away) and must name only the SIZE: the payload is node
// state - weights, paths, prompts - and the journal is not its place.
func TestResumeCheckpointEnvInvalidPayloadSkipped(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	got := e.resumeCheckpointEnv(transport.TaskPayload{
		ResumeCheckpointID:      9,
		ResumeCheckpointPayload: []byte("{step: 3"),
	})
	if len(got) != 1 || got[0] != "MOD_RESUME_CHECKPOINT_ID=9" {
		t.Errorf("malformed payload produced %v; it must be skipped, not forwarded half-parsed", got)
	}
	logged := logBuf.String()
	if !strings.Contains(logged, "not valid JSON") {
		t.Error("the invalid-payload skip was silent; the node restarting from zero is invisible to the operator")
	}
	if strings.Contains(logged, "step") {
		t.Error("the invalid-payload warning quoted the payload content; only its size may be logged")
	}
}

// 40KB is past the 32KB cap. The API caps what it stamps, but the worker
// re-checks: a blob that slipped past the cap must never be pushed at the
// kernel, where an envp overflow fails the exec and the node never starts.
func TestResumeCheckpointEnvOversizePayloadSkipped(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	filler := strings.Repeat("x", 40*1024)
	got := e.resumeCheckpointEnv(transport.TaskPayload{
		ResumeCheckpointID:      11,
		ResumeCheckpointPayload: []byte("{\"state\": \"" + filler + "\"}"),
	})
	if len(got) != 1 || got[0] != "MOD_RESUME_CHECKPOINT_ID=11" {
		t.Errorf("40KB payload produced %v; a blob past the env cap must be skipped, not pushed at the kernel", got)
	}
	logged := logBuf.String()
	if !strings.Contains(logged, "exceeds env size cap") {
		t.Error("the oversize skip was silent; the node restarting from zero is invisible to the operator")
	}
	if strings.Contains(logged, filler) {
		t.Error("the oversize warning quoted the payload content; only its size may be logged")
	}
}

// A label carrying a newline would split one env entry into two at the exec
// boundary AND make the worker's own log claim a task it never ran. NUL is
// worse still: it ends the C string the kernel copies, so everything after it
// silently vanishes from the node's view of its own resume point.
func TestResumeCheckpointEnvLabelSanitized(t *testing.T) {
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	got := e.resumeCheckpointEnv(transport.TaskPayload{
		ResumeCheckpointID:    5,
		ResumeCheckpointLabel: "epoch\n3\x00one",
	})
	want := "MOD_RESUME_CHECKPOINT_LABEL=epoch3one"
	if len(got) != 2 || got[1] != want {
		t.Errorf("label produced %v; expected %s", got, want)
	}
	if logBuf.String() != "" {
		t.Error("sanitizing a label logged something; nothing was skipped here")
	}
}

// The four keys the API stamps must survive the wire. This is the seam the
// sibling API slice writes against: a key that does not land on a field here
// is silently dropped at unmarshal, and the node then restarts from zero with
// no trace of why. RawMessage keeps the blob's bytes exactly as the API wrote
// them - re-serialising through interface{} would reorder keys and change what
// the node sees on each restart.
func TestResumeCheckpointPayloadRoundTrip(t *testing.T) {
	wire := "{" +
		"\"task_id\": 5," +
		"\"resume_checkpoint_id\": 77," +
		"\"resume_checkpoint_label\": \"epoch-12\"," +
		"\"resume_checkpoint_progress\": 0.25," +
		"\"resume_checkpoint_payload\": {\"step\": 12, \"rng\": [1, 2, 3]}" +
		"}"
	p := transport.TaskPayload{}
	if err := json.Unmarshal([]byte(wire), &p); err != nil {
		t.Errorf("the stamped payload failed to unmarshal: %v", err)
		return
	}
	if p.ResumeCheckpointID != 77 {
		t.Errorf("resume_checkpoint_id landed as %d; a node would restart from zero on a stamp the API says exists", p.ResumeCheckpointID)
	}
	if p.ResumeCheckpointLabel != "epoch-12" {
		t.Errorf("resume_checkpoint_label landed as %q", p.ResumeCheckpointLabel)
	}
	if p.ResumeCheckpointProgress == nil {
		t.Error("resume_checkpoint_progress did not land on its field; a node resuming at 0.25 would be told nothing")
	} else if *p.ResumeCheckpointProgress != 0.25 {
		t.Errorf("resume_checkpoint_progress landed as %v, expected 0.25", *p.ResumeCheckpointProgress)
	}
	if len(p.ResumeCheckpointPayload) == 0 {
		t.Error("resume_checkpoint_payload did not land on its field; the node's own state would be dropped at unmarshal")
		return
	}
	// And the landed blob must still reach env, compacted, through the helper.
	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}
	env := e.resumeCheckpointEnv(p)
	found := ""
	for i := 0; i < len(env); i++ {
		if strings.HasPrefix(env[i], "MOD_RESUME_CHECKPOINT=") {
			found = env[i]
		}
	}
	if found != "MOD_RESUME_CHECKPOINT={\"step\":12,\"rng\":[1,2,3]}" {
		t.Errorf("the round-tripped payload reached env as %q; expected the blob compacted with its bytes unchanged", found)
	}
}
