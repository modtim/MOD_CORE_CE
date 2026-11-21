package executor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/minio/minio-go/v7"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// storage_migration.go — WS4.Q17.16 (BYO-S3 migrate-on-activate).
//
// Pure Go, no container: the worker streams every tenant object from the
// OLD backend straight to the NEW one via minio-go, verifying size+SHA-256
// on the destination by an independent re-read (owner decision A.6).
// Bounded memory: every object is a single streamed GetObject -> PutObject
// pass plus one streamed re-read — never buffered whole in RAM. Idempotent:
// a dest object whose size and stored sha256 metadata already match is
// skipped, so a re-run after a partial failure only copies what's still
// missing (owner decision A.5's "abort on first verify failure" implies a
// migration can be resumed by re-enqueueing the same task).

// StorageMigrationObject is transport.StorageMigrationObject — the wire
// shape decoded straight from the task payload. Reusing it here (rather
// than a parallel executor-local type) avoids a pointless conversion pass
// over a list that can run into the thousands.
type StorageMigrationObject = transport.StorageMigrationObject

// StorageMigrationResult is the running/final tally.
// FIX-BYO-MIGRATION-SKIP-MISSING: MissingSourceKeys/MissingCount are the
// opt-in skip report (only populated when the task asked for
// skip_missing_source). A skipped key NEVER counts in ObjectsDone/BytesDone
// — a skipped key is not a verified object. MissingCount keeps counting past
// the listed-key cap (storageMigrationMissingKeyCap).
type StorageMigrationResult struct {
	ObjectsDone       int
	BytesDone         int64
	Skipped           int
	MissingCount      int
	MissingSourceKeys []string
}

// storageMigrationMissingKeyCap is how many missing source keys the worker
// LISTS in the report (the counter is uncapped). The API echoes the same cap
// in the migration-status response.
const storageMigrationMissingKeyCap = 50

// FIX-BYO-S3-ACTIVATION-SKIP-P1: sourceSideErr marks an error as produced by
// the SOURCE store (Get, or a read of the source body during the put). ONLY a
// marked error may ever be skipped when the activation opted in; a NoSuchKey
// on the DESTINATION re-read (verify) is never marked and stays a hard
// failure (DIAG second finding).
// sourceSideErr is the marker: an error wrapped (via %w) in one of these was
// produced by the SOURCE store. errors.As finds it through the fmt.Errorf
// chain; a destination-side error is never wrapped in one.
type sourceSideErr struct {
	err error
}

func (e sourceSideErr) Error() string { return e.err.Error() }

// missingKeyAnswer is true for a genuine "this key is not in the store"
// answer: the typed NoSuchKey ErrorResponse, OR the flattened server text
// ("The specified key does not exist") which minio-go can surface with NO
// typed error in the chain (the xeon failure). Callers must only ever ask
// this about a SOURCE error.
func missingKeyAnswer(err error) bool {
	if err == nil {
		return false
	}
	var resp minio.ErrorResponse
	if errors.As(err, &resp) {
		return resp.Code == "NoSuchKey"
	}
	return strings.Contains(strings.ToLower(err.Error()), "the specified key does not exist")
}

// isMissingSourceError is true only for a genuine "this key is not in the
// source store" answer AND only when the error was produced on the SOURCE
// side (marked by copyAndVerifyOneWith). minio-go's GetObject is LAZY, so a
// missing source key usually surfaces later, inside dst.Put reading the
// TeeReader — srcReadErr below records it and the put path rewraps it as a
// marked source error, whatever minio-go does to the error's type or text.
// Anything else (auth, network, a corrupt bucket, or a NoSuchKey on the
// destination re-read) is NOT a missing source key and must keep aborting.
func isMissingSourceError(err error) bool {
	if err == nil {
		return false
	}
	var m sourceSideErr
	if !errors.As(err, &m) {
		return false
	}
	return missingKeyAnswer(m.err)
}

// srcReadErr wraps the source reader and records the FIRST non-EOF error the
// SOURCE read returned (minio-go's lazy GetObject: a missing key surfaces
// here, inside the put's read of the body, not at Get time).
type srcReadErr struct {
	r   io.Reader
	err error
}

func (s *srcReadErr) Read(p []byte) (int, error) {
	n, err := s.r.Read(p)
	if err != nil && err != io.EOF && s.err == nil {
		s.err = err
	}
	return n, err
}

// StorageMigrationProgressFunc is invoked after each object (copied or
// skipped) completes. Best-effort on the caller's side — a failed progress
// report must never abort the migration.
type StorageMigrationProgressFunc func(result StorageMigrationResult)

// objectStore is the minimal S3 surface storage migration needs. Kept as an
// interface (not *minio.Client directly) so unit tests can substitute an
// in-memory fake instead of talking to a real S3-compatible server.
type objectStore interface {
	// Get opens a streaming reader for bucket/key. Caller must Close it.
	Get(ctx context.Context, bucket, key string) (io.ReadCloser, error)
	// Put streams size bytes from r into bucket/key, tagging it with a
	// sha256 user-metadata header (used for the idempotent skip-check).
	Put(ctx context.Context, bucket, key string, r io.Reader, size int64, sha256Hex string) error
	// Stat returns (size, sha256 metadata, found).
	Stat(ctx context.Context, bucket, key string) (size int64, sha256Hex string, found bool)
}

// minioObjectStore adapts a real minio-go client to objectStore.
type minioObjectStore struct {
	client *minio.Client
}

// storageMigrationSHA256MetaKey is the user-metadata key the migration
// copier stamps on every object it writes (arrives back from the server as
// the "X-Amz-Meta-Sha256" header). Used only for the idempotent skip-check —
// the actual verification always re-reads and re-hashes the object body.
const storageMigrationSHA256MetaKey = "Sha256"

// NewMinioObjectStore builds a real S3-backed objectStore from worker
// credentials (the same MinIOCredentials shape every other sidecar path
// uses — see s3ClientFor in native_staging.go).
func NewMinioObjectStore(creds transport.MinIOCredentials) (objectStore, error) {
	client, err := s3ClientFor(creds)
	if err != nil {
		return nil, err
	}
	return &minioObjectStore{client: client}, nil
}

func (m *minioObjectStore) Get(ctx context.Context, bucket, key string) (io.ReadCloser, error) {
	obj, err := m.client.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	return obj, nil
}

func (m *minioObjectStore) Put(ctx context.Context, bucket, key string, r io.Reader, size int64, sha256Hex string) error {
	_, err := m.client.PutObject(ctx, bucket, key, r, size, minio.PutObjectOptions{
		UserMetadata: map[string]string{storageMigrationSHA256MetaKey: sha256Hex},
	})
	return err
}

func (m *minioObjectStore) Stat(ctx context.Context, bucket, key string) (int64, string, bool) {
	info, err := m.client.StatObject(ctx, bucket, key, minio.StatObjectOptions{})
	if err != nil {
		return 0, "", false
	}
	sha := info.UserMetadata[storageMigrationSHA256MetaKey]
	if sha == "" && info.Metadata != nil {
		sha = info.Metadata.Get("X-Amz-Meta-Sha256")
	}
	return info.Size, sha, true
}

// alreadyAtDest is the resume/idempotency check for one manifest entry: is
// this object already verified at the destination, so the copy can be
// skipped?
//
//   - the manifest carries an expected sha256 (migrate-on-activate, and any
//     move-out entry that maps to a content_blobs row): size + stamped sha
//     must both match, the rule FIX-BYO-MIGRATION-SKIP-MISSING's resume
//     story has always used.
//   - the manifest carries NO sha (the move-out manifest is a bucket LIST, so
//     a FOREIGN key MOD never knew has no content_blobs row): never skip.
//     A key's existence (even with a matching size) proves nothing about its
//     bytes, and a skipped key is counted as a verified copy that later lets
//     the customer's bucket be deactivated. Sha-less entries are therefore
//     ALWAYS re-copied and verified (dst.Put overwrites idempotently).
//
// The opts.CompareWhenUnspecified knob is no longer consulted here; it still
// drives the post-copy comparison in copyAndVerifyOneWith.
func alreadyAtDest(obj StorageMigrationObject, size int64, sha string, opts runMigrationOpts) bool {
	if obj.SHA256 != "" {
		return size == obj.SizeBytes && strings.EqualFold(sha, obj.SHA256)
	}
	return false
}

// RunStorageMigration copies every object from (srcBucket on src) to
// (dstBucket on dst), verifying size+SHA-256 on the destination by an
// independent re-read, and reporting progress after each object. It aborts
// on the FIRST verify (or copy) failure (A.5) — a partial migration is
// safer to resume (re-enqueue; already-matching dest objects are skipped)
// than to paper over with a silently-wrong active connection.
func RunStorageMigration(
	ctx context.Context,
	src, dst objectStore,
	srcBucket, dstBucket string,
	objects []StorageMigrationObject,
	onProgress StorageMigrationProgressFunc,
) (StorageMigrationResult, error) {
	return runMigration(ctx, src, dst, srcBucket, dstBucket, objects, onProgress, false)
}

// RunStorageMigrationSkipMissing is the OPT-IN variant (FIX-BYO-MIGRATION-
// SKIP-MISSING). PROOF-BOX-J-2 F1: dangling content_blobs rows (a source
// object that no longer exists) aborted the whole migration on the FIRST
// such key, leaving migrated_object_count 0 and the connection inactive,
// with no way to finish a migration whose store was partly wiped.
//
// Only a genuine NoSuchKey on the SOURCE is skipped; every other error
// (auth, network, a corrupt bucket, a dest verify mismatch) still aborts
// exactly as before. A skipped key is counted (MissingCount, uncapped) and
// listed (MissingSourceKeys, capped at storageMigrationMissingKeyCap) and
// NEVER counts as copied/verified: ObjectsDone/BytesDone stay exactly what
// the real copies produced, so the API's "did we migrate everything" check
// still holds once it subtracts the reported skips.
func RunStorageMigrationSkipMissing(
	ctx context.Context,
	src, dst objectStore,
	srcBucket, dstBucket string,
	objects []StorageMigrationObject,
	onProgress StorageMigrationProgressFunc,
) (StorageMigrationResult, error) {
	return runMigration(ctx, src, dst, srcBucket, dstBucket, objects, onProgress, true)
}

// RunStorageMoveOutChunk is IMPL-BYO-MOVE-OUT-B-GO-WORKER: one CHUNK of a
// BYO move-out (design s2/s5). Same copy+verify walk, three move-out rules:
//   - RESUMABLE: an object already verified at the destination is skipped
//     (alreadyAtDest), so re-enqueueing a failed chunk only copies what is
//     still missing — the design's idempotent retry.
//   - A MISSING SOURCE KEY IS RECORDED, NEVER AN ABORT (design s5: the
//     manifest is a live bucket LIST, so a customer-side delete between the
//     LIST and the copy is expected fallout, not a failure). skipMissingSource
//     is therefore always true here; the report rides back in the same
//     storage_migration_report field the API's complete_move_out hook reads.
//   - FOREIGN KEYS (no content_blobs row, hence no sha in the manifest) are
//     verified against the bytes that actually came out of the source
//     (compareWhenUnspecified).
//
// The source object is NEVER deleted or truncated — the move only ever
// writes to the destination (soft-delete rule; clean-up is the operator's
// decision).
func RunStorageMoveOutChunk(
	ctx context.Context,
	src, dst objectStore,
	srcBucket, dstBucket string,
	objects []StorageMigrationObject,
	onProgress StorageMigrationProgressFunc,
) (StorageMigrationResult, error) {
	return runMigrationWith(ctx, src, dst, srcBucket, dstBucket, objects, onProgress,
		// A move-out always records (never aborts) a missing source key.
		true, runMigrationOpts{
			// A move-out always compares a sha-less entry against the source.
			CompareWhenUnspecified: true,
		})
}

// runMigrationOpts bundles the two verification knobs so the two public
// entry points (activate / move-out) stay one-liners and every call site
// names what it opted into.
type runMigrationOpts struct {
	// CompareWhenUnspecified: when the manifest carries no expected sha256
	// for a key, compare the destination re-read against the bytes that
	// actually came out of the SOURCE (move-out: foreign keys in a customer
	// bucket have no content_blobs row, hence no sha). False on the
	// migrate-on-activate path — behaviour unchanged there.
	CompareWhenUnspecified bool
}

func runMigration(
	ctx context.Context,
	src, dst objectStore,
	srcBucket, dstBucket string,
	objects []StorageMigrationObject,
	onProgress StorageMigrationProgressFunc,
	skipMissingSource bool,
) (StorageMigrationResult, error) {
	return runMigrationWith(ctx, src, dst, srcBucket, dstBucket, objects, onProgress,
		skipMissingSource, runMigrationOpts{
			// IMPL-BYO-MOVE-OUT-B-GO-WORKER: the activate path never compares
			// when the manifest leaves the sha unspecified (unchanged behaviour).
			CompareWhenUnspecified: false,
		})
}

// runMigrationWith is the shared walk. skipMissingSource is the activate
// opt-in (FIX-BYO-MIGRATION-SKIP-MISSING) and, on the move-out path, always
// true (design s5: a LIST-driven manifest can race a customer-side delete,
// and a missing source key is RECORDED, never an abort). compareWhenUnspecified
// is the move-out verification rule for keys the manifest carries no sha for
// (foreign files in a customer bucket).
func runMigrationWith(
	ctx context.Context,
	src, dst objectStore,
	srcBucket, dstBucket string,
	objects []StorageMigrationObject,
	onProgress StorageMigrationProgressFunc,
	skipMissingSource bool,
	opts runMigrationOpts,
) (StorageMigrationResult, error) {
	var result StorageMigrationResult
	for _, obj := range objects {
		if err := ctx.Err(); err != nil {
			return result, err
		}

		// IMPL-BYO-MOVE-OUT-B-GO-WORKER: the resume/idempotency check moves
		// into alreadyAtDest so the move-out path (manifest entries with no
		// size and no sha, straight from the bucket LIST) can skip on key
		// existence, while the activate path keeps its old size+sha rule.
		if size, sha, found := dst.Stat(ctx, dstBucket, obj.Key); found &&
			alreadyAtDest(obj, size, sha, opts) {
			result.Skipped++
			result.ObjectsDone++
			result.BytesDone += obj.SizeBytes
			if onProgress != nil {
				onProgress(result)
			}
			// IMPL-BYO-MOVE-OUT-B-GO-WORKER: a skipped (already-verified) key
			// still counts toward ObjectsDone/BytesDone — the API's move-status
			// sums chunks and needs the chunk's full accounting, exactly as the
			// activate path has always done.
			continue
		}

		if err := copyAndVerifyOneWith(ctx, src, dst, srcBucket, dstBucket, obj,
			// IMPL-BYO-MOVE-OUT-B-GO-WORKER: compare the dest re-read against
			// the source bytes when the LIST-driven manifest carries no sha.
			// False on the activate path (unchanged).
			(opts.CompareWhenUnspecified)); err != nil {
			// Only a missing SOURCE key is ever skippable, and only when the
			// activation opted in. Everything else keeps A.5's abort.
			if skipMissingSource && isMissingSourceError(err) {
				result.MissingCount++
				if len(result.MissingSourceKeys) < storageMigrationMissingKeyCap {
					result.MissingSourceKeys = append(result.MissingSourceKeys, obj.Key)
				}
				continue
			}
			return result, fmt.Errorf("object %s: %w", obj.Key, err)
		}
		result.ObjectsDone++
		result.BytesDone += obj.SizeBytes
		if onProgress != nil {
			onProgress(result)
		}
	}
	return result, nil
}

// copyAndVerifyOne streams ONE object source -> dest with bounded memory
// (io.TeeReader hashes the exact bytes uploaded, never buffering the whole
// object), then independently re-reads the destination to verify size +
// SHA-256 — the "re-read is safest" verification A.6 asked for.
func copyAndVerifyOne(ctx context.Context, src, dst objectStore, srcBucket, dstBucket string, obj StorageMigrationObject) error {
	return copyAndVerifyOneWith(ctx, src, dst, srcBucket, dstBucket, obj, false)
}

// copyAndVerifyOneWith is copyAndVerifyOne plus the move-out rule: when the
// manifest carries no expected sha256 (a LIST-driven move-out can include
// FOREIGN keys the tenant put in their own bucket — no content_blobs row,
// therefore no sha), verification compares the destination re-read against
// the bytes we actually streamed out of the source instead of against a
// manifest value. compareWhenUnspecified is false on the migrate-on-activate
// path (behaviour unchanged) and true on the move-out path.
func copyAndVerifyOneWith(
	ctx context.Context, src, dst objectStore, srcBucket, dstBucket string,
	obj StorageMigrationObject, compareWhenUnspecified bool,
) error {
	r, err := src.Get(ctx, srcBucket, obj.Key)
	if err != nil {
		// FIX-BYO-S3-ACTIVATION-SKIP-P1: marked as source-side so the skip
		// can tell this apart from a destination miss.
		return fmt.Errorf("get source: %w", sourceSideErr{err: err})
	}
	srcHash := sha256.New()
	// FIX-BYO-S3-ACTIVATION-SKIP-P1: minio-go's GetObject is LAZY — a missing
	// source key surfaces while the put reads the body. srcReadErr records the
	// first such error so the put path can rewrap it as a marked SOURCE error,
	// whatever minio-go does to the error's type or text on the way out.
	sr := srcReadErr{r: r}
	tee := io.TeeReader(&sr, srcHash)
	putErr := dst.Put(ctx, dstBucket, obj.Key, tee, obj.SizeBytes, obj.SHA256)
	closeErr := r.Close()
	// FIX-BYO-S3-ACTIVATION-SKIP-P1: a source-read error that surfaced during
	// the put (lazy GetObject) is re-wrapped as a marked SOURCE error so the
	// loop's skip can tell it apart from a real dest-side failure. A put error
	// with NO source-read error behind it (a genuine dest-side failure) keeps
	// aborting even when the flag is on.
	if putErr != nil {
		if sr.err != nil && missingKeyAnswer(sr.err) {
			return fmt.Errorf("get source: %w", sourceSideErr{err: sr.err})
		}
		return fmt.Errorf("put dest: %w", putErr)
	}
	if closeErr != nil {
		return fmt.Errorf("close source: %w", closeErr)
	}
	gotSrc := hex.EncodeToString(srcHash.Sum(nil))
	if obj.SHA256 != "" && !strings.EqualFold(gotSrc, obj.SHA256) {
		return fmt.Errorf("source hash mismatch: want %s got %s", obj.SHA256, gotSrc)
	}

	dr, err := dst.Get(ctx, dstBucket, obj.Key)
	if err != nil {
		return fmt.Errorf("verify: get dest: %w", err)
	}
	defer dr.Close()
	dstHash := sha256.New()
	n, err := io.Copy(dstHash, dr)
	if err != nil {
		return fmt.Errorf("verify: read dest: %w", err)
	}
	if obj.SizeBytes > 0 && n != obj.SizeBytes {
		return fmt.Errorf("verify: dest size mismatch: want %d got %d", obj.SizeBytes, n)
	}
	gotDst := hex.EncodeToString(dstHash.Sum(nil))
	if obj.SHA256 != "" {
		if !strings.EqualFold(gotDst, obj.SHA256) {
			return fmt.Errorf("verify: dest hash mismatch: want %s got %s", obj.SHA256, gotDst)
		}
	} else if compareWhenUnspecified && !strings.EqualFold(gotDst, gotSrc) {
		return fmt.Errorf("verify: dest hash mismatch: want %s got %s (no expected sha in manifest)", gotSrc, gotDst)
	}
	return nil
}
