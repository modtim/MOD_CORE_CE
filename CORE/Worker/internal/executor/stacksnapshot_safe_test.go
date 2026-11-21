package executor

// 30.6.1 c1-J3a — pure tests for validateTarStream: the worker's layer-2 guard
// that refuses an attacker-supplied volume tarball BEFORE anything is extracted.
// Tars are built in memory with archive/tar + compress/gzip; no S3, no docker.
// The ETag-match between the two passes is NOT unit-testable without S3 — it is
// the single line `opts.SetMatchETag(etag)` on restoreVolume's pass-2 GetObject.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"fmt"
	"io"
	"strings"
	"testing"
)

// gzTar builds an in-memory gzip tar from the headers (regular-file bodies come
// from hdr.Name-keyed contents).
func gzTar(hdrs []*tar.Header, contents map[string]string) []byte {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	w := tar.NewWriter(gz)
	for _, h := range hdrs {
		if err := w.WriteHeader(h); err != nil {
			panic(fmt.Sprintf("write header %s: %v", h.Name, err))
		}
		if h.Typeflag == tar.TypeReg && h.Size > 0 {
			body := contents[h.Name]
			if _, werr := w.Write([]byte(body)); werr != nil {
				panic(fmt.Sprintf("write body %s: %v", h.Name, werr))
			}
		}
	}
	if err := w.Close(); err != nil {
		panic(fmt.Sprintf("close tar: %v", err))
	}
	if err := gz.Close(); err != nil {
		panic(fmt.Sprintf("close gzip: %v", err))
	}
	return buf.Bytes()
}

// validate runs pass-1-style validation (emit = nil) over a gzip tar.
func validateGz(tarball []byte, maxBytes int64) (int64, int64, error) {
	gz, err := gzip.NewReader(bytes.NewBuffer(tarball))
	if err != nil {
		return 0, 0, err
	}
	return validateTarStream(gz, maxBytes, nil)
}

// validateAndEmit runs pass-2-style validation with a re-encoding sink and
// returns the emitted entry count.
func validateAndEmit(tarball []byte, maxBytes int64) (int64, int64, int64, error) {
	gz, err := gzip.NewReader(bytes.NewBuffer(tarball))
	if err != nil {
		return 0, 0, 0, err
	}
	var sink bytes.Buffer
	w := tar.NewWriter(&sink)
	entries, total, verr := validateTarStream(gz, maxBytes, w)
	_ = w.Close()
	emitted := int64(0)
	tr := tar.NewReader(bytes.NewBuffer(sink.Bytes()))
	for {
		h, e := tr.Next()
		if e == io.EOF {
			break
		}
		// tar.Reader.Next returns the SAME non-EOF error forever, so anything
		// other than EOF must fail fast and be reported to the caller.
		if e != nil {
			return entries, total, emitted, fmt.Errorf("sink: %w", e)
		}
		_ = h
		emitted++
	}
	_ = total
	_ = sink
	return entries, total, emitted, verr
}

func mkReg(name string, body string) *tar.Header {
	return &tar.Header{Name: name, Mode: 0o644, Size: int64(len(body)),
		Typeflag: tar.TypeReg, Uid: 7, Gid: 9}
}

func mkDir(name string) *tar.Header {
	return &tar.Header{Name: name, Mode: 0o755, Size: 0, Typeflag: tar.TypeDir}
}

func mkLink(name, target string, t byte) *tar.Header {
	return &tar.Header{Name: name, Mode: 0o777, Size: 0, Typeflag: t, Linkname: target}
}

// X1 — our OWN snapshot shape (`tar czf - -C /data .` -> ./a, ./d/, ./d/b) is
// accepted and pass 2 re-emits the same 3 entries.
func TestX1OwnSnapshotOk(t *testing.T) {
	contents := map[string]string{"./a": "A", "./d/b": "BB"}
	tarball := gzTar([]*tar.Header{mkReg("./a", "A"), mkDir("./d/"),
		mkReg("./d/b", "BB")}, contents)
	entries, total, err := validateGz(tarball, 0)
	if err != nil {
		t.Errorf("X1 own snapshot rejected: %v", err)
	}
	if entries != 3 || total != 3 {
		t.Errorf("X1 entries=%d total=%d want 3/3", entries, total)
	}
	_, _, emitted, err2 := validateAndEmit(tarball, 0)
	if err2 != nil || emitted != 3 {
		t.Errorf("X1 emitted=%d err=%v want 3", emitted, err2)
	}
}

// X2 — `../escape` is refused.
func TestX2Traversal(t *testing.T) {
	tarball := gzTar([]*tar.Header{mkReg("../escape", "A")}, map[string]string{"../escape": "A"})
	_, _, err := validateGz(tarball, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: name") {
		t.Errorf("X2 expected name rejection, got %v", err)
	}
}

// X3 — an absolute member name is refused.
func TestX3Absolute(t *testing.T) {
	tarball := gzTar([]*tar.Header{mkReg("/etc/passwd", "A")}, map[string]string{"/etc/passwd": "A"})
	_, _, err := validateGz(tarball, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: name") {
		t.Errorf("X3 expected name rejection, got %v", err)
	}
}

// X4 — a symlink whose target escapes the root is refused; a relative in-root
// target is accepted.
func TestX4SymlinkTarget(t *testing.T) {
	bad := gzTar([]*tar.Header{mkLink("l", "../../x", tar.TypeSymlink)}, nil)
	_, _, err := validateGz(bad, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: symlink_target") {
		t.Errorf("X4 expected symlink_target rejection, got %v", err)
	}
	abs := gzTar([]*tar.Header{mkLink("l", "/tmp/x", tar.TypeSymlink)}, nil)
	_, _, err2 := validateGz(abs, 0)
	if err2 == nil || !strings.Contains(err2.Error(), "archive_rejected: symlink_target") {
		t.Errorf("X4b absolute symlink target accepted, got %v", err2)
	}
	good := gzTar([]*tar.Header{mkLink("l", "sub/x", tar.TypeSymlink)}, nil)
	_, _, err3 := validateGz(good, 0)
	if err3 != nil {
		t.Errorf("X4c relative in-root symlink rejected: %v", err3)
	}
}

// X5 — a hardlink to an unseen/later name is refused; to an earlier regular
// file it is accepted.
func TestX5Hardlink(t *testing.T) {
	later := gzTar([]*tar.Header{mkLink("h", "a", tar.TypeLink), mkReg("a", "A")},
		map[string]string{"a": "A"})
	_, _, err := validateGz(later, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: hardlink_target") {
		t.Errorf("X5 expected hardlink_target rejection, got %v", err)
	}
	earlier := gzTar([]*tar.Header{mkReg("a", "A"), mkLink("h", "a", tar.TypeLink)},
		map[string]string{"a": "A"})
	_, _, err2 := validateGz(earlier, 0)
	if err2 != nil {
		t.Errorf("X5b hardlink to earlier file rejected: %v", err2)
	}
	// A hardlink to a SYMLINK is refused too (only regular files are targets).
	viaLink := gzTar([]*tar.Header{mkLink("s", "sub", tar.TypeSymlink),
		mkLink("h", "s", tar.TypeLink)}, nil)
	_, _, err3 := validateGz(viaLink, 0)
	if err3 == nil || !strings.Contains(err3.Error(), "archive_rejected: hardlink_target") {
		t.Errorf("X5c hardlink to symlink accepted, got %v", err3)
	}
}

// X6 — device nodes and fifos are refused.
func TestX6SpecialTypes(t *testing.T) {
	chars := gzTar([]*tar.Header{&tar.Header{Name: "c", Mode: 0o666, Size: 0,
		Typeflag: tar.TypeChar, Devmajor: 1, Devminor: 3}}, nil)
	_, _, err := validateGz(chars, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: type") {
		t.Errorf("X6 expected type rejection (chardev), got %v", err)
	}
	fifos := gzTar([]*tar.Header{&tar.Header{Name: "f", Mode: 0o666, Size: 0,
		Typeflag: tar.TypeFifo}}, nil)
	_, _, err2 := validateGz(fifos, 0)
	if err2 == nil || !strings.Contains(err2.Error(), "archive_rejected: type") {
		t.Errorf("X6b expected type rejection (fifo), got %v", err2)
	}
}

// X7 — the declared Size alone (not the real body) must trip the cap.
func TestX7Cap(t *testing.T) {
	tarball := gzTar([]*tar.Header{&tar.Header{Name: "a", Mode: 0o644, Size: 11,
		Typeflag: tar.TypeReg}}, map[string]string{"a": "0123456789A"})
	_, _, err := validateGz(tarball, 10)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: cap") {
		t.Errorf("X7 expected cap rejection at maxBytes=10, got %v", err)
	}
	_, _, err2 := validateGz(tarball, 11)
	if err2 != nil {
		t.Errorf("X7b 11 bytes at maxBytes=11 rejected: %v", err2)
	}
}

// X8 — the setuid/setgid/sticky bits are stripped in the re-encoded header.
func TestX8SetuidStripped(t *testing.T) {
	tarball := gzTar([]*tar.Header{&tar.Header{Name: "a", Mode: 0o4755, Size: 1,
		Typeflag: tar.TypeReg, Uid: 7, Gid: 9}}, map[string]string{"a": "A"})
	var sink bytes.Buffer
	w := tar.NewWriter(&sink)
	gz, err := gzip.NewReader(bytes.NewBuffer(tarball))
	if err != nil {
		t.Errorf("X8 gzip: %v", err)
		return
	}
	_, _, verr := validateTarStream(gz, 0, w)
	_ = w.Close()
	if verr != nil {
		t.Errorf("X8 rejected clean setuid file: %v", verr)
		return
	}
	h, herr := tar.NewReader(bytes.NewBuffer(sink.Bytes())).Next()
	if herr != nil {
		t.Errorf("X8 emitted header unreadable: %v", herr)
		return
	}
	if h.Mode != 0o755 {
		t.Errorf("X8 emitted mode %o, want 0755 (setuid/sticky stripped)", h.Mode)
	}
	if h.Uid != 7 || h.Gid != 9 {
		t.Errorf("X8 uid/gid changed: %d/%d", h.Uid, h.Gid)
	}
}

// X9 — a corrupt gzip is an error, and nothing is emitted beyond what was
// validated before the corruption.
func TestX9Corrupt(t *testing.T) {
	good := gzTar([]*tar.Header{mkReg("./a", "A")}, map[string]string{"./a": "A"})
	rotten := append(append([]byte{}, good...), 0x7b, 0x7c, 0x7d)
	// Truncating the gzip stream is the realistic corruption: pass 1 fails, so
	// pass 2 never runs and nothing is written.
	_, _, err := validateGz(rotten[:len(good)-8], 0)
	if err == nil {
		t.Errorf("X9 expected an error from a truncated archive")
	}
	var sink bytes.Buffer
	w := tar.NewWriter(&sink)
	gz, gerr := gzip.NewReader(bytes.NewBuffer(rotten[:len(good)-8]))
	if gerr != nil {
		_ = sink
		return
	}
	_, _, eerr := validateTarStream(gz, 0, w)
	_ = w.Close()
	if eerr == nil {
		t.Errorf("X9b expected emit-path error from a truncated archive")
	}
	_ = sink
}

// X10 — a symlink CHAIN: l1 -> l2, l2 -> x, then a member that writes THROUGH
// l1 (`l1/sub`) is refused (symlink_parent).
func TestX10SymlinkChain(t *testing.T) {
	bad := gzTar([]*tar.Header{mkLink("l1", "l2", tar.TypeSymlink),
		mkLink("l2", "x", tar.TypeSymlink), mkReg("l1/sub", "A")},
		map[string]string{"l1/sub": "A"})
	_, _, err := validateGz(bad, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: symlink_parent") {
		t.Errorf("X10 expected symlink_parent rejection, got %v", err)
	}
}

// X11 — a symlink whose TARGET is another symlink: l1 -> a, l2 -> l1, then the
// file `l2/x` passes through the earlier symlink l2 -> refused.
func TestX11SymlinkThroughSymlink(t *testing.T) {
	bad := gzTar([]*tar.Header{mkLink("l1", "a", tar.TypeSymlink),
		mkLink("l2", "l1", tar.TypeSymlink), mkReg("l2/x", "A")},
		map[string]string{"l2/x": "A"})
	_, _, err := validateGz(bad, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: symlink_parent") {
		t.Errorf("X11 expected symlink_parent rejection, got %v", err)
	}
}

// X12 — the entry-count cap. The production cap is 1,000,000 members, which no
// unit test can build; the unexported package var maxTarEntries is lowered to 3
// for this test and restored by t.Cleanup.
func TestX12EntryCountCap(t *testing.T) {
	prod := maxTarEntries
	maxTarEntries = 3
	t.Cleanup(func() { maxTarEntries = prod })
	four := gzTar([]*tar.Header{mkReg("a", "A"), mkReg("b", "B"), mkReg("c", "C"),
		mkReg("d", "D")}, map[string]string{"a": "A", "b": "B", "c": "C", "d": "D"})
	_, _, err := validateGz(four, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: entry_count") {
		t.Errorf("X12 expected entry_count rejection at cap 3, got %v", err)
	}
	three := gzTar([]*tar.Header{mkReg("a", "A"), mkReg("b", "B"), mkReg("c", "C")},
		map[string]string{"a": "A", "b": "B", "c": "C"})
	entries, _, err2 := validateGz(three, 0)
	if err2 != nil || entries != 3 {
		t.Errorf("X12b 3 entries at cap 3: entries=%d err=%v", entries, err2)
	}
}

// X13 — a hardlink whose target is a DIRECTORY entry is refused (only earlier
// regular files may be hardlink targets).
func TestX13HardlinkToDir(t *testing.T) {
	bad := gzTar([]*tar.Header{mkDir("d/"), mkLink("h", "d", tar.TypeLink)}, nil)
	_, _, err := validateGz(bad, 0)
	if err == nil || !strings.Contains(err.Error(), "archive_rejected: hardlink_target") {
		t.Errorf("X13 expected hardlink_target rejection (target is a dir), got %v", err)
	}
}

// X14 — a symlink sibling escape: `l -> sub`, then the member `sub/../../x`.
// Refused (the name cleans to `../x`; the symlink-parent rule also covers it).
func TestX14SymlinkSiblingEscape(t *testing.T) {
	bad := gzTar([]*tar.Header{mkLink("l", "sub", tar.TypeSymlink),
		mkReg("sub/../../x", "A")}, map[string]string{"sub/../../x": "A"})
	_, _, err := validateGz(bad, 0)
	if err == nil || (!strings.Contains(err.Error(), "archive_rejected: name") &&
		!strings.Contains(err.Error(), "archive_rejected: symlink_parent")) {
		t.Errorf("X14 expected name/symlink_parent rejection of sub/../../x, got %v", err)
	}
}

// X15 — a symlink in the root whose target carries a `./` prefix: `l -> ./a` is
// accepted.
func TestX15SymlinkInRootOk(t *testing.T) {
	good := gzTar([]*tar.Header{mkReg("./a", "A"), mkLink("l", "./a", tar.TypeSymlink)},
		map[string]string{"./a": "A"})
	entries, _, err := validateGz(good, 0)
	if err != nil || entries != 2 {
		t.Errorf("X15 expected accept of l -> ./a, got entries=%d err=%v", entries, err)
	}
}

// X16 — the exact `tar czf - -C /data .` shape: `./`, `./a`, `./d/`, `./d/b` are
// all accepted, and pass 2 emits the same 4 paths (in cleaned form: `./a` ->
// `a`, `./d/` -> `d/`; the `./` root entry is kept as `./`).
func TestX16DotDirOwnSnapshot(t *testing.T) {
	contents := map[string]string{"./a": "A", "./d/b": "BB"}
	tarball := gzTar([]*tar.Header{mkDir("./"), mkReg("./a", "A"), mkDir("./d/"),
		mkReg("./d/b", "BB")}, contents)
	_, _, err := validateGz(tarball, 0)
	if err != nil {
		t.Errorf("X16 own-snapshot shape rejected: %v", err)
		return
	}
	var sink bytes.Buffer
	w := tar.NewWriter(&sink)
	gz, gerr := gzip.NewReader(bytes.NewBuffer(tarball))
	if gerr != nil {
		t.Errorf("X16 gzip: %v", gerr)
		return
	}
	entries, _, verr := validateTarStream(gz, 0, w)
	_ = w.Close()
	if verr != nil || entries != 4 {
		t.Errorf("X16 emit path: entries=%d err=%v want 4", entries, verr)
		return
	}
	want := map[string]bool{"./": true, "a": true, "d/": true, "d/b": true}
	tr := tar.NewReader(bytes.NewBuffer(sink.Bytes()))
	got := int64(0)
	unknown := ""
	for {
		h, e := tr.Next()
		if e == io.EOF {
			break
		}
		if e != nil {
			t.Errorf("X16 emitted header unreadable: %v", e)
			return
		}
		got++
		if !want[h.Name] {
			unknown = h.Name
		}
	}
	if got != 4 {
		t.Errorf("X16 emitted=%d want 4", got)
	}
	if unknown != "" {
		t.Errorf("X16 unexpected emitted name %s", unknown)
	}
}

// X17 — a long name (the writer adds a PAX `path` record) plus an xattr is
// accepted, and the re-encoded header is clean: cleaned name, no xattrs, and no
// PAX records beyond the `path` the writer itself adds for the long name.
func TestX17PaxLongNameAndXattr(t *testing.T) {
	long := strings.Repeat("x", 120)
	h := mkReg(long, "A")
	h.Xattrs = map[string]string{"user.x": "y"}
	tarball := gzTar([]*tar.Header{h}, map[string]string{long: "A"})
	var sink bytes.Buffer
	w := tar.NewWriter(&sink)
	gz, gerr := gzip.NewReader(bytes.NewBuffer(tarball))
	if gerr != nil {
		t.Errorf("X17 gzip: %v", gerr)
		return
	}
	_, _, verr := validateTarStream(gz, 0, w)
	_ = w.Close()
	if verr != nil {
		t.Errorf("X17 rejected a PAX long-name + xattr member: %v", verr)
		return
	}
	em, eerr := tar.NewReader(bytes.NewBuffer(sink.Bytes())).Next()
	if eerr != nil {
		t.Errorf("X17 emitted header unreadable: %v", eerr)
		return
	}
	if em.Name != long {
		t.Errorf("X17 emitted name %q, want %q", em.Name, long)
	}
	if em.Xattrs != nil && len(em.Xattrs) > 0 {
		t.Errorf("X17 emitted xattrs %v, want none", em.Xattrs)
	}
	if em.PAXRecords != nil {
		for k := range em.PAXRecords {
			if k != "path" {
				t.Errorf("X17 unexpected PAX record key %q", k)
			}
		}
	}
}

// X18 — the gzip trailer is the trust boundary: tar stops at the end-of-archive
// blocks, so validateTarStream now drains the gzip stream and Go's CRC32/ISIZE
// check runs there. Flipping the trailer CRC must be refused on BOTH passes.
func TestX18TrailerCrcFlip(t *testing.T) {
	good := gzTar([]*tar.Header{}, map[string]string{})
	rotten := append([]byte{}, good...)
	rotten[len(good)-8] ^= 0xff
	_, _, err := validateGz(rotten, 0)
	if err == nil || !strings.Contains(fmt.Sprint(err), "archive_rejected: trailer") {
		t.Errorf("X18 want archive_rejected: trailer from a flipped CRC, got %v", err)
	}
	_, _, emitted, err2 := validateAndEmit(rotten, 0)
	if err2 == nil || !strings.Contains(fmt.Sprint(err2), "archive_rejected: trailer") {
		t.Errorf("X18 emit path want archive_rejected: trailer, got %v", err2)
	}
	if emitted != 0 {
		t.Errorf("X18 emit path emitted %d entries, want 0", emitted)
	}
}

// X19 — trailing data: Go's gzip reader is multistream, so an appended second
// member is readable after the tar ends. The 1 MiB drain cap refuses it.
func TestX19AppendedGzipMember(t *testing.T) {
	good := gzTar([]*tar.Header{mkReg("./a", "A")}, map[string]string{"./a": "A"})
	var eb bytes.Buffer
	gz2 := gzip.NewWriter(&eb)
	if _, werr := gz2.Write([]byte(strings.Repeat("0", maxTrailer+1))); werr != nil {
		panic(fmt.Sprintf("X19 write extra member: %v", werr))
	}
	if cerr := gz2.Close(); cerr != nil {
		panic(fmt.Sprintf("X19 close extra member: %v", cerr))
	}
	rotten := append(append([]byte{}, good...), eb.Bytes()...)
	_, _, err := validateGz(rotten, 0)
	if err == nil || !strings.Contains(fmt.Sprint(err), "archive_rejected: trailing data") {
		t.Errorf("X19 want archive_rejected: trailing data, got %v", err)
	}
	_, _, emitted, err2 := validateAndEmit(rotten, 0)
	if err2 == nil || !strings.Contains(fmt.Sprint(err2), "archive_rejected: trailing data") {
		t.Errorf("X19 emit path want archive_rejected: trailing data, got %v", err2)
	}
	_ = emitted
}
