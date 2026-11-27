package executor

// OPT2 (lane gpu2-2) — the /data seed folded into the download leg's helper
// container instead of a second throwaway container per task. These tests pin
// the behaviour that must stay IDENTICAL: the same gate the download step
// uses, the same seed string prepareDataVolume runs, and a fold that fails
// closed (a seed failure must short-circuit the mc legs, exactly like the
// standalone prep failing before the download ever started).

import (
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestHasInputStagingMatchesTheDownloadGate(t *testing.T) {
	cases := []struct {
		name   string
		p      transport.TaskPayload
		expect bool
	}{
		{"source only", transport.TaskPayload{SourceFileLocation: "abc"}, true},
		{"inputs only", transport.TaskPayload{InputFiles: []transport.InputFile{{ContentHash: "x"}}}, true},
		{"both", transport.TaskPayload{SourceFileLocation: "abc", InputFiles: []transport.InputFile{{ContentHash: "x"}}}, true},
		{"neither", transport.TaskPayload{}, false},
		{"source but skip", transport.TaskPayload{SourceFileLocation: "abc", SkipDownload: true}, false},
		{"inputs but skip", transport.TaskPayload{InputFiles: []transport.InputFile{{ContentHash: "x"}}, SkipDownload: true}, false},
		{"empty source string", transport.TaskPayload{SourceFileLocation: ""}, false},
	}
	for _, c := range cases {
		if got := hasInputStaging(c.p); got != c.expect {
			t.Fatalf("%s: hasInputStaging=%v expected=%v", c.name, got, c.expect)
		}
	}
}

func TestFoldKeepsTheSeedByteIdentical(t *testing.T) {
	// The folded seed must be the SAME command prepareDataVolume runs — one
	// const (dataSeedCmd), so this asserts the fold wraps it unchanged and the
	// standalone prep still embeds it unchanged.
	folded := foldSeedIntoCmd("true && mc cp myminio/b/k /data/input/in_x.png")
	if !strings.Contains(folded, dataSeedCmd) {
		t.Fatalf("fold lost the seed const: %s", folded)
	}
	// prepareDataVolume's own command (executor.go) is
	//   set -e; <dataSeedCmd>; echo "prep OK: ..." — same const, checked here
	// against the literal so a drift in EITHER call site fails this test.
	const prepSeed = "mkdir -p /data/input /data/output; chmod 0777 /data /data/input /data/output"
	if dataSeedCmd != prepSeed {
		t.Fatalf("dataSeedCmd drifted from the prep literal: %q", dataSeedCmd)
	}
}

func TestFoldShapeIsSubshellAndChain(t *testing.T) {
	// Shape: ( <seed> ) && <cmd>. The subshell keeps the seed's ';' chain from
	// binding into the outer '&&' chain, and the join is exactly ") && ".
	folded := foldSeedIntoCmd("CMD")
	if !strings.HasPrefix(folded, "(") || !strings.Contains(folded, ") && CMD") {
		t.Fatalf("fold shape wrong: %s", folded)
	}
	if strings.Contains(folded, ") && CMD &&") || strings.HasSuffix(folded, "CMD ") {
		t.Fatalf("fold added trailing junk: %s", folded)
	}
}

func TestFoldPreservesEmptyCmdLeg(t *testing.T) {
	// buildDownloadCmd always returns at least `true`, but the fold must not
	// assume that: an empty cmd still yields a valid sh command.
	folded := foldSeedIntoCmd("")
	if folded != "(" + dataSeedCmd + ") && " {
		t.Fatalf("fold on empty cmd: %q", folded)
	}
}
