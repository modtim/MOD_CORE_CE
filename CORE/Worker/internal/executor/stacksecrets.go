package executor

// GX.S10b — stack file secrets + storage files. The API rewrites the worker
// copy of a stack's compose so that every `secret_file` / `storage_file`
// variable is referenced through two env vars, ${MOD_SECRETS_DIR} and
// ${MOD_FILES_DIR}, which the worker exports into the `docker compose`
// process at deploy time. The actual bytes travel in the task payload:
//
//   secret_files:  [{name, content_b64, mode}]  -> per-stack secrets dir
//   storage_files: [{name, url, size, sha256?, mode}] -> per-stack files dir
//
// Secret content is NEVER logged. A storage URL's query string carries the
// presign signature and is NEVER logged either — only scheme://host/path.

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

// stackSecretsDirBase is the tmpfs root a per-project secrets dir is created
// under, when /dev/shm is present and writable. Secret files live in memory,
// so a leaked secret never touches durable disk. Used by host-process workers
// (whose /dev/shm IS the host's tmpfs, visible to the daemon).
const stackSecretsDirBase = "/dev/shm/mod-stack-secrets"

// containerSecretsRoot is the SAME-PATH bind root a containerized worker
// materializes secret files under. On a containerized worker /dev/shm is a
// PRIVATE tmpfs the host daemon cannot see — so the worker must instead write
// to a path the host daemon CAN see. /run is tmpfs on the host under systemd
// and on WSL hosts, and the worker container binds /run/mod-secrets into itself
// at the same path, so the daemon (outside the worker) sees exactly the files
// the worker writes. Secret files therefore never touch durable disk.
const containerSecretsRoot = "/run/mod-secrets"

// storageDownloadTimeout bounds one presigned-URL fetch. The API mints the URL
// for a short TTL (<= 15 min); the bound is generous enough for a 2 GiB file
// on a slow line yet short enough to fail closed rather than hang a deploy.
const storageDownloadTimeout = 15 * time.Minute

// stackRootDir is the stable on-disk home for a project's materialized files.
// It is stable across deploys (keyed by the sanitized project name), which is
// what lets a redeploy skip re-downloading an unchanged storage file. It lives
// under the worker's package-standard dir, falling back to the system temp.
func stackRootDir(project string) string {
	name := sanitizeProject(project)
	base := filepath.Join("/var/lib/mod-worker", "stacks")
	if err := os.MkdirAll(base, 0o755); err != nil {
		base = filepath.Join(os.TempDir(), "mod-worker-stacks")
	}
	return filepath.Join(base, name)
}

// stackSecretsDirFor returns the per-project secrets dir.
//
// Host-process worker: prefer the host's /dev/shm tmpfs (the daemon sees it
// directly); a size-limited or read-only /dev/shm falls back to the on-disk
// <stack dir>/.mod-secrets so a secret deploy still works (the daemon shares
// the host filesystem).
//
// Containerized worker: ALWAYS the same-path bind root containerSecretsRoot.
// The fail-closed guard (fileSecretsRootStatus) has already verified that root
// is a tmpfs same-path host bind before materialization runs, so we write there
// and NEVER fall back to durable disk — secret bytes must not touch a non-tmpfs
// mount (a leaked secret would survive on disk).
func stackSecretsDirFor(project, fallback string) string {
	if detectContainerized() {
		return filepath.Join(containerSecretsRoot, sanitizeProject(project))
	}
	base := stackSecretsDirBase
	if fi, err := os.Stat(base); err == nil && fi.IsDir() {
		// Probe writability with a throwaway dir we immediately remove.
		if probe := filepath.Join(base, ".probe"); os.MkdirAll(probe, 0o700) == nil {
			os.RemoveAll(probe)
			return filepath.Join(base, sanitizeProject(project))
		}
	}
	return filepath.Join(fallback, ".mod-secrets")
}

// materializeStackFiles writes the payload's secret_files and downloads its
// storage_files for project, returning the two dirs to export to compose, the
// decoded secret values (for log redaction), and any error. It returns four
// zero values when the payload carries neither kind, so a plain deploy is
// byte-identical to before this landed.
//
// The secrets dir is cleared first so a redeploy that drops a variable leaves
// no stale secret file behind (the files dir is NOT cleared — an unchanged
// storage file is skipped and reused).
//
// 30.6.8 c8-W1 — the clear must NOT remove the secrets dir itself. The API
// mounts `${MOD_SECRETS_DIR}/identity:/run/mod-identity:ro`, a DIRECTORY bind,
// and a running container is pinned to the inode that existed when it started.
// os.RemoveAll(dir) + recreate gives `identity/` a brand-new inode, so the
// container keeps reading the deleted one and never sees the rotated token
// (BUG-1027h). clearSecretsDirKeepingBinds keeps `dir` and `dir/identity` —
// the same inodes — and removes every other entry plus the stale files inside
// `identity/`, so generation semantics hold for every secret while the two
// bind-mounted dirs stay live.
func materializeStackFiles(ctx context.Context, project string, p transport.TaskPayload) (secretsDir, filesDir string, secretValues []string, err error) {
	if len(p.SecretFiles) == 0 && len(p.StorageFiles) == 0 {
		return "", "", nil, nil
	}
	root := stackRootDir(project)

	if len(p.SecretFiles) > 0 {
		dir := stackSecretsDirFor(project, root)
		// Clear any prior generation's secrets before writing the new set, but
		// keep the dir inodes a directory bind-mount is pinned to (c8-W1).
		if err := clearSecretsDirKeepingBinds(dir, p.SecretFiles); err != nil {
			return "", "", nil, fmt.Errorf("clear secrets dir: %w", err)
		}
		d, vals, werr := writeStackSecrets(dir, p.SecretFiles)
		if werr != nil {
			return "", "", nil, werr
		}
		markStackHasSecrets(root)
		secretsDir, secretValues = d, vals
	}

	if len(p.StorageFiles) > 0 {
		dir := filepath.Join(root, ".mod-files")
		d, werr := downloadStackFiles(ctx, dir, p.StorageFiles)
		if werr != nil {
			return "", "", nil, werr
		}
		filesDir = d
	}
	return secretsDir, filesDir, secretValues, nil
}

// cleanupStackFiles removes a project's materialized secret and file dirs.
// Called on teardown. Both roots are best-effort: a missing dir is not an
// error, and the tmpfs and on-disk locations are removed independently.
func cleanupStackFiles(project string) {
	if project == "" {
		return
	}
	name := sanitizeProject(project)
	// tmpfs location. Remove from whichever root we materialized under — the
	// containerized same-path bind root, the host /dev/shm root — so a secret
	// never outlives the stack in either.
	for _, base := range []string{containerSecretsRoot, stackSecretsDirBase} {
		if fi, err := os.Stat(base); err == nil && fi.IsDir() {
			os.RemoveAll(filepath.Join(base, name))
		}
	}
	// on-disk locations under the stable project root.
	root := stackRootDir(project)
	os.RemoveAll(filepath.Join(root, ".mod-secrets"))
	os.RemoveAll(filepath.Join(root, ".mod-files"))
	os.Remove(filepath.Join(root, secretsMarkerName))
}

// identitySubdir is the ONE prefix a secret-file name may carry. The API
// mounts `${MOD_SECRETS_DIR}/identity:/run/mod-identity:ro` — a DIRECTORY bind
// — so a rotated identity token is seen by a running container at once, with no
// restart (a FILE bind pins the container to the old inode: BUG-1027h).
const identitySubdir = "identity"

// identityNamePattern is the shape of the part AFTER `identity/`. It is
// deliberately narrower than the unprefixed rule: the sub-dir is the platform's
// own namespace, so a name in it must be a plain token.
const identityNamePattern = `^[A-Za-z0-9_.-]{1,128}$`

var identityNameRe = regexp.MustCompile(identityNamePattern)

// secretTargetName maps a payload name to the path under dir that holds it, or
// "" when the name is not allowed. An UNPREFIXED name keeps today's exact rule
// (non-empty, no `/`, `\` or NUL) so existing stacks are unaffected. A name may
// carry ONE `identity/` prefix whose remainder must match identityNamePattern
// and be neither `.` nor `..`; anything else containing a separator —
// `identity/a/b`, `x/y`, `identity/`, `identity/..`, `/identity/x`,
// `identity\x` — is rejected.
func secretTargetName(name string) string {
	name = strings.TrimSpace(name)
	if name == "" {
		return ""
	}
	if !strings.Contains(name, "/") {
		// Today's rule, unchanged: a backslash or NUL would escape the dir.
		if strings.ContainsAny(name, "\\\x00") {
			return ""
		}
		return name
	}
	prefix := identitySubdir + "/"
	if !strings.HasPrefix(name, prefix) {
		return "" // any other separator is a path-escape attempt
	}
	rest := name[len(prefix):]
	if !identityNameRe.MatchString(rest) || rest == "." || rest == ".." {
		return ""
	}
	return filepath.Join(identitySubdir, rest)
}

// clearSecretsDirKeepingBinds replaces materializeStackFiles' os.RemoveAll(dir)
// for a redeploy: `dir` and `dir/identity` keep their inodes (a directory bind
// mounted into a running container is pinned to them), every OTHER entry of
// `dir` is removed, and the files in `dir/identity` that are not in the new
// payload set are removed. Generation semantics — no stale secret survives a
// redeploy — therefore hold for every secret file, while the two bind targets
// stay live. Teardown (cleanupStackFiles) still removes everything.
func clearSecretsDirKeepingBinds(dir string, files []transport.StackSecretFile) error {
	keep := map[string]bool{identitySubdir: true}
	for _, f := range files {
		rel := secretTargetName(strings.TrimSpace(f.Name))
		if rel == "" {
			continue // writeStackSecrets reports the bad name; nothing to keep
		}
		// Only a top-level name (the legacy unprefixed form) is kept at dir
		// level; an `identity/…` name belongs to the sub-dir pass below.
		if filepath.Dir(rel) == "." {
			keep[rel] = true
		}
	}
	ents, err := os.ReadDir(dir)
	if err != nil {
		if _, statErr := os.Stat(dir); statErr != nil {
			return nil // nothing materialized yet — same as RemoveAll on a miss
		}
		return fmt.Errorf("read secrets dir: %w", err)
	}
	for _, ent := range ents {
		if keep[ent.Name()] && (ent.Name() == identitySubdir || !ent.IsDir()) {
			continue
		}
		// A kept NAME that is a directory is docker's empty auto-created bind
		// source (a missing secret file at container start, e.g. after a reboot
		// wiped /run): it must be replaced by the file, so it is cleared here.
		if rerr := removeStrayDir(filepath.Join(dir, ent.Name())); rerr != nil {
			return fmt.Errorf("clear secrets dir: %w", rerr)
		}
	}
	inner := filepath.Join(dir, identitySubdir)
	innerEnts, err := os.ReadDir(inner)
	if err != nil {
		if _, statErr := os.Stat(inner); statErr != nil {
			return nil // no identity sub-dir yet
		}
		return fmt.Errorf("read identity sub-dir: %w", err)
	}
	innerKeep := map[string]bool{}
	for _, f := range files {
		rel := secretTargetName(strings.TrimSpace(f.Name))
		if filepath.Dir(rel) == identitySubdir {
			innerKeep[filepath.Base(rel)] = true
		}
	}
	for _, ent := range innerEnts {
		// Only FILES are pruned here: a sub-dir of identity/ is another bind
		// target, and removing it would re-pin a running container's mount.
		if ent.IsDir() {
			continue
		}
		if innerKeep[ent.Name()] {
			continue
		}
		if rerr := os.Remove(filepath.Join(inner, ent.Name())); rerr != nil {
			return fmt.Errorf("clear identity sub-dir: %w", rerr)
		}
	}
	return nil
}

// removeStrayDir removes path (file or directory tree). If the first attempt is
// refused (a root-owned or read-only directory created by the docker daemon),
// it relaxes the mode of the tree once and retries, so a stray directory can
// never wedge a deploy ("unlinkat ... permission denied").
func removeStrayDir(path string) error {
	err := os.RemoveAll(path)
	if err == nil {
		return nil
	}
	_ = filepath.WalkDir(path, func(p string, d os.DirEntry, werr error) error {
		if d != nil && d.IsDir() {
			_ = os.Chmod(p, 0o700)
		}
		return nil
	})
	_ = os.Chmod(filepath.Dir(path), 0o700)
	return os.RemoveAll(path)
}

// refreshStackSecretFiles is the body of the refresh_secret_files StackOp: it
// replaces the CONTENT of secret files that already exist, and never creates
// one. Every target is validated first (name rule, base64, regular-file
// existence) so a single bad entry writes nothing at all. It returns the
// decoded values for the caller's log-redaction list and the count refreshed.
// No compose call, no restart, no dir clear.
func refreshStackSecretFiles(dir string, files []transport.StackSecretFile) (int, []string, error) {
	type target struct {
		path string
		data []byte
		mode os.FileMode
	}
	var writes []target
	var values []string
	for _, f := range files {
		rel := secretTargetName(strings.TrimSpace(f.Name))
		if rel == "" {
			return 0, nil, fmt.Errorf("invalid secret file name")
		}
		data, err := base64.StdEncoding.DecodeString(f.ContentB64)
		if err != nil {
			return 0, nil, fmt.Errorf("decode secret %q: %w", strings.TrimSpace(f.Name), err)
		}
		path := filepath.Join(dir, rel)
		fi, err := os.Lstat(path)
		if err != nil || !fi.Mode().IsRegular() {
			// Refresh only REPLACES a secret the stack already has; creating one
			// here would let a payload plant a file the running containers never
			// had mounted.
			return 0, nil, fmt.Errorf("refresh target %q does not exist as a regular file", rel)
		}
		writes = append(writes, target{path: path, data: data, mode: secretFileMode(string(f.Mode))})
		values = append(values, string(data))
	}
	for _, w := range writes {
		if err := writeAtomic(w.path, w.data, w.mode); err != nil {
			return 0, nil, fmt.Errorf("refresh secret: %w", err)
		}
	}
	return len(writes), values, nil
}

// writeStackSecrets materializes each secret file into dir (creating it 0700),
// decoding base64 content and writing it via temp-file + rename so a reader
// never sees a half-written secret and a redeploy overwrites atomically. It
// returns the dir used and the decoded values (for the caller to add to its
// log-redaction list). It never logs content.
func writeStackSecrets(dir string, files []transport.StackSecretFile) (string, []string, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", nil, fmt.Errorf("create secrets dir: %w", err)
	}
	// MkdirAll does not re-chmod a pre-existing dir; enforce 0700 regardless so
	// the secrets dir is private even if it survived from an earlier generation.
	if err := os.Chmod(dir, 0o700); err != nil {
		return "", nil, fmt.Errorf("chmod secrets dir: %w", err)
	}
	var values []string
	for _, f := range files {
		name := strings.TrimSpace(f.Name)
		rel := secretTargetName(name)
		if rel == "" {
			// A path separator would let a payload escape the dir. c8-W1 allows
			// exactly one literal `identity/` prefix and nothing else.
			return "", nil, fmt.Errorf("invalid secret file name")
		}
		if filepath.Dir(rel) == identitySubdir {
			// The identity sub-dir is the dir the API binds at /run/mod-identity:
			// private, like the secrets dir itself.
			sub := filepath.Join(dir, identitySubdir)
			if err := os.MkdirAll(sub, 0o700); err != nil {
				return "", nil, fmt.Errorf("create identity sub-dir: %w", err)
			}
			if err := os.Chmod(sub, 0o700); err != nil {
				return "", nil, fmt.Errorf("chmod identity sub-dir: %w", err)
			}
		}
		data, err := base64.StdEncoding.DecodeString(f.ContentB64)
		if err != nil {
			return "", nil, fmt.Errorf("decode secret %q: %w", name, err)
		}
		if err := writeAtomic(filepath.Join(dir, rel), data, secretFileMode(string(f.Mode))); err != nil {
			return "", nil, fmt.Errorf("write secret %q: %w", name, err)
		}
		values = append(values, string(data))
	}
	return dir, values, nil
}

// secretFileMode parses the payload's mode, defaulting to 0400. The result is
// clamped to at most 0444: no write bit for any class, no exec bit, and no
// setuid/setgid/sticky bit, whatever the payload asks for.
func secretFileMode(mode string) os.FileMode {
	return parseFileMode(mode, 0o400) & 0o444
}

// downloadStackFiles streams each storage file from its presigned URL into dir
// (creating it 0755), enforcing Size, verifying SHA256 when given, and skipping
// an unchanged file whose on-disk sha256 already matches. On any error the
// message names the variable NAME only — never the URL (its query carries the
// signature).
func downloadStackFiles(ctx context.Context, dir string, files []transport.StackStorageFile) (string, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("create files dir: %w", err)
	}
	if err := os.Chmod(dir, 0o755); err != nil {
		return "", fmt.Errorf("chmod files dir: %w", err)
	}
	client := &http.Client{Timeout: storageDownloadTimeout}
	for _, f := range files {
		name := strings.TrimSpace(f.Name)
		if name == "" || strings.ContainsAny(name, "/\\\x00") {
			return "", fmt.Errorf("invalid storage file name")
		}
		target := filepath.Join(dir, name)
		// Skip the re-download when we already hold this exact content on disk.
		if f.SHA256 != "" {
			if existing, ok := sha256OfFile(target); ok && existing == foldHex(f.SHA256) {
				continue
			}
		}
		data, err := fetchStorageFile(ctx, client, f.URL, f.Size)
		if err != nil {
			// Surface the redacted URL only — never the raw one with its query.
			return "", fmt.Errorf("download %q: %w (%s)", name, err, redactURL(f.URL))
		}
		if f.SHA256 != "" {
			sum := sha256.Sum256(data)
			if hex.EncodeToString(sum[:]) != foldHex(f.SHA256) {
				return "", fmt.Errorf("sha256 mismatch for %q", name)
			}
		}
		if err := writeAtomic(target, data, storageFileMode(string(f.Mode))); err != nil {
			return "", fmt.Errorf("write %q: %w", name, err)
		}
	}
	return dir, nil
}

// storageFileMode parses the payload's mode, defaulting to 0444. The result is
// clamped to at most 0644: owner read/write, group and other read-only, and no
// exec, setuid/setgid or sticky bit.
func storageFileMode(mode string) os.FileMode {
	return parseFileMode(mode, 0o444) & 0o644
}

// fetchStorageFile streams the URL with a hard Size cap (aborting the moment it
// is exceeded, so an oversized object cannot exhaust disk/RAM). The returned
// error NEVER includes the URL — only the caller's redacted form may be logged.
func fetchStorageFile(ctx context.Context, client *http.Client, rawURL string, size int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err // transport error: no URL content in the message
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %s", resp.Status)
	}
	// Enforce the cap on the declared length first (cheap, catches a wrong-size
	// object before reading a byte).
	if size > 0 && resp.ContentLength > size {
		return nil, fmt.Errorf("size %d exceeds cap %d", resp.ContentLength, size)
	}
	// One extra byte lets us detect an overrun when no Content-Length is given.
	data, err := io.ReadAll(io.LimitReader(resp.Body, size+1))
	if err != nil {
		return nil, err
	}
	if size > 0 && int64(len(data)) > size {
		return nil, fmt.Errorf("content exceeded size cap %d", size)
	}
	return data, nil
}

// writeAtomic writes data to path via a temp file in the same directory,
// chmods it, then renames over the target — so a reader never observes a
// partial write and a redeploy is a clean overwrite.
//
// B86i-g — a prior bad deploy (e.g. the insideContainer() false-negative
// above, or a payload that genuinely omitted this secret) can leave `path`
// as an empty directory: Docker auto-creates one at a bind-mount's source
// when the file it expected is missing. rename(2) refuses to replace a
// directory with a regular file (EISDIR), so a bare os.Rename here would
// fail every subsequent deploy forever, wedging the stack. Self-heal: if
// the rename fails specifically because `path` is a directory, remove
// EXACTLY that path (never its parent, never a sibling) and retry once.
func writeAtomic(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	_, werr := tmp.Write(data)
	if werr == nil {
		werr = tmp.Close()
	} else {
		tmp.Close()
	}
	if werr != nil {
		os.Remove(tmpName)
		return werr
	}
	if err := os.Chmod(tmpName, mode); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		if isDirAtPath(path) {
			// Stray directory from a prior bad deploy (or docker's own
			// auto-create of a missing bind source) — remove exactly this
			// path, nothing above or beside it, and retry the rename once.
			if rmErr := os.RemoveAll(path); rmErr != nil {
				os.Remove(tmpName)
				return fmt.Errorf("replace stray directory %q: %w", path, rmErr)
			}
			if err := os.Rename(tmpName, path); err != nil {
				os.Remove(tmpName)
				return err
			}
			return nil
		}
		os.Remove(tmpName)
		return err
	}
	return nil
}

// isDirAtPath reports whether path currently exists and is a directory.
func isDirAtPath(path string) bool {
	fi, err := os.Lstat(path)
	return err == nil && fi.IsDir()
}

// sha256OfFile returns the lowercase-hex sha256 of an existing file.
func sha256OfFile(path string) (string, bool) {
	f, err := os.Open(path)
	if err != nil {
		return "", false
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", false
	}
	return hex.EncodeToString(h.Sum(nil)), true
}

// foldHex normalizes a hex digest for comparison (lowercase, trimmed).
func foldHex(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// parseFileMode parses a base-8 mode string. On any problem it returns def
// rather than failing — a missing or odd mode must not abort a deploy, and the
// caller's default is the safe choice.
func parseFileMode(s string, def os.FileMode) os.FileMode {
	s = strings.TrimSpace(s)
	if s == "" {
		return def
	}
	trimmed := strings.TrimPrefix(s, "0o")
	trimmed = strings.TrimPrefix(trimmed, "0")
	if trimmed == "" {
		return def
	}
	var m uint32
	if _, err := fmt.Sscanf(trimmed, "%o", &m); err != nil {
		return def
	}
	return os.FileMode(m)
}

// stackEnvForCompose builds the extra env vars (MOD_SECRETS_DIR / MOD_FILES_DIR)
// to append to the compose command's environment. Only vars with a non-empty
// value are included, so a plain deploy's env is byte-identical to before.
func stackEnvForCompose(secretsDir, filesDir string) []string {
	var env []string
	if secretsDir != "" {
		env = append(env, "MOD_SECRETS_DIR="+secretsDir)
	}
	if filesDir != "" {
		env = append(env, "MOD_FILES_DIR="+filesDir)
	}
	return env
}

// detectContainerized is the seam tests swap to force the containerized
// outcome (the real check reads /proc and env, neither of which a test
// controls). It feeds two decisions: which tmpfs root secrets are written to
// (secretsBaseRoot) and whether the containerized-worker bind verification in
// fileSecretsRootStatus runs at all.
var detectContainerized = insideContainer

// insideContainer reports whether the worker process itself runs in a
// container. A containerized worker's /dev/shm is a PRIVATE tmpfs the docker
// daemon (outside the container) cannot see, so a file-secret deploy must
// either use a same-path host bind the daemon CAN see (the verified, allowed
// path — see fileSecretsRootStatus) or fail closed. This detection only says
// "am I in a container"; the allow/deny decision lives in
// computeFileSecretsStatus.
func insideContainer() bool {
	if os.Getenv("container") != "" {
		return true
	}
	// B86i-g — /proc/1/cgroup's "docker"/"libcontainer" substring check only
	// ever matched the LEGACY cgroup v1 hierarchy (e.g.
	// "1:name=systemd:/docker/<id>"). A cgroup v2 host (the unified
	// hierarchy — this worker's own container reads back just "0::/") never
	// contains either substring, so insideContainer() silently returned
	// false for an ACTUALLY-containerized worker. That false negative sent
	// every stack secret file to the host-process branch's /dev/shm path —
	// private to THIS container, invisible to the host dockerd — instead of
	// the verified same-path bind root (containerSecretsRoot): the fail-closed
	// guard in fileSecretsRootStatus only runs for a containerized worker, so
	// it never caught this either. `docker compose up` then found no file at
	// the secret's host source path and silently auto-created an empty
	// DIRECTORY there (and at the container's mount target) — e.g. stack 114,
	// modstack114g0-api-1, /srv/mod-ca/mod-local-ca.crt — so the app's TLS
	// client had no CA bundle and every MOD API call failed closed with
	// CERTIFICATE_VERIFY_FAILED. /.dockerenv is the version-agnostic signal
	// Docker has written into every container (cgroup v1 or v2) since 1.x, so
	// it is checked first and is sufficient on its own; the cgroup string
	// check stays as a fallback for container runtimes that skip
	// /.dockerenv (e.g. some containerd-only / podman setups).
	_, dockerenvErr := os.Stat("/.dockerenv")
	cgroup, _ := os.ReadFile("/proc/1/cgroup")
	return insideContainerFromSignals(dockerenvErr == nil, string(cgroup))
}

// insideContainerFromSignals is the pure decision behind insideContainer,
// factored out so the cgroup-v2 false-negative (and the /.dockerenv fix) can
// be unit tested without touching the real host's /proc or /.dockerenv.
func insideContainerFromSignals(dockerenvExists bool, cgroup string) bool {
	if dockerenvExists {
		return true
	}
	return strings.Contains(cgroup, "docker") || strings.Contains(cgroup, "libcontainer")
}

// redactURL reduces a presigned URL to scheme://host/path, dropping the query
// (which carries the signature) and any userinfo. Safe to log.
func redactURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" {
		return "<redacted-url>"
	}
	return u.Scheme + "://" + u.Host + u.Path
}

// secretsMarkerName is an EMPTY marker file in the durable stack root recording
// "this project was deployed with secret files". It holds no secret bytes and no
// secret names, so it is safe on persistent disk; it is what lets a restarted
// worker tell "secrets were lost with the tmpfs" from "never had secrets".
const secretsMarkerName = ".mod-has-secrets"

func markStackHasSecrets(root string) {
	if err := os.MkdirAll(root, 0o755); err == nil {
		_ = os.WriteFile(filepath.Join(root, secretsMarkerName), nil, 0o644)
	}
}

// secretsMissingWith is the daemon-free core of StackSecretsMissing: true only
// when the marker says the project had secrets AND its secrets dir is gone or
// empty (tmpfs was wiped by a host/WSL restart). stat/readDir are seams.
func secretsMissingWith(markerPath, secretsDir string,
	stat func(string) (os.FileInfo, error),
	readDir func(string) ([]os.DirEntry, error)) bool {
	if _, err := stat(markerPath); err != nil {
		return false
	}
	ents, err := readDir(secretsDir)
	if err != nil || len(ents) == 0 {
		return true
	}
	return !hasRegularFile(secretsDir, ents, readDir, 0)
}

// hasRegularFile reports whether the tree under dir (ents = its entries,
// searched two levels deep: the secrets dir and its `identity/` sub-dir) holds
// at least one regular file. After a reboot docker may have created EMPTY
// directories at the secret paths (it does that for a missing bind source), so
// a non-empty listing made only of empty directories still means the secrets
// were lost.
func hasRegularFile(dir string, ents []os.DirEntry, readDir func(string) ([]os.DirEntry, error), depth int) bool {
	for _, ent := range ents {
		if !ent.IsDir() {
			return true
		}
		if depth >= 1 {
			continue
		}
		sub := filepath.Join(dir, ent.Name())
		if subEnts, err := readDir(sub); err == nil && hasRegularFile(sub, subEnts, readDir, depth+1) {
			return true
		}
	}
	return false
}

// StackSecretsMissing reports whether project was deployed with secret files
// whose tmpfs copy no longer exists (1900Y1q). Never reads or logs secret bytes.
func StackSecretsMissing(project string) bool {
	root := stackRootDir(project)
	return secretsMissingWith(filepath.Join(root, secretsMarkerName),
		stackSecretsDirFor(project, root), os.Stat, os.ReadDir)
}

// SecretRedeployWanted decides whether the heartbeat should ask the manager to
// re-dispatch this project's deploy (fresh payload = fresh secrets): the secret
// dir is missing and the stack is not fully up.
func SecretRedeployWanted(running, total int, missing bool) bool {
	return missing && running < total
}
