package executor

import (
	"fmt"
	"strings"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// What the sidecar copies had never been asserted, and that is why a real bug
// lived here undetected: a downstream node always receives ONE task carrying
// EVERY cog, the sidecar staged only cog[0], and every task still reported
// success. `blur_detection_image_process` given 38 images logged "Found 0
// images to analyze / Processed: 0" and COMPLETED without error.

func TestSingleFileStagingIsUnchanged(t *testing.T) {
	// The only path that works today. It must keep working byte for byte —
	// every existing node depends on the fixed input_<node_kind> name.
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: "abc123",
		FileExtension:      "jpg",
		NodeKind:           "resize_image_process",
	})
	if err != nil {
		t.Fatalf("valid single-file payload rejected: %v", err)
	}
	want := "mc cp myminio/tenant-1-files/abc123 /data/input/input_resize_image_process.jpg"
	if !strings.Contains(cmd, want) {
		t.Fatalf("single-file staging changed shape.\ngot:  %s\nwant: %s", cmd, want)
	}
	if strings.Count(cmd, "mc cp") != 1 {
		t.Fatalf("expected exactly one copy, got %d: %s", strings.Count(cmd, "mc cp"), cmd)
	}
}

func TestEveryInputFileIsStaged(t *testing.T) {
	// The bug in one assertion: 38 in, 38 copied.
	var files []transport.InputFile
	for i := 1; i <= 38; i++ {
		files = append(files, transport.InputFile{
			CogID: i, ContentHash: "hash", FileExtension: "jpg",
		})
	}
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket: "tenant-1-files", InputFiles: files,
	})
	if err != nil {
		t.Fatalf("valid multi-file payload rejected: %v", err)
	}
	if n := strings.Count(cmd, "mc cp"); n != 38 {
		t.Fatalf("staged %d of 38 inputs: %s", n, cmd)
	}
}

func TestEachFileKeepsItsOwnExtension(t *testing.T) {
	// The reason InputFile carries an extension at all. A capture can mix
	// formats — DJI writes JPG beside DNG — and the payload's single
	// FileExtension is whatever cog[0] happened to be. Applying that to every
	// file hands the container a DNG named .jpg, which it cannot decode, and
	// produces a confident wrong answer about the user's data.
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket:   "tenant-1-files",
		FileExtension: "jpg", // cog[0] was a JPEG; must NOT be applied to all
		InputFiles: []transport.InputFile{
			{CogID: 1, ContentHash: "h1", FileExtension: "jpg"},
			{CogID: 2, ContentHash: "h2", FileExtension: "dng"},
			{CogID: 3, ContentHash: "h3", FileExtension: "tif"},
		},
	})
	if err != nil {
		t.Fatalf("valid mixed-capture payload rejected: %v", err)
	}
	for _, want := range []string{
		"/data/input/1.jpg", "/data/input/2.dng", "/data/input/3.tif",
	} {
		if !strings.Contains(cmd, want) {
			t.Fatalf("missing %s in: %s", want, cmd)
		}
	}
	if strings.Contains(cmd, "2.jpg") {
		t.Fatal("the DNG was named .jpg — cog[0]'s extension leaked onto every file")
	}
}

func TestDestinationsAreUniquePerCog(t *testing.T) {
	// Two cogs can share a filename across folders. Naming by cog id makes a
	// collision unrepresentable; naming by filename would silently reduce the
	// input set, which is the bug being fixed.
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket: "tenant-1-files",
		InputFiles: []transport.InputFile{
			{CogID: 10, ContentHash: "h1", FileExtension: "jpg"},
			{CogID: 11, ContentHash: "h2", FileExtension: "jpg"},
		},
	})
	if err != nil {
		t.Fatalf("valid payload rejected: %v", err)
	}
	seen := map[string]bool{}
	for _, line := range strings.Split(cmd, "&&") {
		if !strings.Contains(line, "mc cp") {
			continue
		}
		fields := strings.Fields(line)
		dest := fields[len(fields)-1]
		if seen[dest] {
			t.Fatalf("two inputs share destination %s — one would overwrite the other", dest)
		}
		seen[dest] = true
	}
}

func TestAFileWithNoHashIsSkippedNotStagedEmpty(t *testing.T) {
	// An unresolvable cog must not become `mc cp bucket/ /data/input/...`,
	// which would fail the whole sidecar and take the other 37 files with it.
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket: "tenant-1-files",
		InputFiles: []transport.InputFile{
			{CogID: 1, ContentHash: "h1", FileExtension: "jpg"},
			{CogID: 2, ContentHash: "", FileExtension: "jpg"},
		},
	})
	if err != nil {
		t.Fatalf("valid payload rejected: %v", err)
	}
	if n := strings.Count(cmd, "mc cp"); n != 1 {
		t.Fatalf("expected the hashless input skipped, got %d copies: %s", n, cmd)
	}
}

func TestExtensionlessFilesGetNoStrayDot(t *testing.T) {
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket: "tenant-1-files",
		InputFiles:  []transport.InputFile{{CogID: 5, ContentHash: "h", FileExtension: ""}},
	})
	if err != nil {
		t.Fatalf("valid extensionless payload rejected: %v", err)
	}
	if !strings.Contains(cmd, "/data/input/5 ") && !strings.HasSuffix(cmd, "/data/input/5") {
		t.Fatalf("expected /data/input/5 with no trailing dot: %s", cmd)
	}
}

func TestDotPrefixedExtensionIsNotDoubled(t *testing.T) {
	// The server sends "jpg"; be tolerant of ".jpg" rather than producing
	// "1..jpg", which no container would match.
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket: "tenant-1-files",
		InputFiles:  []transport.InputFile{{CogID: 1, ContentHash: "h", FileExtension: ".jpg"}},
	})
	if err != nil {
		t.Fatalf("valid dot-prefixed payload rejected: %v", err)
	}
	if strings.Contains(cmd, "1..jpg") {
		t.Fatalf("double dot in destination: %s", cmd)
	}
	if !strings.Contains(cmd, "/data/input/1.jpg") {
		t.Fatalf("expected /data/input/1.jpg: %s", cmd)
	}
}

// R-154 follow-up: the tenant's FileExtension and NodeKind are interpolated
// into the sidecar's shell command. The native staging path (R-154) already
// allowlists the extension and asserts the dest stays inside the staging dir;
// the container path must hold the same contract — an escaping name is
// rejected with a clear error and ZERO downloads staged (validation happens
// before the command exists, so nothing is ever partially copied).
func TestDownloadCmdRejectsEscapingPayload(t *testing.T) {
	longExt := "abcdefghijklmnopq" // 17 alphanumerics: over the 16 cap
	cases := []struct {
		name    string
		payload transport.TaskPayload
	}{
		{"payload ext traversal", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "../evil", NodeKind: "k",
		}},
		{"payload ext absolute", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "/etc/passwd", NodeKind: "k",
		}},
		{"payload ext embedded dot", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "tar.gz", NodeKind: "k",
		}},
		{"payload ext NUL byte", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "png\x00x", NodeKind: "k",
		}},
		{"payload ext over 16 chars", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: longExt, NodeKind: "k",
		}},
		{"input file ext traversal", transport.TaskPayload{
			MinioBucket: "b",
			InputFiles:  []transport.InputFile{{CogID: 1, ContentHash: "h", FileExtension: "../../x"}},
		}},
		{"input file ext over 16 chars", transport.TaskPayload{
			MinioBucket: "b",
			InputFiles:  []transport.InputFile{{CogID: 1, ContentHash: "h", FileExtension: longExt}},
		}},
		{"node kind traversal", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "jpg", NodeKind: "../evil",
		}},
		{"node kind absolute", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "jpg", NodeKind: "/etc/passwd",
		}},
		{"node kind subdir", transport.TaskPayload{
			MinioBucket: "b", SourceFileLocation: "h", FileExtension: "jpg", NodeKind: "a/b",
		}},
		// A bad name MID-LIST must sink the whole command — not stage the
		// good files and fail on the bad one.
		{"bad second of two files", transport.TaskPayload{
			MinioBucket: "b",
			InputFiles: []transport.InputFile{
				{CogID: 1, ContentHash: "h1", FileExtension: "jpg"},
				{CogID: 2, ContentHash: "h2", FileExtension: "../x"},
			},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cmd, err := buildDownloadCmd(tc.payload)
			if err == nil {
				t.Fatalf("accepted an escaping payload, cmd=%q", cmd)
			}
			if !strings.Contains(err.Error(), "refusing") {
				t.Fatalf("error is not the clear refusal, got: %v", err)
			}
			if n := strings.Count(cmd, "mc cp"); n != 0 {
				t.Fatalf("rejection must stage zero downloads, got %d: %s", n, cmd)
			}
		})
	}
}

// The guard must not reject legitimate tenants: a long-but-legal extension
// (near the 16-char cap) passes, and the single-file name keeps its exact
// byte shape.
func TestDownloadCmdAcceptsBoundaryValidNames(t *testing.T) {
	cmd, err := buildDownloadCmd(transport.TaskPayload{
		MinioBucket: "b", SourceFileLocation: "h",
		FileExtension: "abcdefghijklmno", // 15 chars: under the cap
		NodeKind:      "resize_image_process",
	})
	if err != nil {
		t.Fatalf("valid boundary payload rejected: %v", err)
	}
	want := "mc cp myminio/b/h /data/input/input_resize_image_process.abcdefghijklmno"
	if !strings.Contains(cmd, want) {
		t.Fatalf("boundary staging changed shape.\ngot:  %s\nwant: %s", cmd, want)
	}
}

// --- calibration-health-fixtures-and-timeouts §3 / §7.2 -------------------
//
// Regression guard for the truncation bug design.md calls out: a sidecar's
// stdout success banner ("Added myminio successfully.") landed in front of
// its stderr failure line, and whatever assembled the stored error_message
// cut the string off inside the banner — "Added myminio successfully.
// \nmc" — losing the diagnostic entirely. These tests pin the fix: stdout
// and stderr are kept SEPARATE on a sidecar exit, and only the tool's own
// stderr flows into the failure path that ultimately reports back to the
// API as error_detail.

func TestSidecarStderrSurvivesSeparateFromStdout(t *testing.T) {
	// Simulates "mc cp source-not-found": stdout carries the success
	// banner, stderr carries the actual diagnostic.
	sidecarErr := &SidecarExitError{
		ExitCode: 1,
		Stdout:   "Added `myminio` successfully.\n",
		Stderr: "mc: <ERROR> Unable to stat remote file " +
			"`myminio/tenant-1-files/calibration-data/large/sample.usd`. " +
			"The specified key does not exist.\n",
	}

	// Error() keeps the old combined shape — anything that only logs
	// err.Error() (e.g. taskLog.Error) still sees both halves.
	combined := sidecarErr.Error()
	if !strings.Contains(combined, "Added `myminio` successfully.") {
		t.Fatalf("combined message lost the stdout banner: %s", combined)
	}
	if !strings.Contains(combined, "specified key does not exist") {
		t.Fatalf("combined message lost the stderr diagnostic: %s", combined)
	}

	// sidecarErrorResult must extract STDERR ONLY into ErrorDetail — the
	// whole point of §3 is a field where the banner can never crowd out
	// the diagnostic line.
	res := sidecarErrorResult(sidecarErr)
	if res == nil {
		t.Fatal("expected a non-nil *Result carrying ErrorDetail")
	}
	if strings.Contains(res.ErrorDetail, "successfully") {
		t.Fatalf("ErrorDetail leaked the stdout success banner: %s", res.ErrorDetail)
	}
	if !strings.Contains(res.ErrorDetail, "specified key does not exist") {
		t.Fatalf("ErrorDetail is missing the actual failure: %s", res.ErrorDetail)
	}
}

func TestSidecarErrorResultUnwrapsThroughWrappedErrors(t *testing.T) {
	// prepareDataVolume wraps with fmt.Errorf("prepare /data: %w", err);
	// Run()'s download/upload branches pass the sidecar error through
	// unwrapped before wrapping it themselves. Both shapes must still
	// surface ErrorDetail via errors.As.
	base := &SidecarExitError{ExitCode: 1, Stdout: "", Stderr: "mc: <ERROR> access denied\n"}
	wrapped := fmt.Errorf("prepare /data: %w", base)

	res := sidecarErrorResult(wrapped)
	if res == nil || !strings.Contains(res.ErrorDetail, "access denied") {
		t.Fatalf("sidecarErrorResult did not unwrap a wrapped SidecarExitError: %+v", res)
	}
}

func TestSidecarErrorResultNilForNonSidecarErrors(t *testing.T) {
	// A generic error (e.g. "pull minio-client" failing, or a context
	// timeout) must not be misreported as sidecar stderr.
	if res := sidecarErrorResult(fmt.Errorf("pull minio-client: connection refused")); res != nil {
		t.Fatalf("expected nil Result for a non-sidecar error, got %+v", res)
	}
}

func TestSidecarErrorResultNilWhenStderrEmpty(t *testing.T) {
	// A sidecar that exits non-zero with nothing on stderr (unusual, but
	// possible) must not produce a Result with an empty ErrorDetail that
	// would silently overwrite a more useful log_tail downstream.
	sidecarErr := &SidecarExitError{ExitCode: 1, Stdout: "some banner\n", Stderr: ""}
	if res := sidecarErrorResult(sidecarErr); res != nil {
		t.Fatalf("expected nil Result when Stderr is empty, got %+v", res)
	}
}
