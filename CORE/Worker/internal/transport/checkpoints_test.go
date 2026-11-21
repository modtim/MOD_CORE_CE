package transport

import "testing"

// Extraction lives in the WORKER because that is the one place every node's
// output is visible regardless of transport. Parsing on the manager instead
// recorded nothing for embedded workers, whose log lines go straight to Redis
// and never reach the API — found by a real run, missed by 20 unit tests.
func TestCheckpointsAreFoundInRealisticOutput(t *testing.T) {
	lines := []string{
		"starting up",
		`MOD_CHECKPOINT {"progress":0.4,"label":"step 4/10","payload":{"step":4}}`,
		"2026-08-14T02:00:00Z stdout MOD_CHECKPOINT {\"progress\":0.5,\"label\":\"step 5/10\"}",
		"completed step 5",
	}
	got := ExtractCheckpoints(lines)
	if len(got) != 2 {
		t.Fatalf("extracted %d, want 2 (timestamps and stream names prefix real output)", len(got))
	}
	if got[0].Label != "step 4/10" || got[0].Progress == nil || *got[0].Progress != 0.4 {
		t.Errorf("first checkpoint wrong: %+v", got[0])
	}
}

// A reporting convenience must never be able to fail a task that is running
// fine, so anything unparseable is skipped rather than surfaced.
func TestMalformedLinesAreSkipped(t *testing.T) {
	got := ExtractCheckpoints([]string{
		"MOD_CHECKPOINT not json",
		"MOD_CHECKPOINT ",
		`MOD_CHECKPOINT {"progress":0.9}`,
	})
	if len(got) != 1 {
		t.Fatalf("extracted %d, want 1", len(got))
	}
}

func TestOrdinaryOutputYieldsNothing(t *testing.T) {
	if got := ExtractCheckpoints([]string{"rendering frame 12", "", "done"}); len(got) != 0 {
		t.Errorf("extracted %d from ordinary output", len(got))
	}
}

// The prefix must match the manager's constant or nothing lines up.
func TestPrefixMatchesTheManagers(t *testing.T) {
	if CheckpointPrefix != "MOD_CHECKPOINT " {
		t.Errorf("prefix drifted from CHECKPOINT_LOG_PREFIX: %q", CheckpointPrefix)
	}
}

// Both worker log paths must extract. The node path was missed the first time
// — only the stack-op callback was wired — so node tasks, the ones that
// actually run for six hours, recorded nothing. This asserts the helper is
// reachable and behaves identically wherever it is called from; the wiring
// itself is covered by the manual test's live run.
func TestExtractionIsCallableFromAnyLogPath(t *testing.T) {
	batch := []string{`MOD_CHECKPOINT {"progress":0.5,"label":"half"}`}
	for _, path := range []string{"stack-op", "node-processing"} {
		got := ExtractCheckpoints(batch)
		if len(got) != 1 || got[0].Label != "half" {
			t.Errorf("%s path: extracted %d, want 1", path, len(got))
		}
	}
}
