package executor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"testing"

	"github.com/minio/minio-go/v7"
)

// fakeObjectStore is an in-memory objectStore for WS4.Q17.16 unit tests —
// no real S3-compatible server needed, per the task's "fake S3 client/
// interface" requirement.
type fakeObjectStore struct {
	mu      sync.Mutex
	objects map[string][]byte // bucket/key -> content
	sha     map[string]string // bucket/key -> stamped sha256 metadata
	getErr  map[string]error  // bucket/key -> error to return from Get
	putErr  map[string]error  // bucket/key -> error to return from Put
	// readErr: bucket/key -> Get succeeds but the FIRST Read of the returned
	// body fails with this error (models minio-go's LAZY GetObject: a missing
	// source key surfaces inside the put's read, FIX-BYO-S3-ACTIVATION-SKIP-P1).
	readErr map[string]error
	// calls records every method invoked as "Method bucket/key" so tests can
	// assert (never a Delete/Remove on the source; Put called for a re-copy).
	calls []string
	// corruptRead: bucket/key -> bytes served by Get INSTEAD of the stored
	// content (a destination that returns wrong bytes on the re-read).
	corruptRead map[string][]byte
}

func (f *fakeObjectStore) callsTo(method string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.calls {
		if strings.HasPrefix(c, method+" ") {
			n++
		}
	}
	return n
}

func newFakeObjectStore() *fakeObjectStore {
	return &fakeObjectStore{
		objects: map[string][]byte{},
		sha:     map[string]string{},
		getErr:  map[string]error{},
		putErr:  map[string]error{},
		readErr: map[string]error{},
	}
}

// failingReader is a ReadCloser whose FIRST Read fails with err (the LAZY
// minio-go GetObject shape: Get returns fine, the body read carries the
// NoSuchKey / flattened-text error inside the put).
type failingReaderImpl struct {
	err    error
	served bool
}

func (f *failingReaderImpl) Read(p []byte) (int, error) {
	if !f.served {
		f.served = true
		return 0, f.err
	}
	return 0, io.EOF
}

func (f *failingReaderImpl) Close() error { return nil }

func failingReader(err error) io.ReadCloser {
	return &failingReaderImpl{err: err}
}

func bk(bucket, key string) string { return bucket + "/" + key }

func (f *fakeObjectStore) Get(ctx context.Context, bucket, key string) (io.ReadCloser, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "Get "+bk(bucket, key))
	if err, ok := f.getErr[bk(bucket, key)]; ok {
		return nil, err
	}
	if err, ok := f.readErr[bk(bucket, key)]; ok {
		// LAZY shape: Get succeeds, the first body Read fails.
		return failingReader(err), nil
	}
	if bad, ok := f.corruptRead[bk(bucket, key)]; ok {
		return io.NopCloser(bytes.NewReader(bad)), nil
	}
	data, ok := f.objects[bk(bucket, key)]
	if !ok {
		return nil, errors.New("not found")
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

func (f *fakeObjectStore) Put(ctx context.Context, bucket, key string, r io.Reader, size int64, sha256Hex string) error {
	f.mu.Lock()
	f.calls = append(f.calls, "Put "+bk(bucket, key))
	putErr := f.putErr[bk(bucket, key)]
	f.mu.Unlock()
	if putErr != nil {
		return putErr
	}
	data, err := io.ReadAll(r)
	if err != nil {
		return err
	}
	f.mu.Lock()
	f.objects[bk(bucket, key)] = data
	f.sha[bk(bucket, key)] = sha256Hex
	f.mu.Unlock()
	return nil
}

func (f *fakeObjectStore) Stat(ctx context.Context, bucket, key string) (int64, string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "Stat "+bk(bucket, key))
	data, ok := f.objects[bk(bucket, key)]
	if !ok {
		return 0, "", false
	}
	return int64(len(data)), f.sha[bk(bucket, key)], true
}

func hashOf(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func TestRunStorageMigrationCopiesAndVerifies(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("hello world, this is object one")
	src.objects[bk("src-bucket", "obj1")] = content

	objs := []StorageMigrationObject{
		{Key: "obj1", SHA256: hashOf(content), SizeBytes: int64(len(content))},
	}

	var progressCalls int
	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", objs,
		func(r StorageMigrationResult) { progressCalls++ })
	if err != nil {
		t.Fatalf("RunStorageMigration returned error: %v", err)
	}
	if result.ObjectsDone != 1 || result.BytesDone != int64(len(content)) || result.Skipped != 0 {
		t.Fatalf("unexpected result: %+v", result)
	}
	if progressCalls != 1 {
		t.Fatalf("expected 1 progress call, got %d", progressCalls)
	}
	gotData, ok := dst.objects[bk("dst-bucket", "obj1")]
	if !ok || !bytes.Equal(gotData, content) {
		t.Fatalf("dest object missing or mismatched: %v %v", ok, gotData)
	}
}

func TestRunStorageMigrationAbortsOnFirstVerifyFailure(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	goodContent := []byte("good object content")
	badContent := []byte("this object's stored hash will not match")
	src.objects[bk("src-bucket", "good")] = goodContent
	src.objects[bk("src-bucket", "bad")] = badContent

	objs := []StorageMigrationObject{
		{Key: "good", SHA256: hashOf(goodContent), SizeBytes: int64(len(goodContent))},
		// Wrong expected hash — source-hash check inside copyAndVerifyOne must fail this.
		{Key: "bad", SHA256: hashOf([]byte("not the real content")), SizeBytes: int64(len(badContent))},
		{Key: "never-reached", SHA256: "deadbeef", SizeBytes: 10},
	}
	src.objects[bk("src-bucket", "never-reached")] = []byte("0123456789")

	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err == nil {
		t.Fatal("expected an error from the mismatched object, got nil")
	}
	if result.ObjectsDone != 1 {
		t.Fatalf("expected exactly 1 object done before abort, got %d", result.ObjectsDone)
	}
	if _, ok := dst.objects[bk("dst-bucket", "never-reached")]; ok {
		t.Fatal("third object must never have been attempted after the abort")
	}
}

func TestRunStorageMigrationIdempotentSkip(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("already migrated content")
	h := hashOf(content)
	src.objects[bk("src-bucket", "obj1")] = content
	// Dest already has the object with matching size+sha — simulates a re-run
	// after a partial prior migration (A.5's resumability).
	dst.objects[bk("dst-bucket", "obj1")] = content
	dst.sha[bk("dst-bucket", "obj1")] = h

	objs := []StorageMigrationObject{
		{Key: "obj1", SHA256: h, SizeBytes: int64(len(content))},
	}

	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Skipped != 1 || result.ObjectsDone != 1 {
		t.Fatalf("expected the object to be skipped as already-matching, got %+v", result)
	}
}

func TestRunStorageMigrationSkipMismatchReCopies(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("the correct current content")
	h := hashOf(content)
	src.objects[bk("src-bucket", "obj1")] = content
	// Dest has an object at this key, but it's stale (different content / hash).
	dst.objects[bk("dst-bucket", "obj1")] = []byte("stale old content")
	dst.sha[bk("dst-bucket", "obj1")] = hashOf([]byte("stale old content"))

	objs := []StorageMigrationObject{
		{Key: "obj1", SHA256: h, SizeBytes: int64(len(content))},
	}

	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Skipped != 0 {
		t.Fatalf("stale dest object must be re-copied, not skipped: %+v", result)
	}
	gotData := dst.objects[bk("dst-bucket", "obj1")]
	if !bytes.Equal(gotData, content) {
		t.Fatalf("dest object was not overwritten with the correct content")
	}
}

func TestRunStorageMigrationAbortsOnSourceGetError(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.getErr[bk("src-bucket", "obj1")] = errors.New("source unreachable")

	objs := []StorageMigrationObject{
		{Key: "obj1", SHA256: "abc", SizeBytes: 5},
	}
	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err == nil {
		t.Fatal("expected an error when the source Get fails")
	}
	if result.ObjectsDone != 0 {
		t.Fatalf("expected 0 objects done, got %d", result.ObjectsDone)
	}
}

func TestRunStorageMigrationEmptyManifest(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", nil, nil)
	if err != nil {
		t.Fatalf("unexpected error on empty manifest: %v", err)
	}
	if result.ObjectsDone != 0 || result.Skipped != 0 || result.BytesDone != 0 {
		t.Fatalf("expected a zero-value result, got %+v", result)
	}
}

// --- FIX-BYO-MIGRATION-SKIP-MISSING ----------------------------------------
// PROOF-BOX-J-2 F1: dangling content_blobs rows (the source object is gone)
// aborted the whole migration. The skip is OPT-IN, only NoSuchKey is skipped,
// and a skipped key never counts as copied/verified.

func missingSourceErr(key string) error {
	return minio.ErrorResponse{Code: "NoSuchKey", Key: key, Message: "The specified key does not exist."}
}

func TestRunStorageMigrationSkipMissingFalseAbortsAsBefore(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	// A real S3 GetObject is LAZY: the missing key surfaces inside Put. The
	// fake models the eager shape (Get errors) — both are NoSuchKey.
	src.getErr[bk("src-bucket", "gone")] = missingSourceErr("gone")

	objs := []StorageMigrationObject{
		{Key: "gone", SHA256: "abc", SizeBytes: 5},
		{Key: "later", SHA256: "def", SizeBytes: 5},
	}
	result, err := RunStorageMigration(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err == nil {
		t.Fatal("default (flag false) must still abort on a missing source key")
	}
	if result.ObjectsDone != 0 || result.MissingCount != 0 {
		t.Fatalf("default path must not skip anything, got %+v", result)
	}
}

func TestRunStorageMigrationSkipMissingTrueSkipsNoSuchKeyAndReports(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	c1 := []byte("first object content")
	c3 := []byte("third object content")
	src.objects[bk("src-bucket", "one")] = c1
	src.objects[bk("src-bucket", "three")] = c3
	// The middle object's source key is dangling (the J-2 case: 15 rows, 3
	// objects in the store).
	src.getErr[bk("src-bucket", "two")] = missingSourceErr("two")

	objs := []StorageMigrationObject{
		{Key: "one", SHA256: hashOf(c1), SizeBytes: int64(len(c1))},
		{Key: "two", SHA256: hashOf([]byte("gone content")), SizeBytes: 42},
		{Key: "three", SHA256: hashOf(c3), SizeBytes: int64(len(c3))},
	}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err != nil {
		t.Fatalf("skip-missing run must not abort on a NoSuchKey: %v", err)
	}
	if result.ObjectsDone != 2 {
		t.Fatalf("expected 2 objects copied, got %d", result.ObjectsDone)
	}
	if result.MissingCount != 1 || len(result.MissingSourceKeys) != 1 ||
		result.MissingSourceKeys[0] != "two" {
		t.Fatalf("expected the missing key counted+listed, got %+v", result)
	}
	if result.BytesDone != int64(len(c1)+len(c3)) {
		t.Fatalf("a skipped key must never count toward bytes, got %d", result.BytesDone)
	}
	if _, ok := dst.objects[bk("dst-bucket", "two")]; ok {
		t.Fatal("the missing key must not have been written to the dest")
	}
}

func TestRunStorageMigrationSkipMissingTrueStillAbortsOnOtherErrors(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	// A permission error is NOT a missing key: even with the flag on it must
	// abort, and nothing may be reported as skipped.
	src.getErr[bk("src-bucket", "denied")] = minio.ErrorResponse{Code: "AccessDenied", Key: "denied"}

	objs := []StorageMigrationObject{
		{Key: "denied", SHA256: "abc", SizeBytes: 5},
	}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err == nil {
		t.Fatal("only NoSuchKey may be skipped — AccessDenied must abort")
	}
	if result.MissingCount != 0 {
		t.Fatalf("a non-NoSuchKey error must not be counted as missing, got %+v", result)
	}
}

func TestRunStorageMigrationSkipMissingCap50KeysCountContinues(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	var objs []StorageMigrationObject
	for i := 0; i < 60; i++ {
		key := fmt.Sprintf("gone-%02d", i)
		src.getErr[bk("src-bucket", key)] = missingSourceErr(key)
		objs = append(objs, StorageMigrationObject{Key: key, SHA256: "abc", SizeBytes: 3})
	}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst, "src-bucket", "dst-bucket", objs, nil)
	if err != nil {
		t.Fatalf("all-missing run must not abort: %v", err)
	}
	if result.MissingCount != 60 {
		t.Fatalf("MissingCount must keep counting past the cap, got %d", result.MissingCount)
	}
	if len(result.MissingSourceKeys) != storageMigrationMissingKeyCap {
		t.Fatalf("listed keys must cap at %d, got %d", storageMigrationMissingKeyCap, len(result.MissingSourceKeys))
	}
	if result.ObjectsDone != 0 || result.BytesDone != 0 {
		t.Fatalf("nothing was copied, got %+v", result)
	}
}

// --- IMPL-BYO-MOVE-OUT-B-GO-WORKER -----------------------------------------
// The move-out chunk runner: the manifest is a bucket LIST (entries can
// carry no size and no sha - a FOREIGN key the tenant owns but MOD never
// knew has no content_blobs row), a missing source key is RECORDED (never
// an abort), an object already verified at the destination is skipped
// (resumable retry), and a sha-less entry is verified against the bytes
// that actually came out of the source.

func TestMoveOutChunkCopiesAndVerifies(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("move-out object one")
	src.objects[bk("byo-bucket", "mod-key-1")] = content
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "mod-key-1", SHA256: hashOf(content), SizeBytes: int64(len(content))}}, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.ObjectsDone != 1 || result.BytesDone != int64(len(content)) {
		t.Fatalf("expected one copied object, got %+v", result)
	}
	got := dst.objects[bk("house-bucket", "mod-key-1")]
	if got == nil || !bytes.Equal(got, content) {
		t.Fatalf("dest must hold the exact bytes, got %+v", result)
	}
}

// A FOREIGN key: the LIST page gave us the key only (no size, no sha).
// Verification must still compare the dest re-read against the bytes that
// came out of the source, and the copy must land.
func TestMoveOutChunkForeignKeysWithoutShaAreCopiedAndVerified(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("a file the tenant put in their own bucket")
	src.objects[bk("byo-bucket", "foreign-readme")] = content
	// Manifest entry from the LIST: key only.
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "foreign-readme"}}, nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.ObjectsDone != 1 {
		t.Fatalf("expected one copied object, got %+v", result)
	}
	got := dst.objects[bk("house-bucket", "foreign-readme")]
	if got == nil || !bytes.Equal(got, content) {
		t.Fatalf("dest must hold the exact bytes, got %+v", result)
	}
	// The activate path must NOT skip a sha-less entry (unchanged behaviour).
	src2 := newFakeObjectStore()
	dst2 := newFakeObjectStore()
	other := []byte("activate path never skips without a sha")
	src2.objects[bk("src-bucket", "no-sha")] = other
	dst2.objects[bk("dst-bucket", "no-sha")] = other
	r2, err2 := RunStorageMigration(context.Background(), src2, dst2, "src-bucket", "dst-bucket",
		[]StorageMigrationObject{{Key: "no-sha"}}, nil)
	if err2 != nil {
		t.Fatalf("unexpected error: %v", err2)
	}
	if r2.Skipped != 0 || r2.ObjectsDone != 1 {
		t.Fatalf("activate path must re-copy a sha-less entry, got %+v", r2)
	}
}

// RESUME after a partial chunk (design s5 "Partial failure": retry is
// idempotent ONLY if the Go runner skips a key already present at dest).
// Chunk 1 copied obj A then failed on obj B; re-running the SAME chunk
// must skip A (already verified at dest) and copy B, and the tally must
// account for both.
func TestMoveOutChunkResumesAfterPartialChunk(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	a := []byte("aaaa")
	c := []byte("cccccc")
	b := []byte("bbbbbbbb")
	src.objects[bk("byo-bucket", "a")] = a
	src.objects[bk("byo-bucket", "c")] = c
	src.objects[bk("byo-bucket", "b")] = b
	objs := []StorageMigrationObject{
		// a is SHA-CARRYING (MOD knows it): resume skips it cheaply.
		{Key: "a", SHA256: hashOf(a), SizeBytes: int64(len(a))},
		// c is a FOREIGN key (sha-less): a key's existence proves nothing, so
		// resume must RE-COPY it.
		{Key: "c", SizeBytes: int64(len(c))},
		{Key: "b", SHA256: hashOf(b), SizeBytes: int64(len(b))},
	}
	// Pass 1: obj b's Put fails (a transient dest hiccup) -> abort.
	dst.putErr[bk("house-bucket", "b")] = errors.New("dest hiccup")
	_, firstErr := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		objs, nil)
	if firstErr == nil {
		t.Fatalf("pass 1 must fail on the dest hiccup")
	}
	if dst.objects[bk("house-bucket", "a")] == nil || dst.objects[bk("house-bucket", "c")] == nil {
		t.Fatalf("pass 1 must have left the prefix it copied, got %v", firstErr)
	}
	putsToCBefore := dst.callsTo("Put") // a, c, b(failed) = 3 Put attempts
	// Pass 2 (the retry): the hiccup is gone; a is already verified at dest.
	// (Reassigning the map instead of `delete` keeps the fake's other keys
	// intact and avoids a statement form the older toolchain trips on.)
	dst.putErr = map[string]error{}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		objs, nil)
	if err != nil {
		t.Fatalf("retry must complete the chunk: %v", err)
	}
	if result.Skipped != 1 || result.ObjectsDone != 3 ||
		// IMPL-BYO-MOVE-OUT-B-GO-WORKER: a skipped (already-verified) key
		// still counts, so the chunk's tally is the FULL accounting the API's
		// move-status sums across chunks.
		result.BytesDone != int64(len(a))+int64(len(b))+int64(len(c)) {
		t.Fatalf("resume must skip only sha-carrying a, re-copy c and b, got %+v", result)
	}
	// Pass 2 issued Put for c and b (NOT for a): 2 more Put calls.
	if got := dst.callsTo("Put") - putsToCBefore; got != 2 {
		t.Fatalf("pass 2 must Put exactly c and b (sha-less always re-copied, sha-carrying a skipped), got %d Puts", got)
	}
	if !bytes.Equal(dst.objects[bk("house-bucket", "c")], c) {
		t.Fatalf("re-copied c must equal the source bytes")
	}
}

// FIX-GO-MOVE-OUT-ALREADY-AT-DEST (BLOCK-1): a sha-less (foreign) key whose
// dest object has the SAME SIZE but WRONG bytes must NOT be skipped on size
// alone: it is overwritten with the source bytes, verified, counted ONCE.
func TestMoveOutChunkShaLessSameSizeWrongBytesIsOverwritten(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	good := []byte("the right bytes!")
	wrong := []byte("WRONG WRONG BYTE") // same length, different content
	if len(good) != len(wrong) {
		t.Fatalf("test setup: sizes must match")
	}
	src.objects[bk("byo-bucket", "foreign")] = good
	dst.objects[bk("house-bucket", "foreign")] = wrong
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "foreign", SizeBytes: int64(len(good))}}, nil)
	if err != nil {
		t.Fatalf("must overwrite and verify: %v", err)
	}
	if result.Skipped != 0 || result.ObjectsDone != 1 || result.BytesDone != int64(len(good)) {
		t.Fatalf("counted once, never skipped, got %+v", result)
	}
	if !bytes.Equal(dst.objects[bk("house-bucket", "foreign")], good) {
		t.Fatalf("dest must now equal the source bytes")
	}
	if dst.callsTo("Put") != 1 {
		t.Fatalf("exactly one Put expected, got %d", dst.callsTo("Put"))
	}
}

// CONCERN-2: the DEST-side hash branch. The source is CORRECT (matches the
// manifest sha) but the destination returns WRONG bytes on the post-copy
// re-read: the dest checksum check must fire (not the source check).
func TestMoveOutChunkDestReadBackCorruptionAbortsOnDestChecksum(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	good := []byte("correct source!!")
	src.objects[bk("byo-bucket", "k")] = good
	// Same length so only the dest HASH check can catch it.
	dst.corruptRead = map[string][]byte{bk("house-bucket", "k"): []byte("corrupt readback")}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "k", SHA256: hashOf(good), SizeBytes: int64(len(good))}}, nil)
	if err == nil {
		t.Fatalf("a corrupt dest read-back must abort")
	}
	if !strings.Contains(err.Error(), "dest hash mismatch") {
		t.Fatalf("error must name the dest checksum mismatch, got %v", err)
	}
	if strings.Contains(err.Error(), "source hash mismatch") {
		t.Fatalf("the SOURCE check must not be the one that fired: %v", err)
	}
	if result.ObjectsDone != 0 {
		t.Fatalf("nothing verified, got %+v", result)
	}
}

// Nothing is ever deleted from the source: the walk only Gets/Stats it. The
// fake records every call; assert the source saw only Get (never Put, and the
// store has no Delete/Remove method to call at all).
func TestMoveOutChunkNeverWritesOrDeletesSource(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	x := []byte("xxxx")
	y := []byte("yyyyy")
	src.objects[bk("byo-bucket", "x")] = x
	src.objects[bk("byo-bucket", "y")] = y
	if _, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{
			{Key: "x", SHA256: hashOf(x), SizeBytes: 4},
			{Key: "y", SizeBytes: 5},
		}, nil); err != nil {
		t.Fatalf("copy: %v", err)
	}
	for _, c := range src.calls {
		if !strings.HasPrefix(c, "Get ") && !strings.HasPrefix(c, "Stat ") {
			t.Fatalf("source saw a non-read call: %q", c)
		}
	}
	if src.callsTo("Put") != 0 || len(src.objects) != 2 {
		t.Fatalf("source must be untouched, calls=%v", src.calls)
	}
}

// MISSING SOURCE (design s5): a LIST-driven manifest can race a customer-
// side delete. A genuine NoSuchKey is RECORDED (counted + listed), never an
// abort, and NEVER counts as copied.
func TestMoveOutChunkRecordsMissingSource(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	keep := []byte("kept")
	src.objects[bk("byo-bucket", "kept")] = keep
	src.getErr[bk("byo-bucket", "deleted-by-customer")] = missingSourceErr("deleted-by-customer")
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{
			{Key: "deleted-by-customer"},
			{Key: "kept", SHA256: hashOf(keep), SizeBytes: int64(len(keep))},
		}, nil)
	if err != nil {
		t.Fatalf("a missing source key must never abort a move-out: %v", err)
	}
	if result.MissingCount != 1 || result.MissingSourceKeys != nil && len(result.MissingSourceKeys) != 1 {
		t.Fatalf("the missing key must be counted and listed, got %+v", result)
	}
	if result.ObjectsDone != 1 || result.BytesDone != int64(len(keep)) {
		t.Fatalf("a skipped key must not count as copied, got %+v", result)
	}
}

// CHECKSUM MISMATCH (the manifest DID carry a sha and the source holds
// different bytes): still a hard failure - the move-out never papers over
// a corrupt object.
func TestMoveOutChunkAbortsOnChecksumMismatch(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.objects[bk("byo-bucket", "corrupt")] = []byte("actual bytes")
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "corrupt", SHA256: hashOf([]byte("not the actual bytes")), SizeBytes: 12}}, nil)
	if err == nil {
		t.Fatalf("a sha mismatch must abort the chunk")
	}
	if result.ObjectsDone != 0 {
		t.Fatalf("nothing verified, got %+v", result)
	}
}

// CANCEL (POST /move-out/cancel): the API flips the chunk rows to
// CANCELLED and the worker's ctx is cancelled - the walk must stop at the
// next object boundary, keep the partial tally, and never write a source
// object. Nothing is deleted by the runner either way.
func TestMoveOutChunkStopsOnCancel(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	a := []byte("aaaa")
	b := []byte("bbbbbbbb")
	src.objects[bk("byo-bucket", "a")] = a
	src.objects[bk("byo-bucket", "b")] = b
	objs := []StorageMigrationObject{
		{Key: "a", SHA256: hashOf(a), SizeBytes: int64(len(a))},
		{Key: "b", SHA256: hashOf(b), SizeBytes: int64(len(b))},
	}
	ctx, cancel := context.WithCancel(context.Background())
	result, err := RunStorageMoveOutChunk(ctx, src, dst, "byo-bucket", "house-bucket", objs,
		func(r StorageMigrationResult) {
			// The progress callback is where the API's cancel lands (the
			// worker's own ctx is cancelled by the task's terminal report).
			if r.ObjectsDone == 1 {
				cancel()
			}
		})
	if err == nil {
		t.Fatalf("a cancelled run must report the cancellation")
	}
	if result.ObjectsDone != 1 || result.BytesDone != int64(len(a)) {
		t.Fatalf("the run must stop after the first object, got %+v", result)
	}
	if dst.objects[bk("house-bucket", "b")] != nil {
		t.Fatalf("a cancelled run must not copy the rest, got %+v", result)
	}
}

// --- TEST-HARDEN-MOVE-OUT-SHA-LESS-GO (lane dell-1) ------------------------
// Coverage map for the sha-less (foreign, LIST-driven) move-out path: the
// alreadyAtDest false return, the chunk copy at part boundaries, the dest
// re-read, cancel between objects, and key-shape edges.

// fillOf builds a deterministic n-byte payload whose bytes vary, so a
// truncated copy changes the hash (a run of identical bytes would not).
func fillOf(n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = byte('a' + (i % 26))
	}
	return b
}

// EMPTY MANIFEST: a chunk that lists nothing is a clean no-op - no store
// touched, zero tally, no error.
func TestMoveOutChunkEmptyManifestReturnsZeroTally(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{}, nil)
	if err != nil {
		t.Fatalf("an empty chunk must be a clean no-op: %v", err)
	}
	if result.ObjectsDone != 0 || result.BytesDone != 0 || result.Skipped != 0 || result.MissingCount != 0 {
		t.Fatalf("empty chunk must tally zero, got %+v", result)
	}
	if len(src.calls) != 0 || len(dst.calls) != 0 {
		t.Fatalf("empty chunk must touch no store, calls src=%d dst=%d", len(src.calls), len(dst.calls))
	}
}

// SHA-LESS KEY ALREADY AT DEST WITH THE SAME SIZE AND THE SAME BYTES: the
// heart of FIX-GO-MOVE-OUT-ALREADY-AT-DEST. Existence + size proves nothing,
// so the entry is RE-COPIED (Put), counted ONCE, and still verified.
func TestMoveOutChunkShaLessIdenticalAtDestIsRecopiedCountedOnce(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("foreign key, bytes identical at dest")
	src.objects[bk("byo-bucket", "same")] = content
	dst.objects[bk("house-bucket", "same")] = content
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "same", SizeBytes: int64(len(content))}}, nil)
	if err != nil {
		t.Fatalf("must re-copy and verify, never skip: %v", err)
	}
	if result.Skipped != 0 || result.ObjectsDone != 1 || result.BytesDone != int64(len(content)) {
		t.Fatalf("counted exactly once, never skipped, got %+v", result)
	}
	if dst.callsTo("Put") != 1 {
		t.Fatalf("a sha-less entry at dest must still be Put once, got %d", dst.callsTo("Put"))
	}
	if !bytes.Equal(dst.objects[bk("house-bucket", "same")], content) {
		t.Fatalf("dest bytes must still equal the source")
	}
}

// SHA-LESS ZERO-BYTE OBJECT (an empty foreign file): must still copy (Put)
// and verify. The old skip fired on `obj.SizeBytes == 0` before comparing.
func TestMoveOutChunkShaLessZeroByteObjectCopiesAndVerifies(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.objects[bk("byo-bucket", "empty-note")] = []byte{}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "empty-note"}}, nil)
	if err != nil {
		t.Fatalf("an empty foreign object must copy and verify: %v", err)
	}
	if result.ObjectsDone != 1 || result.Skipped != 0 {
		t.Fatalf("empty object must be copied and counted, got %+v", result)
	}
	if dst.callsTo("Put") != 1 {
		t.Fatalf("exactly one Put expected, got %d", dst.callsTo("Put"))
	}
	if _, found := dst.objects[bk("house-bucket", "empty-note")]; !found {
		t.Fatalf("the dest object must exist (empty), got %+v", result)
	}
}

// SHA-LESS ZERO-BYTE OBJECT ALREADY AT DEST: still re-copied, never skipped.
func TestMoveOutChunkShaLessZeroByteAlreadyAtDestStillRecopied(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.objects[bk("byo-bucket", "empty")] = []byte{}
	dst.objects[bk("house-bucket", "empty")] = []byte{}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "empty"}}, nil)
	if err != nil {
		t.Fatalf("must re-copy an empty sha-less entry: %v", err)
	}
	if result.Skipped != 0 || result.ObjectsDone != 1 {
		t.Fatalf("an empty sha-less entry must never skip, got %+v", result)
	}
	if dst.callsTo("Put") != 1 {
		t.Fatalf("expected exactly one Put, got %d", dst.callsTo("Put"))
	}
}

// SIZE DRIFT: the manifest is a live bucket LIST, so the size stamped at
// LIST time can differ from the bytes the source serves at read time (a
// customer overwrite between LIST and copy). The run must either verify the
// bytes that actually came out of the source, or abort NAMING the dest size
// check - never a silent pass with a wrong tally.
func TestMoveOutChunkShaLessSizeChangedBetweenListAndRead(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.objects[bk("byo-bucket", "drift")] = []byte("twelve bytes") // 12 served
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "drift", SizeBytes: 8}}, nil) // LIST said 8
	if err != nil {
		if !strings.Contains(err.Error(), "dest size mismatch") {
			t.Fatalf("a size-drift failure must name the dest size check, got %v", err)
		}
		if result.ObjectsDone != 0 {
			t.Fatalf("an aborted drift copy must not count, got %+v", result)
		}
		return
	}
	if result.ObjectsDone != 1 {
		t.Fatalf("drift entry must be counted once, got %+v", result)
	}
	got := dst.objects[bk("house-bucket", "drift")]
	if got == nil || !bytes.Equal(got, []byte("twelve bytes")) {
		t.Fatalf("dest must hold the bytes the source actually served")
	}
}

// DEST PUT FAILS MID-CHUNK, then the retry succeeds: the failed key must NOT
// be counted (the abort path), and the retry must copy it exactly once and
// count it once.
func TestMoveOutChunkShaLessPutFailsThenRetryCountsOnce(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	body := []byte("foreign body bytes")
	src.objects[bk("byo-bucket", "f")] = body
	dst.putErr[bk("house-bucket", "f")] = errors.New("dest hiccup")
	objs := []StorageMigrationObject{{Key: "f", SizeBytes: int64(len(body))}}
	_, firstErr := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		objs, nil)
	if firstErr == nil {
		t.Fatalf("pass 1 must fail on the dest hiccup")
	}
	dst.putErr = map[string]error{}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		objs, nil)
	if err != nil {
		t.Fatalf("retry must complete the chunk: %v", err)
	}
	if result.ObjectsDone != 1 || result.Skipped != 0 || result.BytesDone != int64(len(body)) {
		t.Fatalf("retry counts the sha-less key once, got %+v", result)
	}
	if !bytes.Equal(dst.objects[bk("house-bucket", "f")], body) {
		t.Fatalf("dest must hold the source bytes after the retry")
	}
}

// DEST RE-READ RETURNS A SHORT READ: the dest object is shorter than the
// manifest size, so the dest SIZE check must fire and nothing counts.
func TestMoveOutChunkDestShortReadAbortsOnDestSizeCheck(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	good := []byte("full object bytes")
	src.objects[bk("byo-bucket", "s")] = good
	dst.corruptRead = map[string][]byte{bk("house-bucket", "s"): []byte("short")}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "s", SizeBytes: int64(len(good))}}, nil)
	if err == nil {
		t.Fatalf("a short dest read must abort")
	}
	if !strings.Contains(err.Error(), "dest size mismatch") {
		t.Fatalf("error must name the dest size check, got %v", err)
	}
	if result.ObjectsDone != 0 {
		t.Fatalf("nothing verified, got %+v", result)
	}
}

// CANCEL BETWEEN TWO OBJECTS (POST /move-out/cancel): the cancel lands in
// the progress callback after object 1; the walk must stop at the NEXT
// object boundary, keep the partial tally, and never half-count object 2.
func TestMoveOutChunkCancelBetweenObjectsNeverHalfCounts(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	one := []byte("first object")
	two := []byte("second object is longer")
	src.objects[bk("byo-bucket", "one")] = one
	src.objects[bk("byo-bucket", "two")] = two
	src.objects[bk("byo-bucket", "three")] = []byte("third")
	objs := []StorageMigrationObject{
		{Key: "one", SizeBytes: int64(len(one))}, // sha-less: always re-copied
		{Key: "two", SizeBytes: int64(len(two))},
		{Key: "three", SizeBytes: 5},
	}
	ctx, cancel := context.WithCancel(context.Background())
	result, err := RunStorageMoveOutChunk(ctx, src, dst, "byo-bucket", "house-bucket", objs,
		func(r StorageMigrationResult) {
			if r.ObjectsDone == 1 {
				cancel()
			}
		})
	if err == nil {
		t.Fatalf("a cancelled run must report the cancellation")
	}
	if result.ObjectsDone != 1 || result.BytesDone != int64(len(one)) {
		t.Fatalf("must stop after the first object with an exact tally, got %+v", result)
	}
	if dst.objects[bk("house-bucket", "two")] != nil {
		t.Fatalf("a cancelled run must not copy the next object, got %+v", result)
	}
	if dst.callsTo("Put") != 1 {
		t.Fatalf("exactly one Put before the cancel, got %d", dst.callsTo("Put"))
	}
}

// UNUSUAL KEY CHARACTERS (unicode, spaces, '..', leading slash): the dest
// key must be the SAME key string the manifest carried, under the dest
// bucket only - the runner must not rewrite, trim, or re-derive it.
func TestMoveOutChunkUnusualKeyCharactersLandVerbatimAtDest(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	oddKeys := []string{
		"café/résumé ☂.txt",
		"two spaces here.dat",
		"weird/../up.bin",
		"/leading-slash.bin",
	}
	objs := make([]StorageMigrationObject, 0, len(oddKeys))
	for _, k := range oddKeys {
		body := []byte("body for key " + k)
		src.objects[bk("byo-bucket", k)] = body
		objs = append(objs, StorageMigrationObject{Key: k, SizeBytes: int64(len(body))})
	}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		objs, nil)
	if err != nil {
		t.Fatalf("odd keys must copy verbatim: %v", err)
	}
	if result.ObjectsDone != len(oddKeys) {
		t.Fatalf("every odd key must be counted, got %+v", result)
	}
	for _, k := range oddKeys {
		want := []byte("body for key " + k)
		got := dst.objects[bk("house-bucket", k)]
		if got == nil || !bytes.Equal(got, want) {
			t.Fatalf("dest key must be exactly %q under the dest bucket, got %+v", k, result)
		}
	}
}

// DUPLICATE KEYS IN THE MANIFEST (a wire bug / a chunk that overlaps itself):
// a sha-less key must be copied and counted PER ENTRY (never skipped on the
// first copy's existence), so the chunk tally stays the full accounting the
// API's move-status sums across chunks.
func TestMoveOutChunkDuplicateShaLessKeysAreCopiedPerEntry(t *testing.T) {
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	body := []byte("duplicated key body")
	src.objects[bk("byo-bucket", "dup")] = body
	objs := []StorageMigrationObject{
		{Key: "dup", SizeBytes: int64(len(body))},
		{Key: "dup", SizeBytes: int64(len(body))},
	}
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		objs, nil)
	if err != nil {
		t.Fatalf("duplicate sha-less entries must both copy: %v", err)
	}
	if result.Skipped != 0 || result.ObjectsDone != 2 || result.BytesDone != 2*int64(len(body)) {
		t.Fatalf("each sha-less entry must copy and count, got %+v", result)
	}
	if dst.callsTo("Put") != 2 {
		t.Fatalf("expected two Puts (one per entry), got %d", dst.callsTo("Put"))
	}
	if !bytes.Equal(dst.objects[bk("house-bucket", "dup")], body) {
		t.Fatalf("dest must still hold the source bytes")
	}
}

// LARGE SHA-LESS OBJECT at the minio part boundary: exactly one part, then
// part+1 byte (the copy crosses into a second part). The payload varies per
// byte, so a copy truncated at the boundary changes the hash and must be
// caught by the dest re-read.
func checkLargeShaLessAtSize(t *testing.T, n int) {
	partPayload := fillOf(n)
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.objects[bk("byo-bucket", "big")] = partPayload
	result, err := RunStorageMoveOutChunk(context.Background(), src, dst, "byo-bucket", "house-bucket",
		[]StorageMigrationObject{{Key: "big", SizeBytes: int64(n)}}, nil)
	if err != nil {
		t.Fatalf("large sha-less copy at %d bytes must succeed: %v", n, err)
	}
	if result.ObjectsDone != 1 || result.BytesDone != int64(n) {
		t.Fatalf("large object counted once with its full size at %d, got %+v", n, result)
	}
	got := dst.objects[bk("house-bucket", "big")]
	if got == nil || !bytes.Equal(got, partPayload) {
		t.Fatalf("dest must hold all %d bytes exactly at the part boundary", n)
	}
	if hashOf(got) != hashOf(partPayload) {
		t.Fatalf("dest hash must match the source bytes at %d bytes", n)
	}
}

func TestMoveOutChunkShaLessLargeObjectAtPartBoundaryIsVerified(t *testing.T) {
	// minio-go's default part size: exactly one part, then part+1 byte so the
	// copy crosses into a second part.
	checkLargeShaLessAtSize(t, 5*1024*1024)
	checkLargeShaLessAtSize(t, 5*1024*1024+1)
}

// --- FIX-BYO-S3-ACTIVATION-SKIP-P2: four table cases (lazy source reads)
// The fake now models minio-go's LAZY GetObject (readErr): Get succeeds and
// the FIRST body Read carries the error, which is where a real missing
// source key surfaces (inside dst.Put). The skip fires ONLY for a source-
// side missing key; a NoSuchKey on the destination re-read stays a hard
// failure (DIAG second finding).

func TestSkipP2LazySourceNoSuchKeyIsSkipped(t *testing.T) {
	// (a) lazy source read fails with the typed NoSuchKey during the Put:
	// skipped, MissingCount 1, no error.
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.readErr[bk("src-bucket", "gone")] = missingSourceErr("gone")
	objs := []StorageMigrationObject{{Key: "gone", SHA256: "abc", SizeBytes: 5}}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst,
		"src-bucket", "dst-bucket", objs, nil)
	if err != nil {
		t.Fatalf("a lazy source NoSuchKey must be skipped, got: %v", err)
	}
	if result.MissingCount != 1 || result.ObjectsDone != 0 {
		t.Fatalf("expected MissingCount 1 / ObjectsDone 0, got %+v", result)
	}
	if len(result.MissingSourceKeys) != 1 || result.MissingSourceKeys[0] != "gone" {
		t.Fatalf("missing key not listed: %+v", result)
	}
	if _, ok := dst.objects[bk("dst-bucket", "gone")]; ok {
		t.Fatal("a skipped key must never be written to the dest")
	}
}

func TestSkipP2LazySourceFlattenedTextIsSkipped(t *testing.T) {
	// (b) the source read fails with the FLATTENED untyped server text
	// (no typed ErrorResponse in the chain - the xeon failure): skipped too.
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.readErr[bk("src-bucket", "flat")] = errors.New("The specified key does not exist.")
	objs := []StorageMigrationObject{{Key: "flat", SHA256: "abc", SizeBytes: 5}}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst,
		"src-bucket", "dst-bucket", objs, nil)
	if err != nil {
		t.Fatalf("the flattened missing-key text must also be skipped, got: %v", err)
	}
	if result.MissingCount != 1 || result.ObjectsDone != 0 {
		t.Fatalf("expected MissingCount 1 / ObjectsDone 0, got %+v", result)
	}
}

func TestSkipP2DestVerifyNoSuchKeyIsHardFailure(t *testing.T) {
	// (c) the copy succeeds but the destination verify get answers NoSuchKey:
	// hard failure, MissingCount 0, even with the skip flag on.
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	content := []byte("object content for dest miss")
	src.objects[bk("src-bucket", "obj")] = content
	dst.getErr[bk("dst-bucket", "obj")] = missingSourceErr("obj")
	objs := []StorageMigrationObject{
		{Key: "obj", SHA256: hashOf(content), SizeBytes: int64(len(content))},
	}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst,
		"src-bucket", "dst-bucket", objs, nil)
	if err == nil {
		t.Fatal("a NoSuchKey on the destination verify must abort")
	}
	if result.MissingCount != 0 {
		t.Fatalf("a dest-side miss must never be counted as skipped, got %+v", result)
	}
}

func TestSkipP2LazySourceAccessDeniedIsHardFailure(t *testing.T) {
	// (d) the source read fails with AccessDenied: hard failure, not a
	// missing key.
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.readErr[bk("src-bucket", "denied")] = minio.ErrorResponse{Code: "AccessDenied", Key: "denied"}
	objs := []StorageMigrationObject{{Key: "denied", SHA256: "abc", SizeBytes: 5}}
	result, err := RunStorageMigrationSkipMissing(context.Background(), src, dst,
		"src-bucket", "dst-bucket", objs, nil)
	if err == nil {
		t.Fatal("AccessDenied must abort even with the skip flag on")
	}
	if result.MissingCount != 0 {
		t.Fatalf("AccessDenied must not be counted as missing, got %+v", result)
	}
}

func TestSkipP2LazyMissingStillAbortsWhenFlagFalse(t *testing.T) {
	// with skipMissingSource=false both lazy missing shapes still fail.
	src := newFakeObjectStore()
	dst := newFakeObjectStore()
	src.readErr[bk("src-bucket", "typed")] = missingSourceErr("typed")
	src.readErr[bk("src-bucket", "flat")] = errors.New("The specified key does not exist.")
	objs := []StorageMigrationObject{
		{Key: "typed", SHA256: "abc", SizeBytes: 5},
		{Key: "flat", SHA256: "def", SizeBytes: 5},
	}
	for _, o := range objs {
		result, err := RunStorageMigration(context.Background(), src, dst,
			"src-bucket", "dst-bucket", []StorageMigrationObject{o}, nil)
		if err == nil {
			t.Fatalf("skipMissingSource=false must still abort on %s", o.Key)
		}
		if result.MissingCount != 0 {
			t.Fatalf("flag-off path must not skip, got %+v", result)
		}
	}
}
