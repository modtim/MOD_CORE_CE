package executor

import (
	"bytes"
	"context"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// encrypted_scratch_test.go — FX.123. Covers the AES-256-GCM scratch layer
// (round-trip, nothing readable on disk, per-file nonce, tamper detection,
// name refusal, Destroy semantics) and the native staging hook that puts a
// Destroy on a defer in the staging path.

// scratchBase is a scratch root the tests mint task dirs inside.
func scratchBase(t *testing.T) string {
	d := t.TempDir()
	if err := os.Mkdir(filepath.Join(d, "root"), 0o700); err != nil {
		t.Fatalf("scratch root: %v", err)
	}
	return filepath.Join(d, "root")
}

func TestEncryptedScratchRoundTrip(t *testing.T) {
	s, err := NewEncryptedScratch(scratchBase(t), "4242")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	defer func() { _ = s.Destroy() }()
	const payload = "the task's bytes, sealed"
	if err := s.WriteFile("input_src.mp4", []byte(payload)); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	got, rerr := s.ReadFile("input_src.mp4")
	if rerr != nil {
		// (a sealed file must open back to exactly what was written)
		t.Fatalf("ReadFile: %v", rerr)
	}
	if !bytes.Equal(got, []byte(payload)) {
		t.Fatalf("round-trip = %q, want %q", got, payload)
	}
}

func TestEncryptedScratchBytesOnDiskAreNotPlaintext(t *testing.T) {
	base := scratchBase(t)
	s, err := NewEncryptedScratch(base, "4300")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	defer func() { _ = s.Destroy() }()
	const payload = "PLAINTEXT-MARKER-DO-NOT-LEAK"
	if err := s.WriteFile("secret.bin", []byte(payload)); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	blob, rerr := os.ReadFile(filepath.Join(base, "4300", "secret.bin"))
	if rerr != nil {
		// (the sealed file must exist inside the 0700 task dir)
		t.Fatalf("sealed file missing: %v", rerr)
	}
	if bytes.Contains(blob, []byte(payload)) {
		// (the whole point of the layer: the plaintext is never on disk)
		t.Fatalf("plaintext found on disk: %q", blob)
	}
	if !bytes.HasPrefix(blob, []byte("MODSCR1\n")) {
		t.Fatalf("sealed file has no scratch header: %q", blob)
	}
}

func TestEncryptedScratchSamePlaintextGivesDifferentCiphertext(t *testing.T) {
	base := scratchBase(t)
	s, err := NewEncryptedScratch(base, "4301")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	defer func() { _ = s.Destroy() }()
	const payload = "identical input bytes both times"
	if err := s.WriteFile("a.bin", []byte(payload)); err != nil {
		t.Fatalf("WriteFile a.bin: %v", err)
	}
	if err := s.WriteFile("b.bin", []byte(payload)); err != nil {
		t.Fatalf("WriteFile b.bin: %v", err)
	}
	one, err := os.ReadFile(filepath.Join(base, "4301", "a.bin"))
	if err != nil {
		// (the sealed file must exist inside the 0700 task dir)
		t.Fatalf("reading a.bin: %v", err)
	}
	two, err := os.ReadFile(filepath.Join(base, "4301", "b.bin"))
	if err != nil {
		t.Fatalf("reading b.bin: %v", err)
	}
	if bytes.Equal(one, two) {
		// (a fresh nonce per file is what keeps equal plaintexts apart)
		_ = one
		_ = two
		t.Fatalf("two writes of the same plaintext produced identical bytes")
	}
}

// cipherFactory hands the encrypted scratch to the native staging hook.
func cipherFactory(baseDir, taskID string) (ScratchCipher, error) {
	return NewEncryptedScratch(baseDir, taskID)
}

func TestEncryptedScratchDestroyRemovesDirAndZeroesKey(t *testing.T) {
	base := scratchBase(t)
	s, err := NewEncryptedScratch(base, "4302")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	if err := s.WriteFile("x.bin", []byte("scratch bytes")); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	live := false
	for _, b := range s.key {
		if b != 0 {
			live = true
		}
	}
	if !live {
		t.Fatalf("key was already zero before Destroy")
	}
	if err := s.Destroy(); err != nil {
		t.Fatalf("Destroy: %v", err)
	}
	if _, err := os.Stat(filepath.Join(base, "4302")); err == nil {
		t.Fatalf("scratch dir still present after Destroy")
	}
	for i, b := range s.key {
		if b != 0 {
			t.Fatalf("key byte %d is %d after Destroy, want 0", i, b)
		}
	}
	if err := s.Destroy(); err != nil {
		t.Fatalf("second Destroy: %v", err)
	}
}

func TestEncryptedScratchUseAfterDestroyErrors(t *testing.T) {
	s, err := NewEncryptedScratch(scratchBase(t), "4303")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	if err := s.WriteFile("x.bin", []byte("scratch bytes")); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	if err := s.Destroy(); err != nil {
		t.Fatalf("Destroy: %v", err)
	}
	if _, rerr := s.ReadFile("x.bin"); rerr == nil {
		t.Fatalf("ReadFile after Destroy must fail")
	}
	if werr := s.WriteFile("y.bin", []byte("late")); werr == nil {
		t.Fatalf("WriteFile after Destroy must fail")
	}
}

func TestEncryptedScratchRefusesBadNames(t *testing.T) {
	s, err := NewEncryptedScratch(scratchBase(t), "4304")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	defer func() { _ = s.Destroy() }()
	for _, bad := range []string{"", "..", "../escape", "sub/dir", "sub\\dir", "a/"} {
		if werr := s.WriteFile(bad, []byte("smuggled")); werr == nil {
			t.Fatalf("WriteFile accepted hostile name %q", bad)
		}
		if _, rerr := s.ReadFile(bad); rerr == nil {
			t.Fatalf("ReadFile accepted hostile name %q", bad)
		}
	}
}

func TestEncryptedScratchTamperedCiphertextFailsToRead(t *testing.T) {
	base := scratchBase(t)
	s, err := NewEncryptedScratch(base, "4305")
	if err != nil {
		t.Fatalf("NewEncryptedScratch: %v", err)
	}
	defer func() { _ = s.Destroy() }()
	if err := s.WriteFile("x.bin", []byte("authentic bytes")); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	path := filepath.Join(base, "4305", "x.bin")
	blob, rerr := os.ReadFile(path)
	if rerr != nil {
		t.Fatalf("reading sealed file: %v", rerr)
	}
	// (flip one byte of the ciphertext, past the 8-byte header and nonce)
	off := len("MODSCR1\n") + 12
	blob[off] = blob[off] ^ 0x01
	if err := os.WriteFile(path, blob, 0o600); err != nil {
		t.Fatalf("writing tampered file: %v", err)
	}
	if _, terr := s.ReadFile("x.bin"); terr == nil {
		t.Fatalf("tampered ciphertext read back without error")
	}
}

// FX.123.s1b: the scratch now survives staging (the native process has to
// read its inputs) and is destroyed by the run's teardown instead.
type scratchTracker struct {
	s *EncryptedScratch
}

var scratchTracked scratchTracker

func cipherFactoryTracked(baseDir, taskID string) (ScratchCipher, error) {
	sc, err := NewEncryptedScratch(baseDir, taskID)
	if err != nil {
		return nil, err
	}
	scratchTracked.s = sc
	return sc, nil
}

func zeroedKey(s *EncryptedScratch) bool {
	for _, b := range s.key {
		if b != 0 {
			return false
		}
	}
	return true
}

func TestStagingHookUsesEncryptedScratch(t *testing.T) {
	fake := newFakeS3("tenant-1-files")
	const content = "the one and only input"
	hash := sha256s(content)
	fake.objs[hash] = []byte(content)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := fakeS3Creds(srv)
	client := clientForFake(t, creds)
	base := scratchBase(t)
	SetScratchFactory(cipherFactoryTracked)
	defer SetScratchFactory(nil)
	defer func() { scratchTracked.s = nil }()
	inputDir := filepath.Join(base, "5001", "input")
	payload := transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: hash,
		FileExtension:      "jpg",
		NodeKind:           "resize_image_process",
	}
	if err := StageNativeInputs(context.Background(), client, payload, inputDir); err != nil {
		t.Fatalf("staging through the hook: %v", err)
	}
	// FX.123.s1b: the scratch must still be alive when staging returns — the
	// native process reads its inputs after this point, so a Destroy here would
	// leave the run with nothing to read.
	sealedPath := filepath.Join(base, "5001", "input_resize_image_process.jpg")
	sealed, err := os.ReadFile(sealedPath)
	if err != nil {
		t.Fatalf("sealed scratch file missing while the run is alive: %v", err)
	}
	if bytes.Contains(sealed, []byte(content)) {
		t.Fatalf("plaintext found in the sealed scratch file: %q", sealed)
	}
	if !bytes.HasPrefix(sealed, []byte("MODSCR1\n")) {
		t.Fatalf("sealed file has no scratch header: %q", sealed)
	}
	// The run step reads the decrypted input from the task's 0700 input dir.
	got, rerr := os.ReadFile(filepath.Join(inputDir, "input_resize_image_process.jpg"))
	if rerr != nil {
		t.Fatalf("run step cannot read its input: %v", rerr)
	}
	if !bytes.Equal(got, []byte(content)) {
		t.Fatalf("run input = %q, want %q", got, content)
	}
	// Teardown (the run's Cancel defer) is what destroys the scratch.
	if derr := destroyStagedScratch(inputDir); derr != nil {
		t.Fatalf("teardown Destroy: %v", derr)
	}
	if _, err := os.Stat(filepath.Join(base, "5001")); err == nil {
		t.Fatalf("task dir still present after teardown")
	}
	if scratchTracked.s == nil || !zeroedKey(scratchTracked.s) {
		t.Fatalf("key not zeroed after teardown")
	}
}

// The run's teardown destroys the scratch on every terminal path, including
// the cancel path (dispatchNativeTask defers Cancel whatever the outcome is).
type runStager struct {
	client *minio.Client
}

func (s *runStager) StageInputs(ctx context.Context, task *transport.Task, _ *transport.Credentials, inputDir string) error {
	return StageNativeInputs(ctx, s.client, task.Payload, inputDir)
}

func (s *runStager) CollectOutputs(_ context.Context, _ *transport.Task, _ *transport.Credentials, _ string) ([]transport.OutputFile, error) {
	return nil, nil
}

func TestStagingHookRunTeardownDestroysScratch(t *testing.T) {
	fake := newFakeS3("tenant-1-files")
	const content = "bytes the run must be able to read"
	hash := sha256s(content)
	fake.objs[hash] = []byte(content)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := fakeS3Creds(srv)
	client := clientForFake(t, creds)
	base := scratchBase(t)
	SetScratchFactory(cipherFactoryTracked)
	defer SetScratchFactory(nil)
	defer func() { scratchTracked.s = nil }()

	nb := NewNativeBackend(base)
	nb.SetStager(&runStager{client: client})
	spec := &TaskSpec{
		NativeCommand: "sh",
		NativeArgs:    []string{"-c", "cat {input_dir}/* > /dev/null; exit 0"},
		Env:           map[string]string{"MOD_TASK_ID": "5002"},
	}
	task := &transport.Task{ID: 5002, Payload: transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: hash,
		FileExtension:      "mp4",
		NodeKind:           "resize_image_process",
	}}
	inputDir := filepath.Join(base, "5002", "input")
	h, err := nb.SubmitStaged(context.Background(), spec, task, &transport.Credentials{MinIO: creds})
	if err != nil {
		t.Fatalf("SubmitStaged: %v", err)
	}
	// The run's inputs exist while it runs.
	if _, rerr := os.Stat(filepath.Join(inputDir, "input_resize_image_process.mp4")); rerr != nil {
		t.Fatalf("run input missing before teardown: %v", rerr)
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		st, perr := nb.Poll(context.Background(), h)
		if perr != nil {
			t.Fatalf("Poll: %v", perr)
		}
		if st.Phase != TaskRunning {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("run never settled")
		}
		time.Sleep(20 * time.Millisecond)
	}
	if cerr := nb.Cancel(context.Background(), h); cerr != nil {
		t.Fatalf("Cancel: %v", cerr)
	}
	if _, err := os.Stat(filepath.Join(base, "5002")); err == nil {
		t.Fatalf("task dir still present after the run's teardown")
	}
	if scratchTracked.s == nil || !zeroedKey(scratchTracked.s) {
		t.Fatalf("key not zeroed after the run's teardown")
	}
}

// The cancel path destroys too: a staging pass that is cut short (cancelled
// context, hash mismatch) leaves no scratch dir and no live key.
func TestStagingHookCancelDestroysScratch(t *testing.T) {
	fake := newFakeS3("tenant-1-files")
	const content = "the one and only input"
	hash := sha256s(content)
	fake.objs[hash] = []byte(content)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := fakeS3Creds(srv)
	client := clientForFake(t, creds)
	base := scratchBase(t)
	SetScratchFactory(cipherFactoryTracked)
	defer SetScratchFactory(nil)
	defer func() { scratchTracked.s = nil }()
	inputDir := filepath.Join(base, "5003", "input")
	payload := transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: hash,
		FileExtension:      "jpg",
		NodeKind:           "resize_image_process",
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := StageNativeInputs(ctx, client, payload, inputDir); err == nil {
		t.Fatalf("a cancelled staging pass must fail")
	}
	if _, err := os.Stat(filepath.Join(base, "5003")); err == nil {
		t.Fatalf("cancelled staging left the scratch dir behind")
	}
	if scratchTracked.s == nil || !zeroedKey(scratchTracked.s) {
		t.Fatalf("cancelled staging left a live key")
	}
	// A second teardown is a no-op success (Destroy is idempotent).
	if derr := destroyStagedScratch(inputDir); derr != nil {
		t.Fatalf("second teardown: %v", derr)
	}
}

// Flag off (the default, and what production wires today) keeps the plaintext
// staging path byte for byte: a plain input dir, plaintext files on disk, and
// nothing removed by staging itself.
func TestStagingHookFlagOffKeepsPlaintextPath(t *testing.T) {
	fake := newFakeS3("tenant-1-files")
	const content = "PLAINTEXT-MARKER-DO-NOT-LEAK"
	hash := sha256s(content)
	fake.objs[hash] = []byte(content)
	srv := httptest.NewServer(fake)
	defer srv.Close()
	creds := fakeS3Creds(srv)
	client := clientForFake(t, creds)
	base := scratchBase(t)
	SetScratchFactory(nil)
	inputDir := filepath.Join(base, "5004", "input")
	payload := transport.TaskPayload{
		MinioBucket:        "tenant-1-files",
		SourceFileLocation: hash,
		FileExtension:      "jpg",
		NodeKind:           "resize_image_process",
	}
	if err := StageNativeInputs(context.Background(), client, payload, inputDir); err != nil {
		t.Fatalf("plaintext staging: %v", err)
	}
	if _, err := os.Stat(filepath.Join(base, "5004", "input")); err != nil {
		t.Fatalf("plaintext input dir missing: %v", err)
	}
	blob, err := os.ReadFile(filepath.Join(inputDir, "input_resize_image_process.jpg"))
	if err != nil {
		t.Fatalf("reading staged plaintext input: %v", err)
	}
	if !bytes.Equal(blob, []byte(content)) {
		t.Fatalf("plaintext staging changed the staged bytes: %q", blob)
	}
	// Staging never removes anything on the plaintext path (the scratch tree's
	// own cleanup owns the task dir).
	if _, err := os.Stat(filepath.Join(base, "5004")); err != nil {
		t.Fatalf("plaintext staging removed the task dir: %v", err)
	}
	if derr := destroyStagedScratch(inputDir); derr != nil {
		t.Fatalf("teardown with no scratch registered: %v", derr)
	}
}
