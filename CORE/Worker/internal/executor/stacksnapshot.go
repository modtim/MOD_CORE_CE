package executor

// add-persistent-stacks §3: the volume snapshot/restore-to-MinIO primitive — the
// backbone for both backup and cross-worker migration.
//
// snapshot: `docker run -v <vol>:/data alpine tar czf - .` → stream to MinIO PutObject.
// restore:  MinIO GetObject → pipe into `docker run -i -v <vol>:/data alpine tar xzf -`.
//
// v1 uses the embedded worker's MinIO root creds from env (MINIO_INTERNAL_URL +
// MINIO_ROOT_USER/PASSWORD). Remote workers will use per-task STS creds — a
// follow-up; this keeps the primitive proven on the control-server worker first.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path"
	"strconv"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/storage"
)

func minioClient() (*minio.Client, error) {
	endpoint, secure := storage.Endpoint()
	ak, sk, ok := storage.RootCredentials()
	if !ok {
		return nil, fmt.Errorf("snapshot needs MinIO creds (MINIO_ROOT_USER/PASSWORD); only the embedded worker has them in v1")
	}
	opts := &minio.Options{
		Creds:  credentials.NewStaticV4(ak, sk, ""),
		Secure: secure,
	}
	// add-multi-region-mesh §2.4 — secure cross-region transfers: when the MinIO
	// endpoint is TLS, trust the mesh CA and present a client cert (mTLS) if the
	// Vault-Agent-templated cert files are mounted. Inert on the plain-HTTP bed.
	if secure {
		tr, err := minioTLSTransport()
		if err != nil {
			return nil, err
		}
		if tr != nil {
			opts.Transport = tr
		}
	}
	return minio.New(endpoint, opts)
}

// minioTLSTransport builds an HTTP transport that trusts the mesh CA and, when a
// client cert/key pair is present, presents it for mutual TLS. Cert material is
// read from files (MINIO_CA_CERT / MINIO_CLIENT_CERT / MINIO_CLIENT_KEY) that a
// Vault Agent templates onto disk — so certs come from Vault, never baked in.
// Returns (nil, nil) when no custom material is configured, so the minio client
// falls back to the system trust store.
func minioTLSTransport() (http.RoundTripper, error) {
	caPath := os.Getenv("S3_CA_CERT")
	if caPath == "" {
		caPath = os.Getenv("MINIO_CA_CERT")
	}
	certPath := os.Getenv("S3_CLIENT_CERT")
	if certPath == "" {
		certPath = os.Getenv("MINIO_CLIENT_CERT")
	}
	keyPath := os.Getenv("S3_CLIENT_KEY")
	if keyPath == "" {
		keyPath = os.Getenv("MINIO_CLIENT_KEY")
	}
	skipVerify := os.Getenv("S3_TLS_SKIP_VERIFY") == "true" || os.Getenv("MINIO_TLS_SKIP_VERIFY") == "true"
	if caPath == "" && certPath == "" && keyPath == "" && !skipVerify {
		return nil, nil // nothing custom — use defaults
	}
	// BYO S3 endpoints may speak TLS 1.2 only: the worker API client pin to 1.3 does not apply here.
	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if caPath != "" {
		pem, err := os.ReadFile(caPath)
		if err != nil {
			return nil, fmt.Errorf("read MINIO_CA_CERT %q: %w", caPath, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("MINIO_CA_CERT %q: no certificates parsed", caPath)
		}
		tlsCfg.RootCAs = pool
	}
	if certPath != "" || keyPath != "" {
		if certPath == "" || keyPath == "" {
			return nil, fmt.Errorf("mTLS needs BOTH MINIO_CLIENT_CERT and MINIO_CLIENT_KEY")
		}
		pair, err := tls.LoadX509KeyPair(certPath, keyPath)
		if err != nil {
			return nil, fmt.Errorf("load MinIO client cert (mTLS): %w", err)
		}
		tlsCfg.Certificates = []tls.Certificate{pair}
	}
	tlsCfg.InsecureSkipVerify = skipVerify
	return &http.Transport{
		Proxy:             http.ProxyFromEnvironment,
		ForceAttemptHTTP2: true,
		TLSClientConfig:   tlsCfg,
	}, nil
}

func ensureBucket(ctx context.Context, c *minio.Client, bucket string) error {
	ok, err := c.BucketExists(ctx, bucket)
	if err != nil {
		return err
	}
	if !ok {
		return c.MakeBucket(ctx, bucket, minio.MakeBucketOptions{})
	}
	return nil
}

// snapshotVolume tars a docker volume and streams it to MinIO. Returns bytes written.
func (e *Executor) snapshotVolume(ctx context.Context, volume, bucket, key string) (int64, error) {
	c, err := minioClient()
	if err != nil {
		return 0, err
	}
	if err := ensureBucket(ctx, c, bucket); err != nil {
		return 0, fmt.Errorf("ensure bucket: %w", err)
	}
	// Runs through the execCommandContext seam (not exec.CommandContext
	// directly) so the snapshot's tar can be faked in unit tests exactly like
	// every other docker shell-out in this package — the freeze/unfreeze
	// order tests in stacksnapshot_freeze_test.go depend on it.
	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-v", volume+":/data:ro",
		"alpine", "tar", "czf", "-", "-C", "/data", ".")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return 0, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	info, putErr := c.PutObject(ctx, bucket, key, stdout, -1, minio.PutObjectOptions{
		ContentType: "application/gzip",
	})
	waitErr := cmd.Wait()
	if waitErr != nil {
		return 0, fmt.Errorf("tar volume %s: %v: %s", volume, waitErr, stderr.String())
	}
	if putErr != nil {
		return 0, fmt.Errorf("put snapshot: %w", putErr)
	}
	return info.Size, nil
}

// migrateStack performs a blue/green move (add-persistent-stacks §5): for each
// base volume, snapshot {oldProject}_{vol} -> MinIO -> restore into
// {newProject}_{vol}; bring the new project up (volumes pre-populated); then drain
// the old project. If the new instance fails to come up the old one is left
// running (no cutover), so a failed migrate is safe.
func (e *Executor) migrateStack(ctx context.Context, oldProject, newProject string,
	volumes []string, bucket, composePath string, freeze bool, extraEnv, secrets []string) (string, error) {
	if bucket == "" {
		bucket = "stack-snapshots"
	}
	// §5.2 write-freeze: stop the old generation before snapshotting so no write
	// is missed during the migrate window (brief downtime, write-heavy stacks).
	if freeze {
		if out, err := e.composeCmd(ctx, Callbacks{}, oldProject, "", nil, "stop"); err != nil {
			return out, fmt.Errorf("freeze (stop old generation) failed: %w\n%s", err, out)
		}
	}
	for _, v := range volumes {
		oldVol := oldProject + "_" + v
		newVol := newProject + "_" + v
		key := fmt.Sprintf("%s/migrate/%s/%s.tar.gz", oldProject, newProject, v)
		if _, err := e.snapshotVolume(ctx, oldVol, bucket, key); err != nil {
			return "", fmt.Errorf("snapshot %s: %w", oldVol, err)
		}
		if err := e.restoreVolume(ctx, newVol, bucket, key, 0); err != nil {
			return "", fmt.Errorf("restore %s: %w", newVol, err)
		}
	}
	// Bring the new generation up with the restored volumes. GX.S10b — pass the
	// MOD_SECRETS_DIR / MOD_FILES_DIR env and the secret values for redaction,
	// so a stack carrying file secrets migrates identically to a fresh deploy.
	out, err := e.composeCmdRedacted(ctx, Callbacks{}, newProject, composePath, secrets, extraEnv, "up", "-d", "--remove-orphans")
	if err != nil {
		return out, fmt.Errorf("compose up new generation: %w\n%s", err, out)
	}
	if spec, rerr := os.ReadFile(composePath); rerr == nil {
		if healed := e.healStackNetworks(ctx, Callbacks{}, newProject, string(spec)); len(healed) > 0 {
			out += "\n" + strings.Join(healed, "\n")
		}
	}
	// Cut over succeeded — drain the old generation (best-effort).
	down, derr := e.composeCmd(ctx, Callbacks{}, oldProject, "", nil, "down", "--remove-orphans", "-v")
	if derr != nil {
		return out + "\n[warn] old generation teardown failed: " + down, nil
	}
	// The old generation is gone — drop its materialized secret/file dirs so
	// nothing secret outlives the stack it belonged to.
	cleanupStackFiles(oldProject)
	return out + "\n" + down, nil
}

// rehearseSnapshot proves a snapshot is restorable WITHOUT touching the live
// stack (managed-ai-surfaces-and-stack-ops §9.4).
//
// An untested backup is a hypothesis, and the first restore attempt should not
// be during an incident. But the obvious way to test one — restore it — is the
// single most destructive thing you can do to a healthy stack, so this never
// goes near the real volume: it restores into a scratch volume, inspects what
// landed, and removes the scratch volume on every path out.
//
// It reports FAILURE when the snapshot extracts nothing. A tar that unpacks
// cleanly to an empty directory exits zero, so "the restore command succeeded"
// is not evidence of a usable backup — which is exactly the false confidence
// this exists to remove.
//
// Returns (files, bytes) recovered.
func (e *Executor) rehearseSnapshot(ctx context.Context, volume, bucket, key string, maxBytes int64) (int64, int64, error) {
	scratch := rehearsalVolumeName(volume)
	if scratch == volume {
		return 0, 0, fmt.Errorf("refusing to rehearse: scratch name collides with the live volume %q", volume)
	}

	// Always, on every path out. A rehearsal that leaves its scratch copy behind
	// silently doubles the disk cost of every stack it protects.
	defer func() {
		_ = exec.Command("docker", "volume", "rm", "-f", scratch).Run()
	}()

	// Start from nothing, so a previous rehearsal's leftovers cannot be counted
	// as this snapshot's contents.
	_ = exec.CommandContext(ctx, "docker", "volume", "rm", "-f", scratch).Run()

	if err := e.restoreVolume(ctx, scratch, bucket, key, 0); err != nil {
		return 0, 0, fmt.Errorf("rehearsal restore failed: %w", err)
	}

	files, bytes, err := inspectVolume(ctx, scratch)
	if err != nil {
		return 0, 0, fmt.Errorf("rehearsal could not inspect the restored data: %w", err)
	}
	if files == 0 {
		return 0, 0, fmt.Errorf("rehearsal restored 0 files from %s/%s — the snapshot "+
			"is present but empty, so this stack is NOT recoverable from it", bucket, key)
	}
	return files, bytes, nil
}

// rehearsalVolumeName is the scratch target for a rehearsal. Prefixed rather
// than suffixed so the name sorts away from real volumes in `docker volume ls`
// and cannot be mistaken for the stack's own data during an incident.
func rehearsalVolumeName(volume string) string {
	return "modrehearsal-" + volume
}

// inspectVolume counts regular files and total bytes in a volume.
func inspectVolume(ctx context.Context, volume string) (int64, int64, error) {
	cmd := exec.CommandContext(ctx, "docker", "run", "--rm", "-v", volume+":/data:ro",
		"alpine", "sh", "-c",
		"find /data -type f | wc -l; find /data -type f -exec cat {} + 2>/dev/null | wc -c")
	var out, stderr bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return 0, 0, fmt.Errorf("%v: %s", err, stderr.String())
	}
	fields := strings.Fields(out.String())
	if len(fields) < 2 {
		return 0, 0, fmt.Errorf("unexpected inspect output %q", out.String())
	}
	files, err := strconv.ParseInt(fields[0], 10, 64)
	if err != nil {
		return 0, 0, fmt.Errorf("parse file count %q: %w", fields[0], err)
	}
	size, err := strconv.ParseInt(fields[1], 10, 64)
	if err != nil {
		return 0, 0, fmt.Errorf("parse byte count %q: %w", fields[1], err)
	}
	return files, size, nil
}

// restoreVolume downloads a snapshot from MinIO and extracts it into a docker volume.
// 30.6.1 (c1-J3a) — a SEEDED tar is attacker-supplied, so the worker validates the
// archive BEFORE anything touches the volume, in two passes:
//
//	pass 1: StatObject (keep the ETag) -> GetObject -> gunzip -> tar headers only.
//	        Every member is checked against the rules in validateTarStream and the
//	        uncompressed size is summed. Nothing is written anywhere.
//	pass 2: only if pass 1 was clean. GetObject with SetMatchETag(etag) so an object
//	        swapped between passes fails, then the SAME validation runs again while
//	        streaming, and a RE-ENCODED, uncompressed tar of only the validated
//	        entries is piped into `docker run -i ... tar xf - -C /data`.
//
// The docker-run idiom is kept because the worker may have no host access to the
// volume path; we never shell out to a host tar.
func (e *Executor) restoreVolume(ctx context.Context, volume, bucket, key string, maxBytes int64) error {
	c, err := minioClient()
	if err != nil {
		return err
	}
	if maxBytes <= 0 {
		maxBytes = DEFAULT_MAX_UNCOMPRESSED_BYTES
	}
	// Pass 1 — stat first so we can pin the ETag for pass 2.
	st, err := c.StatObject(ctx, bucket, key, minio.StatObjectOptions{})
	if err != nil {
		return err
	}
	etag := st.ETag
	// Pass 1: headers only, nothing written anywhere.
	obj, err := c.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return err
	}
	defer obj.Close()
	gz, err := gzip.NewReader(obj)
	if err != nil {
		return fmt.Errorf("restore pass 1: %w", err)
	}
	if _, _, err := validateTarStream(gz, maxBytes, nil); err != nil {
		_ = gz.Close()
		return err
	}
	_ = gz.Close()
	// Pass 2 — same object (ETag match), re-validated while streaming, re-encoded
	// into an uncompressed tar we control byte for byte.
	var sink bytes.Buffer
	w := tar.NewWriter(&sink)
	// SetMatchETag is the swap guard: if the object changed between the passes the
	// GET fails instead of validating one tar and extracting another.
	opts := minio.GetObjectOptions{}
	opts.SetMatchETag(etag)
	obj2, err := c.GetObject(ctx, bucket, key, opts)
	if err != nil {
		_ = w.Close()
		return err
	}
	defer obj2.Close()
	gz2, err := gzip.NewReader(obj2)
	if err != nil {
		_ = w.Close()
		return fmt.Errorf("restore pass 2: %w", err)
	}
	if _, _, err := validateTarStream(gz2, maxBytes, w); err != nil {
		_ = gz2.Close()
		_ = w.Close()
		return err
	}
	_ = gz2.Close()
	if err := w.Close(); err != nil {
		_ = w.Close()
		return err
	}
	tarball := sink.Bytes()
	// Make sure the target volume exists (idempotent).
	_ = exec.CommandContext(ctx, "docker", "volume", "create", volume).Run()
	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-i", "-v", volume+":/data",
		"alpine", "sh", "-c", "tar xf - -C /data")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	// The re-encoded tar is uncompressed, so `tar xf -` sniffs it; no gzip layer
	// is trusted on the way in.
	cmd.Stdin = bytes.NewBuffer(tarball)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("restore tar into %s: %v: %s", volume, err, stderr.String())
	}
	return nil
}

// ============================================================================
// 30.6.1 c1-J3a — the worker's own layer-2 archive validation (the API validates
// at commit; the worker MUST refuse on its own). A seeded tar is attacker
// supplied: `../`, absolute paths, symlink/hardlink escapes, device nodes and
// decompression bombs all come straight from the object.
// ============================================================================

// maxTarEntries caps the member count so a tar of a million one-byte members
// cannot be used to burn the worker's CPU/heap before the byte cap matters. It is
// a package var rather than a const ONLY so the count cap is unit-testable: the
// safe-extract tests lower it and restore it with t.Cleanup. Nothing else writes
// it, and the production value stays 1,000,000 (Lead decision 2, "entry count <=
// 1,000,000 (a const)").
var maxTarEntries int64 = 1000000

// DEFAULT_MAX_UNCOMPRESSED_BYTES is the worker's own bomb cap when the API does
// not send one — 512 GiB, matching the API default at
// CORE/API/services/stack_seed.py:74.
const DEFAULT_MAX_UNCOMPRESSED_BYTES = int64(512) << 30

// SETUID_SETGID_STICKY are the mode bits we strip from every re-encoded header:
// a seeded archive must not be able to hand the volume a setuid/setgid/sticky
// file. uid/gid are left as the archive states them.
const SETUID_SETGID_STICKY = 04000 | 02000 | 01000

// maxTrailer bounds the bytes left in the gzip stream AFTER the tar end-of-
// archive blocks. tar padding is at most 2 records (10240 bytes), so 1 MiB is
// generous; anything beyond it is trailing garbage or an appended member.
const maxTrailer = 1 << 20

// archiveRejected is the error shape the brief mandates: the RULE is named and
// the member name is only ever included truncated to 64 chars, so a hostile
// name cannot blow up the log line and never appears in full.
func archiveRejected(rule, name string) error {
	return fmt.Errorf("archive_rejected: %s %s", rule, trunc64(name))
}

// trunc64 bounds a member name before it reaches an error/log line.
func trunc64(s string) string {
	if len(s) <= 64 {
		return s
	}
	return s[:64]
}

// cleanName returns the cleaned member name, or "" when the name is unusable
// (empty, NUL, absolute, or escaping the root through a `..` component). Our own
// snapshots come from `tar czf - -C /data .`, so `./`, `./a`, `./d/` all clean to
// usable names (`.`, `a`, `d`) and MUST be accepted.
func cleanName(raw string) string {
	if raw == "" || strings.IndexByte(raw, 0) >= 0 {
		return ""
	}
	if raw[0] == '/' {
		return "" // absolute
	}
	n := path.Clean(raw)
	if n == "" || n == ".." || strings.HasSuffix(n, "/..") || strings.HasPrefix(n, "../") {
		return ""
	}
	if n == "." {
		return "."
	}
	return n
}

// insideRoot is true when a cleaned path stays at or under the archive root.
func insideRoot(n string) bool {
	return n == "." || (!strings.HasPrefix(n, "../") && n != ".." && n != "" && n[0] != '/')
}

// validateTarStream walks EVERY header in r, applies the rules, and sums the
// uncompressed bytes. When emit != nil it ALSO re-encodes, in order, only the
// validated entries into emit as an uncompressed tar (the bytes the volume will
// actually receive). Returns (entries, totalBytes, err). A nil emit = validate
// only (pass 1) — nothing is written anywhere on that path.
//
// Go's archive/tar surfaces PAX/kv metadata itself: tar.Next consumes
// TypeXGlobalHeader / TypePaxHeader / GNU TypeLongName records internally and
// applies them to the following Header, so they never reach this loop; the
// explicit skips below are defence in depth for readers that do surface them.
func validateTarStream(r io.Reader, maxBytes int64, emit *tar.Writer) (int64, int64, error) {
	if maxBytes <= 0 {
		maxBytes = DEFAULT_MAX_UNCOMPRESSED_BYTES
	}
	tr := tar.NewReader(r)
	entries := int64(0)
	total := int64(0)
	// symlinkNames are the members already seen that ARE symlinks; regularNames
	// are the earlier validated regular files a hardlink may point at.
	symlinkNames := map[string]string{}
	regularNames := map[string]bool{}
	for {
		hdr, err := tr.Next()
		if err != nil {
			if err == io.EOF {
				// tar stopped at the end-of-archive blocks, so the gzip trailer
				// was never read: Go's gzip reader checks CRC32 + ISIZE only when
				// it reaches EOF. Drain it here so a truncated/corrupted trailer or
				// trailing garbage (including an appended second gzip member) is
				// refused rather than accepted. The error text names no path/bytes.
				n, derr := io.Copy(io.Discard, io.LimitReader(r, maxTrailer+1))
				if derr != nil {
					return entries, total, fmt.Errorf("archive_rejected: trailer: %w", derr)
				}
				if n > maxTrailer {
					return entries, total, errors.New("archive_rejected: trailing data")
				}
				break
			}
			// A truncated/corrupt archive is refused, and on the emit path only
			// the entries validated BEFORE the corruption were already written.
			if emit == nil {
				return entries, total, fmt.Errorf("archive_rejected: corrupt_tar")
			}
			return entries, total, fmt.Errorf("archive_rejected: corrupt_tar")
		}
		if hdr.Typeflag == tar.TypeXGlobalHeader || hdr.Typeflag == tar.TypeXHeader ||
			hdr.Typeflag == tar.TypeGNULongName || hdr.Typeflag == tar.TypeGNULongLink {
			continue // metadata only — Go applies these itself; skip defensively
		}
		entries++
		if entries > maxTarEntries {
			return entries, total, archiveRejected("entry_count", hdr.Name)
		}
		name := cleanName(hdr.Name)
		if name == "" {
			return entries, total, archiveRejected("name", hdr.Name)
		}
		// Symlink parents: an entry must not pass THROUGH an earlier symlink, or
		// the extract follows the link out of the volume (l -> d, then l/../../x).
		// Every proper prefix of the cleaned name is checked.
		if p := symlinkParentPrefix(name, symlinkNames); p != "" {
			return entries, total, archiveRejected("symlink_parent", hdr.Name)
		}
		switch hdr.Typeflag {
		case tar.TypeReg, tar.TypeDir, tar.TypeSymlink, tar.TypeLink:
		default:
			// char/block devices, fifo, and anything else Go surfaces.
			return entries, total, archiveRejected("type", hdr.Name)
		}
		if hdr.Size < 0 || total+hdr.Size > maxBytes {
			return entries, total, archiveRejected("cap", hdr.Name)
		}
		total += hdr.Size
		if hdr.Typeflag == tar.TypeSymlink {
			tgt := hdr.Linkname
			if tgt == "" || strings.IndexByte(tgt, 0) >= 0 || tgt[0] == '/' {
				return entries, total, archiveRejected("symlink_target", hdr.Name)
			}
			joined := path.Clean(path.Join(path.Dir(name), tgt))
			if !insideRoot(joined) {
				return entries, total, archiveRejected("symlink_target", hdr.Name)
			}
			symlinkNames[name] = tgt
		} else if hdr.Typeflag == tar.TypeLink {
			tgt := cleanName(hdr.Linkname)
			if tgt == "" || !regularNames[tgt] {
				// A hardlink may only name an EARLIER validated regular file
				// (never a symlink, never a later member, never a path outside).
				return entries, total, archiveRejected("hardlink_target", hdr.Name)
			}
		} else if hdr.Typeflag == tar.TypeReg {
			regularNames[name] = true
		}
		if emit == nil {
			continue
		}
		// Build a FRESH header rather than editing a copy of hdr: tar.Header
		// caches the encoded block, so a shallow copy + field edits are ignored
		// by WriteHeader, and the copy would also smuggle the reader's PAXRecords
		// /Xattrs (attacker-supplied) into the re-encoded stream — tar.Writer
		// refuses basic PAX keys such as `path`, so a long-name archive would
		// fail to emit at all.
		// linkOut is filled below (Go forbids declaring it inside the branches).
		var linkOut string
		if hdr.Typeflag == tar.TypeSymlink {
			// already validated above: relative, NUL-free, stays inside the root.
			// Keep the ORIGINAL relative target (cleanName would rewrite it).
			linkOut = hdr.Linkname
		} else if hdr.Typeflag == tar.TypeLink {
			// the CLEAN validated target of an earlier regular file.
			linkOut = cleanName(hdr.Linkname)
		}
		// Strip setuid/setgid/sticky (uid/gid unchanged). Written as a subtract
		// of the masked bits rather than a ~ mask: this Go toolchain parses a
		// leading ~ as the type-constraint operator, so the arithmetic form is
		// the one that compiles identically on every version.
		perm := hdr.Mode - (hdr.Mode & int64(SETUID_SETGID_STICKY))
		// Only regular files carry body bytes; every other type emits Size 0.
		sizeOut := int64(0)
		if hdr.Typeflag == tar.TypeReg {
			// hdr.Size was validated >= 0 and within the cap above.
			sizeOut = hdr.Size
		}
		// Fresh header: no PAXRecords, no Xattrs, no Uname/Gname, FormatPAX.
		// uid/gid and mtime are preserved (they are not attacker path data).
		out := tar.Header{
			Typeflag: hdr.Typeflag,
			Name:     name,
			Linkname: linkOut,
			Size:     sizeOut,
			Mode:     perm,
			Uid:      hdr.Uid,
			Gid:      hdr.Gid,
			ModTime:  hdr.ModTime,
			Format:   tar.FormatPAX,
		}
		if out.Typeflag == tar.TypeDir && !strings.HasSuffix(out.Name, "/") {
			out.Name += "/"
		}
		if err := emit.WriteHeader(&out); err != nil {
			return entries, total, fmt.Errorf("archive_rejected: emit: %w", err)
		}
		if hdr.Typeflag == tar.TypeReg && hdr.Size > 0 {
			if err := copyExact(tr, emit, hdr.Size); err != nil {
				return entries, total, fmt.Errorf("archive_rejected: emit: %w", err)
			}
		}
	}
	return entries, total, nil
}

// symlinkParentPrefix returns the earlier symlink whose name is a proper path
// prefix of name (i.e. this entry writes THROUGH that symlink), or "".
func symlinkParentPrefix(name string, symlinks map[string]string) string {
	for p := path.Dir(name); p != "." && p != "/"; p = path.Dir(p) {
		if _, ok := symlinks[p]; ok {
			return p
		}
	}
	_ = symlinks
	return ""
}

// copyExact copies exactly n bytes from r to w (tar.Writer.Write must see the
// declared Size bytes before the next header).
func copyExact(r io.Reader, w io.Writer, n int64) error {
	buf := make([]byte, 16384)
	for n > 0 {
		want := int64(len(buf))
		if n < want {
			want = n
		}
		got, rerr := r.Read(buf[:int(want)])
		if rerr == io.EOF && got > 0 {
			// tar.Reader reports io.EOF ALONGSIDE the last bytes of an entry, so
			// those bytes are still good; only a read that yields nothing fails.
			rerr = nil
		}
		if rerr != nil || got <= 0 {
			return fmt.Errorf("short read: %d of %d bytes", got, n)
		}
		if _, werr := w.Write(buf[:got]); werr != nil {
			return werr
		}
		n -= int64(got)
	}
	_ = w
	return nil
}
