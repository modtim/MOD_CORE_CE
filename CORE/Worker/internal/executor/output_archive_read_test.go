package executor

// UNDER1S-OUTPUT-HARBOR-PAID: pure (no docker) tests for the registry-posture
// archive read: the parse half is the trust boundary, so every adversarial tar
// must be an ERROR (-> the caller runs the verbatim helper read), never content.

import (
	"archive/tar"
	"bytes"
	"context"
	"os"
	"strings"
	"testing"
)

func hpTar(t *testing.T, hdrs ...*tar.Header) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, h := range hdrs {
		if err := tw.WriteHeader(h); err != nil {
			t.Fatalf("hdr: %v", err)
		}
		if h.Typeflag == tar.TypeReg && h.Size > 0 && h.Size <= 1<<20 {
			tw.Write(bytes.Repeat([]byte("x"), int(h.Size)))
		}
	}
	tw.Close()
	return buf.Bytes()
}

func hpFile(t *testing.T, name, body string) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	tw.WriteHeader(&tar.Header{Name: name, Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(body))})
	tw.Write([]byte(body))
	tw.Close()
	return buf.Bytes()
}

func TestHPArchiveSingleFile_TrimsLikeCat(t *testing.T) {
	cases := map[string]string{
		"x":        `{"a":1}`,
		"  pad \n\n": `{"a":1}`,
	}
	for body := range cases {
		got, err := archiveSingleRegularFile(bytes.NewReader(hpFile(t, "result.json", body)), 0o644, "result.json")
		got = strings.TrimSpace(got) // the caller trims, as the cat split does
		if err != nil || got != strings.TrimSpace(body) {
			t.Fatalf("body %q: got %q err %v", body, got, err)
		}
		// same answer as the helper-cat split for the same content
		sp := splitHelperCatOutput("\nM result.json\n"+body+"\n", "M", []string{"result.json"})
		if sp["result.json"] != got {
			t.Fatalf("parity with splitHelperCatOutput: %q vs %q", sp["result.json"], got)
		}
	}
	if got, err := archiveSingleRegularFile(bytes.NewReader(hpFile(t, "result.json", "")), 0o644, "result.json"); err != nil || got != "" {
		t.Fatalf("empty file must read empty, got %q %v", got, err)
	}
}

func TestHPArchiveSingleFile_RefusesEverythingElse(t *testing.T) {
	big := &tar.Header{Name: "result.json", Typeflag: tar.TypeReg, Mode: 0o644, Size: archiveMetaMaxBytes + 1}
	cases := map[string]struct {
		tarBytes []byte
		mode     os.FileMode
	}{
		"symlink stat":     {hpFile(t, "result.json", "{}"), 0o777 | os.ModeSymlink},
		"dir stat":         {hpFile(t, "result.json", "{}"), 0o755 | os.ModeDir},
		"device stat":      {hpFile(t, "result.json", "{}"), 0o644 | os.ModeDevice},
		"symlink entry":    {hpTar(t, &tar.Header{Name: "result.json", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd", Mode: 0o777}), 0o644},
		"hardlink entry":   {hpTar(t, &tar.Header{Name: "result.json", Typeflag: tar.TypeLink, Linkname: "/etc/passwd", Mode: 0o644}), 0o644},
		"chardev entry":    {hpTar(t, &tar.Header{Name: "result.json", Typeflag: tar.TypeChar, Mode: 0o644}), 0o644},
		"dir entry":        {hpTar(t, &tar.Header{Name: "result.json/", Typeflag: tar.TypeDir, Mode: 0o755}), 0o644},
		"dotdot name":      {hpFile(t, "../result.json", "{}"), 0o644},
		"absolute name":    {hpFile(t, "/etc/passwd", "{}"), 0o644},
		"nested name":      {hpFile(t, "sub/result.json", "{}"), 0o644},
		"other file":       {hpFile(t, "export_result.json", "{}"), 0o644},
		"oversize claim":   {hpTar(t, big), 0o644},
		"two entries":      {hpTar(t, &tar.Header{Name: "result.json", Typeflag: tar.TypeReg, Mode: 0o644}, &tar.Header{Name: "extra", Typeflag: tar.TypeReg, Mode: 0o644}), 0o644},
		"empty archive":    {nil, 0o644},
		"garbage":          {[]byte("not a tar at all, just bytes"), 0o644},
	}
	for name, c := range cases {
		if got, err := archiveSingleRegularFile(bytes.NewReader(c.tarBytes), c.mode, "result.json"); err == nil {
			t.Errorf("%s: must be refused, got content %q", name, got)
		}
	}
}

func TestHPArchiveSingleFile_SizeClaimLieRefused(t *testing.T) {
	good := hpFile(t, "result.json", `{"abcdefghij":1}`)
	if _, err := archiveSingleRegularFile(bytes.NewReader(good[:512+5]), 0o644, "result.json"); err == nil {
		t.Fatalf("truncated body (header claims more than delivered) must be refused")
	}
}

func TestHPReadViaArchive_FailClosedWithoutDockerAndBadNames(t *testing.T) {
	var e *Executor
	if _, err := e.readResultFilesViaArchive(context.Background(), "v", "alpine", resultFileReadNames); err == nil {
		t.Fatalf("nil executor must error")
	}
	e = &Executor{}
	if _, err := e.readResultFilesViaArchive(context.Background(), "v", "alpine", resultFileReadNames); err == nil {
		t.Fatalf("no docker client must error (caller falls back)")
	}
}

func TestHPSafeSwitchDefaultOn(t *testing.T) {
	if !harborArchiveReadEnabled {
		t.Fatalf("the registry-posture archive read ships ON (safe switch flips it off)")
	}
}

// A real body over the cap (not just a lying header) must be refused, so a large
// metadata file is never silently truncated: the helper read (uncapped) decides.
func TestHPArchiveSingleFile_RealOversizeBodyRefused(t *testing.T) {
	body := strings.Repeat("a", archiveMetaMaxBytes+1)
	if _, err := archiveSingleRegularFile(bytes.NewReader(hpFile(t, "result.json", body)), 0o644, "result.json"); err == nil {
		t.Fatalf("a body over archiveMetaMaxBytes must be refused")
	}
	body = strings.Repeat("a", archiveMetaMaxBytes)
	if got, err := archiveSingleRegularFile(bytes.NewReader(hpFile(t, "result.json", body)), 0o644, "result.json"); err != nil || len(got) != archiveMetaMaxBytes {
		t.Fatalf("a body exactly at the cap is accepted, got len %d err %v", len(got), err)
	}
}
