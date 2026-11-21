// Package releases handles automatic publication of the worker binary
// and release manifest to the object store when the embedded worker
// container starts. The object store is the versitygw S3 gateway by
// default (see internal/storage); the endpoint and credentials are
// resolved there, not hardcoded here.
//
// Design rationale:
//
//   - Mod Core deployments run entirely on the control server; operators
//     never shell into the manager. So the "cut a release" workflow must
//     be a side effect of the normal service lifecycle, not a script
//     someone runs.
//   - The embedded worker container is always present on the control
//     server, has direct object-store root credentials (resolved by
//     internal/storage.RootCredentials), and its image carries both
//     linux/amd64 and linux/arm64 binaries (see Dockerfile). It is the
//     natural place to publish releases from.
//   - Remote workers never run this code. They discover new releases via
//     the heartbeat response and pull binaries from the API, which
//     serves them out of the worker-releases bucket.
//
// Behaviour on startup (embedded mode only):
//   1. Read the baked-in /releases/VERSION file.
//   2. Compare to the version advertised in worker-releases/latest.json.
//   3. If different (or the manifest is missing), upload both binaries
//      and a fresh manifest, binaries first so a remote worker polling
//      the manifest never sees a version advertised before its binary
//      is on disk.
//   4. Errors here never prevent the worker from starting — a failed
//      publish is logged but the worker still runs and claims tasks.
//      The next embedded-worker restart retries the publish.
package releases

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/selfupdate"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/storage"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/vault"
)

// ReleaseBinary describes a single platform binary on disk.
//
// WIN.1.s15 adds the optional Artifact field: the Windows installer (MSI) and
// the tray app exe are published through the SAME channel as the worker
// binaries (same bucket, same upload loop, same manifest-last ordering rule)
// instead of getting a second release mechanism. Artifact is empty for the
// worker binaries, so every existing caller is unchanged.
type ReleaseBinary struct {
	OS       string // "linux" | "windows" | "darwin"
	Arch     string // "amd64" | "arm64"
	Path     string // path to the binary file in the container filesystem
	Artifact string // "" (worker binary) | "installer" | "app"
}

// Manifest matches the JSON shape the API reads to drive the Dashboard.
// Must stay in sync with CORE/API/utils/worker_releases.py.
//
// Amd64SHA256/Arm64SHA256 are the ORIGINAL fields and always describe the
// linux binaries — that's what already-released workers' self-update
// reader (CORE/Worker self-update client) and the API's
// get_latest_release_info() expect, so their meaning can never change.
// The windows/darwin fields below are purely additive: older API/worker
// builds that don't know about them simply ignore the extra JSON keys.
type Manifest struct {
	Version            string    `json:"version"`
	ReleasedAt         time.Time `json:"released_at"`
	Amd64SHA256        string    `json:"amd64_sha256,omitempty"`         // linux/amd64
	Arm64SHA256        string    `json:"arm64_sha256,omitempty"`         // linux/arm64
	WindowsAmd64SHA256 string    `json:"windows_amd64_sha256,omitempty"` // windows/amd64
	DarwinAmd64SHA256  string    `json:"darwin_amd64_sha256,omitempty"`  // darwin/amd64
	DarwinArm64SHA256  string    `json:"darwin_arm64_sha256,omitempty"`  // darwin/arm64
	ChangelogURL       string    `json:"changelog_url,omitempty"`
	Notes              string    `json:"notes,omitempty"`
	// WIN.1.s15: the Windows installer (MSI) + tray app exe ride the SAME
	// release channel as the worker binaries — same bucket, same upload loop,
	// same manifest-last ordering — so there is no second update mechanism
	// (R38.10 WIN-D = worker-driven). Purely additive: an API/worker build
	// that predates them ignores these keys, and they are absent until an
	// artifact is actually published (nothing is advertised before it ships).
	InstallerSHA256    string    `json:"installer_sha256,omitempty"` // MOD-Setup-<version>.msi
	AppSHA256          string    `json:"app_sha256,omitempty"`       // MODTray-<version>.exe
	// FX.113.p5c: signatures are NOT recorded here. Each artefact's detached
	// signature rides the bucket as a sibling object named <object>.sig
	// (internal/selfupdate's SignatureURL is the single definition of that
	// naming rule), and every download path verifies that file against the
	// public key compiled into the downloading binary — a key the bucket
	// cannot influence — so a manifest entry for it would be a second, weaker
	// copy of the same fact. The release step that cuts the .sig must upload
	// it BEFORE this manifest upload runs (see README.md, "What the release
	// pipeline must do"): the ordering rule this function already enforces for
	// binaries extends to .sigs, so a worker racing a publish never sees a
	// version advertised before its signature is on disk. A release whose .sig
	// is missing is refused by the downloader, never treated as "nothing to
	// verify".
}

// releaseObjectName maps (os, arch) to the worker-releases bucket object
// name. MUST match CORE/API/routers/worker_provisioning.py's
// _worker_object_name() exactly — that function is what a downloading
// worker/installer asks the API for; if these two naming schemes drift,
// downloads 404. linux/darwin binaries carry no extension; windows
// binaries keep `.exe` so Windows' loader/AV/SmartScreen treat them as
// one.
func releaseObjectName(osName, arch string) string {
	name := fmt.Sprintf("mod-worker-%s-%s", osName, arch)
	if osName == "windows" {
		name += ".exe"
	}
	return name
}

const (
	bucket             = "worker-releases"
	manifestObjectName = "latest.json"
	// manifestReadCap bounds the read of the remote manifest object: latest
	// .json is a few hundred bytes, so anything near a megabyte is not a
	// manifest we should parse (a corrupt/hostile object must not make this
	// startup slurp the bucket).
	manifestReadCap = 1 << 20
)

// artifactKindName validates the artifact kind a ReleaseBinary carries
// (WIN.1.s15). Only two kinds exist today; anything else is a caller bug and
// must fail the publish rather than silently upload an untracked object.
func artifactKindName(kind string) (string, error) {
	switch kind {
	case "installer", "app":
		return kind, nil
	default:
		return "", fmt.Errorf("unknown release artifact kind %q", kind)
	}
}

// artifactObjectName maps an artifact kind to its worker-releases bucket
// object name (WIN.1.s15). The MSI keeps its `.msi` and the tray app its
// `.exe` for the same reason the windows worker binary keeps `.exe` — the
// Windows loader/AV/SmartScreen and the browser download handler treat the
// extension as the file type. The version is the release version so an
// upgrade-by-re-run never races the previous artifact's bytes. These names
// are what CORE/release/windows-installer/build.sh emits (MOD-Setup-<version>
// .msi) and what CORE/WindowsApp/build.sh will emit (MODTray-<version>.exe).
func artifactObjectName(kind, version string) (string, error) {
	switch kind {
	case "installer":
		return fmt.Sprintf("MOD-Setup-%s.msi", version), nil
	case "app":
		return fmt.Sprintf("MODTray-%s.exe", version), nil
	default:
		return "", fmt.Errorf("unknown release artifact kind %q", kind)
	}
}

// PublishOptions carries the operator knobs for one publish. Every field
// defaults to the safe behaviour, so a caller that passes nothing gets
// strictly-newer-only publishing with fail-closed version parsing.
type PublishOptions struct {
	// AllowDowngrade is the OPERATOR OVERRIDE (owner decision 11-11: newer-only
	// by default, an explicit override permits a downgrade). Default FALSE.
	// It is set through WithAllowDowngrade (a functional option) and, at the
	// worker CLI, through --allow-downgrade — never through a new env var.
	AllowDowngrade bool
}

// PublishOption is the functional-option shape: a function that mutates the
// caller's PublishOptions. Variadic on PublishIfNewer so every existing call
// site keeps compiling unchanged.
type PublishOption = func(*PublishOptions)

// WithAllowDowngrade permits publishing an OLDER version over a newer
// manifest (the rollback path). Off unless an operator asks for it.
func WithAllowDowngrade(allow bool) PublishOption {
	return func(o *PublishOptions) { o.AllowDowngrade = allow }
}

// PublishIfNewer uploads the release artefacts to the object store when the
// baked-in version is STRICTLY NEWER than what's already in the manifest
// (semver compare, not string equality — an OLDER version used to publish
// here and roll every remote worker BACK). Equal -> skip; older -> skip
// unless the operator passed the downgrade override; a version that cannot
// be parsed on EITHER side fails closed (an error, never a silent publish).
// Safe to call on every startup. Returns (publishedVersion, didPublish, err).
func PublishIfNewer(ctx context.Context, binaries []ReleaseBinary, version string, log *slog.Logger, opts ...PublishOption) (string, bool, error) {
	var po PublishOptions
	for _, apply := range opts {
		apply(&po)
	}
	version = strings.TrimSpace(version)
	if version == "" || version == "docker" || version == "dev" {
		// An untagged local build — don't overwrite a real release with a
		// placeholder version. The image was built with `|| echo docker`
		// but we refuse to publish anything that isn't a genuine release
		// string, to prevent remote workers from "updating" to a docker
		// build.
		log.Warn("refusing to publish release with placeholder version", "version", version)
		return version, false, nil
	}

	// Artifact kinds are validated up-front, before any upload, so a typo in a
	// caller's Artifact string can never leave a half-published release (WIN.1.s15).
	for _, b := range binaries {
		if b.Artifact == "" {
			continue
		}
		if _, kerr := artifactKindName(b.Artifact); kerr != nil {
			// A caller typo is a BUG, not a skip: returning nil here made
			// main.go log "release manifest already current" for a release
			// that was never published. Surface it so the operator sees the
			// publish failed (nothing was uploaded, so nothing is orphaned).
			log.Warn("refusing to publish release: unknown artifact kind", "path", b.Path, "error", kerr)
			return version, false, fmt.Errorf("release artifact kind: %w", kerr)
		}
	}

	host, useSSL := storage.Endpoint()
	accessKey, secretKey, ok := storage.RootCredentials()
	if !ok {
		log.Warn("skipping release publish: object-store root credentials not set (this worker isn't running on the control server)")
		return version, false, nil
	}

	client, err := minio.New(host, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
	})
	if err != nil {
		return version, false, fmt.Errorf("object-store client: %w", err)
	}

	// Ensure the bucket exists. Idempotent.
	exists, err := client.BucketExists(ctx, bucket)
	if err != nil {
		return version, false, fmt.Errorf("bucket exists check: %w", err)
	}
	if !exists {
		if err := client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
			return version, false, fmt.Errorf("create bucket: %w", err)
		}
		log.Info("created worker-releases bucket")
	}

	// Only a genuine "the manifest object does not exist yet" answer may
	// publish fresh. minio-go's GetObject is LAZY, so a missing key surfaces
	// here as a typed NoSuchKey (the same shape internal/executor/storage_
	// migration.go keys on). Any OTHER read/parse failure (transient network,
	// auth, a corrupt object, an oversized one) must NOT be treated as
	// "publish fresh": that would overwrite the live manifest on a hiccup
	// and orphan the release the API is already advertising. Log at Warn and
	// return: the caller logs it and nothing is overwritten.
	existing, err := readExistingManifest(ctx, client)
	if err != nil {
		if !isMissingManifestError(err) {
			log.Warn("release manifest read failed (refusing to overwrite it)",
				"object", bucket + "/" + manifestObjectName, "error", err)
			return version, false, fmt.Errorf("read existing release manifest: %w", err)
		}
		log.Debug("no existing release manifest (will publish fresh)", "error", err)
	}
	// NEWER-ONLY (owner decision 11-11 ~6:10 PM CT, D3): the old guard was
	// `existing.Version == version` — string equality — so an OLDER payload
	// version published and rolled every remote worker BACK (observed:
	// worker-example-host v1.0.42 -> v1.0.39 with update_requested stuck true).
	// Publish only when the new version is STRICTLY NEWER by semver.
	//
	// Fail closed: a version that cannot be parsed on EITHER side is an ERROR,
	// never a silent publish — a manifest whose version and bytes disagree is
	// worse than no manifest at all.
	newCmp, newOK := parseSemver(version)
	if !newOK {
		log.Error("refusing to publish release: version is not a parseable MAJOR.MINOR.PATCH[-prerelease]",
			"version", version)
		return version, false, fmt.Errorf("unparseable new release version %q: refusing to publish (fail closed)", version)
	}
	if existing != nil {
		existingCmp, existingOK := parseSemver(existing.Version)
		if !existingOK {
			log.Error("refusing to publish release: manifest version is not parseable",
				"manifest_version", existing.Version, "version", version)
			return version, false, fmt.Errorf("unparseable existing manifest version %q: refusing to publish over it (fail closed; republish requires an operator override of the manifest)", existing.Version)
		}
		if cmp := compareSemver(newCmp, existingCmp); cmp < 0 {
			// The new version is OLDER than the manifest's. Refuse unless the
			// operator explicitly allowed a downgrade.
			if !po.AllowDowngrade {
				log.Warn("refusing to publish an older release over a newer manifest",
					"manifest_version", existing.Version,
					"version", version,
					"override", "pass PublishOptions.AllowDowngrade (CLI --allow-downgrade) to publish a deliberate rollback",
				)
				return version, false, fmt.Errorf("refusing downgrade %s -> %s (newer-only; use the AllowDowngrade override to publish a deliberate rollback)", existing.Version, version)
			}
			log.Warn("publishing an OLDER release over a newer manifest (operator downgrade override in effect)",
				"manifest_version", existing.Version, "version", version)
		} else if cmp == 0 {
			// Same version already advertised — nothing to do. Most common
			// case on a normal restart where the image hasn't changed.
			log.Info("release manifest already current; skipping publish",
				"version", version, "manifest_version", existing.Version)
			return version, false, nil
		}
	}

	log.Info("publishing worker release to object store",
		"version", version,
		"previous", existingVersionForLog(existing),
		"binaries", len(binaries),
	)

	// FX.113.p5c-adjacent: sign every binary/artifact so the published
	// release is something internal/selfupdate.VerifyArtifact can actually
	// check. signingKey is nil (and already logged why) when Vault isn't
	// reachable or carries no usable key yet — every call site below treats
	// nil as "skip this .sig", never as a reason to fail the publish.
	signingKey := signingKeyFromVault(log)

	m := Manifest{
		Version:    version,
		ReleasedAt: time.Now().UTC(),
	}

	// Upload binaries first, manifest last. A remote worker that races the
	// publish can only ever see the new manifest AFTER all binaries are
	// on disk — never a manifest pointing at missing binaries. The Windows
	// installer + tray app (WIN.1.s15) go through this SAME loop, so the
	// ordering rule covers them too: the manifest never advertises an
	// artifact whose bytes are not yet in the bucket.
	for _, b := range binaries {
		// Artifacts (installer/app) upload + record their checksum in one
		// step; worker binaries record theirs in the switch below. Either way
		// every upload happens BEFORE uploadManifest runs.
		if b.Artifact != "" {
			if err := uploadArtifact(ctx, client, b, version, &m, signingKey, log); err != nil {
				return version, false, fmt.Errorf("upload artifact %s/%s: %w", b.OS, b.Artifact, err)
			}
			continue
		}
		sum, err := uploadBinary(ctx, client, b, signingKey, log)
		if err != nil {
			return version, false, fmt.Errorf("upload %s/%s: %w", b.OS, b.Arch, err)
		}
		switch {
		case b.OS == "linux" && b.Arch == "amd64":
			m.Amd64SHA256 = sum
		case b.OS == "linux" && b.Arch == "arm64":
			m.Arm64SHA256 = sum
		case b.OS == "windows" && b.Arch == "amd64":
			m.WindowsAmd64SHA256 = sum
		case b.OS == "darwin" && b.Arch == "amd64":
			m.DarwinAmd64SHA256 = sum
		case b.OS == "darwin" && b.Arch == "arm64":
			m.DarwinArm64SHA256 = sum
		default:
			// Unknown combo — still uploaded (so downloads work) but we
			// have no manifest field to record its checksum in. Should
			// never happen outside of adding a new target without
			// updating this switch.
			log.Warn("release binary uploaded but not represented in manifest schema", "os", b.OS, "arch", b.Arch)
		}
	}

	if err := uploadManifest(ctx, client, m); err != nil {
		return version, false, fmt.Errorf("upload manifest: %w", err)
	}
	log.Info("release published",
		"version", version,
		"amd64_sha256", truncate(m.Amd64SHA256, 12),
		"arm64_sha256", truncate(m.Arm64SHA256, 12),
		"windows_amd64_sha256", truncate(m.WindowsAmd64SHA256, 12),
		"darwin_amd64_sha256", truncate(m.DarwinAmd64SHA256, 12),
		"darwin_arm64_sha256", truncate(m.DarwinArm64SHA256, 12),
		"installer_sha256", truncate(m.InstallerSHA256, 12),
		"app_sha256", truncate(m.AppSHA256, 12),
	)
	return version, true, nil
}

// readExistingManifest fetches worker-releases/latest.json. The read is
// capped (manifestReadCap): the manifest is a few hundred bytes, so an
// object bigger than the cap is not a manifest and must not be slurped
// and parsed. A genuine "the key does not exist" answer is reported as a
// NoSuchKey error (minio-go's GetObject is LAZY, so a missing key usually
// surfaces inside the read, not at GetObject); the CALLER decides what a
// missing manifest means (publish fresh) versus every other failure
// (refuse to overwrite).
func readExistingManifest(ctx context.Context, client *minio.Client) (*Manifest, error) {
	obj, err := client.GetObject(ctx, bucket, manifestObjectName, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	defer obj.Close()
	data, err := io.ReadAll(io.LimitReader(obj, int64(manifestReadCap)+1))
	if err != nil {
		return nil, err
	}
	if len(data) > manifestReadCap {
		return nil, fmt.Errorf("release manifest object is larger than %d bytes (not a manifest): refusing to read it", manifestReadCap)
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// isMissingManifestError is true ONLY for a genuine "the manifest object
// does not exist yet" answer (NoSuchKey) — the one case where publishing
// fresh is correct. minio-go's ErrorResponse is usually wrapped in a
// fmt.Errorf chain, so errors.As is the reliable check (same idiom as
// internal/executor/storage_migration.go's isMissingSourceError).
func isMissingManifestError(err error) bool {
	if err == nil {
		return false
	}
	var resp minio.ErrorResponse
	if errors.As(err, &resp) {
		return resp.Code == "NoSuchKey"
	}
	return false
}

// uploadBinary uploads one worker binary and returns its sha256. Artifact
// entries never reach here — they use uploadArtifact (their object names
// carry the release version, which uploadBinary's signature has no room for).
// When signingKey is non-nil, it also uploads that binary's detached
// signature (see signAndUploadSig) before returning, so the ordering this
// file already enforces for binaries-before-manifest covers .sigs too: a
// race never sees a manifest for a binary whose signature isn't on disk yet.
func uploadBinary(ctx context.Context, client *minio.Client, b ReleaseBinary, signingKey *ecdsa.PrivateKey, log *slog.Logger) (string, error) {
	f, err := os.Open(b.Path)
	if err != nil {
		return "", fmt.Errorf("open %s: %w", b.Path, err)
	}
	defer f.Close()
	stat, err := f.Stat()
	if err != nil {
		return "", fmt.Errorf("stat %s: %w", b.Path, err)
	}

	// Compute SHA-256 by streaming into both the hasher and a memory
	// buffer (binaries are small — ~8 MiB each).
	hasher := sha256.New()
	var buf bytes.Buffer
	if _, err := io.Copy(io.MultiWriter(hasher, &buf), f); err != nil {
		return "", fmt.Errorf("read %s: %w", b.Path, err)
	}
	sum := hex.EncodeToString(hasher.Sum(nil))

	objName := releaseObjectName(b.OS, b.Arch)
	data := buf.Bytes()
	_, err = client.PutObject(ctx, bucket, objName, bytes.NewReader(data), stat.Size(), minio.PutObjectOptions{
		ContentType: "application/octet-stream",
	})
	if err != nil {
		return "", fmt.Errorf("put %s: %w", objName, err)
	}
	if signingKey != nil {
		signAndUploadSig(ctx, client, signingKey, objName, data, log)
	} else {
		// FIX-GO-RELEASES-STALE-SIG (S-go-worker-releases.md row 12): no signing key
		// means NO new signature for these new bytes, so the PREVIOUS release's
		// "<objName>.sig" would still sit beside them — internal/selfupdate.VerifyArtifact
		// would reject the new binary against the old signature and worker updates would
		// stall until the next signed publish. Delete it so the bucket matches the
		// documented unsigned-release behaviour ("no signature").
		//
		// Downgrade / version-equality semantics are NOT touched here (owner item 101:
		// PublishIfNewer's equality check stays as it is).
		removeStaleSig(ctx, client, objName, log)
	}
	return sum, nil
}

// uploadArtifact uploads one release artifact (installer/app) and records its
// sha256 in the manifest struct the caller is building. Same streaming-hash +
// PutObject shape as uploadBinary; only the object name differs (it carries the
// release version). Must run before uploadManifest — see the ordering rule.
// Signs the artifact the same way uploadBinary does, for the same reason the
// README gives: a signature rule that only lives inside the worker binaries
// is one the desktop/installer clients silently don't have.
func uploadArtifact(ctx context.Context, client *minio.Client, b ReleaseBinary, version string, m *Manifest, signingKey *ecdsa.PrivateKey, log *slog.Logger) error {
	objName, err := artifactObjectName(b.Artifact, version)
	if err != nil {
		return err
	}
	f, err := os.Open(b.Path)
	if err != nil {
		return fmt.Errorf("open %s: %w", b.Path, err)
	}
	defer f.Close()
	stat, err := f.Stat()
	if err != nil {
		return fmt.Errorf("stat %s: %w", b.Path, err)
	}
	hasher := sha256.New()
	var buf bytes.Buffer
	if _, err := io.Copy(io.MultiWriter(hasher, &buf), f); err != nil {
		return fmt.Errorf("read %s: %w", b.Path, err)
	}
	sum := hex.EncodeToString(hasher.Sum(nil))
	data := buf.Bytes()
	_, err = client.PutObject(ctx, bucket, objName, bytes.NewReader(data), stat.Size(), minio.PutObjectOptions{
		ContentType: "application/octet-stream",
	})
	if err != nil {
		return fmt.Errorf("put %s: %w", objName, err)
	}
	if signingKey != nil {
		signAndUploadSig(ctx, client, signingKey, objName, data, log)
	} else {
		removeStaleSig(ctx, client, objName, log) // same stale-sig rule as uploadBinary
	}
	switch b.Artifact {
	case "installer":
		m.InstallerSHA256 = sum
	case "app":
		m.AppSHA256 = sum
	}
	return nil
}

func uploadManifest(ctx context.Context, client *minio.Client, m Manifest) error {
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	_, err = client.PutObject(ctx, bucket, manifestObjectName, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{
		ContentType: "application/json",
	})
	return err
}

func existingVersionForLog(m *Manifest) string {
	if m == nil {
		return "(none)"
	}
	return m.Version
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// semverParts is the parsed shape parseSemver returns: the three numbers plus
// the prerelease text (empty when the version is a plain release, or when the
// suffix was build metadata "+...", which semver treats as non-ordering).
type semverParts struct {
	Major, Minor, Patch int
	Prerelease          string
}

// parseSemver parses the version shape the release pipeline really uses: an
// optional leading "v" (CORE/Worker/VERSION ships "v1.0.84"; the ldflags bake
// is -X main.Version=${VERSION} from that same file), then MAJOR.MINOR.PATCH,
// then an optional "-prerelease" and/or "+build" suffix. Returns (parts, ok);
// ok is false for anything else ("docker", "dev", "", "1.2", "v1.x.3"), which
// is what makes PublishIfNewer fail closed. Hand-rolled on purpose: go.mod
// carries no semver module and adding a dependency for ~40 lines is not worth
// it (the brief forbids one).
func parseSemver(raw string) (semverParts, bool) {
	var p semverParts
	s := strings.TrimSpace(raw)
	if len(s) > 0 && (s[0] == 'v' || s[0] == 'V') {
		s = s[1:]
	}
	// Peel the suffix first: semver puts it after PATCH, after '-' or '+'.
	// A "+build" suffix is metadata only; a "-prerelease" orders lower.
	var suffix string
	for i := 0; i < len(s); i++ {
		if s[i] == '-' || s[i] == '+' {
			suffix = s[i:]
			s = s[:i]
			break
		}
	}
	if len(suffix) > 0 && suffix[0] == '-' {
		// "-rc1", "-beta.2": orders BELOW the plain release. A bare trailing
		// '-' ("1.2.3-") is NOT a prerelease: reject it.
		p.Prerelease = suffix[1:]
		if p.Prerelease == "" {
		 return p, false
		}
	}
	// The three numbers, dot-separated, each all-digits, PATCH required.
	nums := [3]int{}
	seen := 0
	cur := 0
	digits := 0
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == '.' {
			if digits == 0 || seen >= 3 {
				return p, false
			}
			nums[seen] = cur
			seen++
			cur = 0
			digits = 0
			continue
		}
		c := s[i]
		if c < '0' || c > '9' {
			return p, false
		}
		cur = cur*10 + int(c-'0')
		if cur > 1<<30 {
			return p, false // absurd number: refuse rather than overflow
		}
		digits++
	}
	if seen != 3 {
		// "1.2" (PATCH missing) or "1.2.3.4" - not the shipped shape.
		return p, false
	}
	if pre := p.Prerelease; pre != "" && !validPrerelease(pre) {
		return p, false
	}
	p.Major, p.Minor, p.Patch = nums[0], nums[1], nums[2]
	return p, true
}

// atoiDigits parses an all-decimal-digit token; ok=false on anything else
// (leading '+'/'-', empty, letters).
func atoiDigits(tok string) (int, bool) {
	if tok == "" {
		return 0, false
	}
	n := 0
	for i := 0; i < len(tok); i++ {
		c := tok[i]
		if c < '0' || c > '9' {
			return 0, false
		}
		n = n*10 + int(c-'0')
		if n > 1<<30 {
			return 0, false
		}
	}
	return n, true
}

// validPrerelease accepts semver's prerelease charset: alphanumeric runs
// separated by dots (rc1, beta.2, 001).
func validPrerelease(pre string) bool {
	if pre == "" {
		return false
	}
	run := 0
	for i := 0; i < len(pre); i++ {
		c := pre[i]
		alnum := (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
		if alnum {
			run++
			continue
		}
		if c == '.' {
			if run == 0 {
				return false
			}
			run = 0
			continue
		}
		return false
	}
	return run > 0
}

// compareSemver orders two parsed versions: <0 when a is older, 0 when equal,
// >0 when newer. Numeric on the three numbers; a prerelease sorts BELOW the
// plain release (1.3.0-rc1 < 1.3.0), and two prereleases compare semver-style:
// numeric components below names, numbers numerically, otherwise lexically.
func compareSemver(a, b semverParts) int {
	if a.Major != b.Major {
		if a.Major > b.Major {
			return 1
		}
		return -1
	}
	if a.Minor != b.Minor {
		if a.Minor > b.Minor {
			return 1
		}
		return -1
	}
	if a.Patch != b.Patch {
		if a.Patch > b.Patch {
			return 1
		}
		return -1
	}
	// Prerelease vs release: a plain release is NEWER than any prerelease of
	// the same number.
	if a.Prerelease == "" && b.Prerelease == "" {
		return 0
	}
	if a.Prerelease == "" {
		return 1
	}
	if b.Prerelease == "" {
		return -1
	}
	// Both prereleases: compare dot-separated components.
	ca := strings.Split(a.Prerelease, ".")
	cb := strings.Split(b.Prerelease, ".")
	n := len(ca)
	if len(cb) > n {
		n = len(cb)
	}
	for i := 0; i < n; i++ {
		if i >= len(ca) {
			return -1 // fewer components sorts first
		}
		if i >= len(cb) {
			return 1
		}
		x, y := ca[i], cb[i]
		xn, xok := atoiDigits(x)
		yn, yok := atoiDigits(y)
		if xok && yok {
			if xn != yn {
				if xn > yn {
					return 1
				}
				return -1
			}
			continue
		}
		if xok != yok {
			// numeric components sort below names (semver rule 4c).
			if xok {
				return -1
			}
			return 1
		}
		if x != y {
			if x > y {
				return 1
			}
			return -1
		}
	}
	return 0
}

// signingKeyFromVault reads the release-signing ECDSA P-256 private key from
// Vault KV v2 at secret/release/cosign, field "key" — the same path
// CORE/release/sign-release.sh's cosign step reads (--vault-path/
// --vault-field there default to that exact path/field). This uses the SAME
// Vault access the embedded worker already authenticates with elsewhere
// (VAULT_ADDR + VAULT_APPROLE_ROLE_ID/SECRET_ID — see internal/config and
// cmd/worker/main.go's vaultClient); no new env var, no new secret source.
//
// Every failure path here returns nil, never an error: an un-provisioned key
// is the expected state until Ops cuts one (see internal/selfupdate's
// README), and PublishIfNewer's existing philosophy is that a missing
// capability degrades the publish, it never blocks it. Each nil path logs
// once at Warn so the reason is visible without ever logging key material.
func signingKeyFromVault(log *slog.Logger) *ecdsa.PrivateKey {
	addr := strings.TrimSpace(os.Getenv("VAULT_ADDR"))
	if addr == "" {
		log.Warn("skipping binary signing: VAULT_ADDR not set — published release will be unsigned")
		return nil
	}
	// Empty fallback role/secret: NewWithEnv reads VAULT_APPROLE_ROLE_ID /
	// VAULT_APPROLE_SECRET_ID from the environment itself (same env vars
	// internal/config.Config loads them from), so there is nothing else to
	// pass in from this package.
	vc := vault.NewWithEnv(addr, "", "")
	if err := vc.Login(); err != nil {
		log.Warn("skipping binary signing: vault login failed — published release will be unsigned", "error", err)
		return nil
	}
	secret, err := vc.ReadSecret("release/cosign")
	if err != nil {
		log.Warn("skipping binary signing: could not read secret/release/cosign — published release will be unsigned", "error", err)
		return nil
	}
	keyPEM := strings.TrimSpace(secret["key"])
	if keyPEM == "" {
		log.Warn("skipping binary signing: secret/release/cosign has no 'key' field — published release will be unsigned")
		return nil
	}
	priv, err := parseECPrivateKeyPEM(keyPEM)
	if err != nil {
		log.Warn("skipping binary signing: release signing key did not parse as an ECDSA private key — published release will be unsigned", "error", err)
		return nil
	}
	return priv
}

// parseECPrivateKeyPEM accepts either SEC1 ("EC PRIVATE KEY") or PKCS8
// ("PRIVATE KEY") PEM encodings of an ECDSA private key — stdlib only
// (crypto/x509, encoding/pem), same algorithm family internal/selfupdate's
// VerifyArtifact already checks against, so signer and verifier never
// disagree on format.
func parseECPrivateKeyPEM(keyPEM string) (*ecdsa.PrivateKey, error) {
	block, _ := pem.Decode([]byte(keyPEM))
	if block == nil {
		return nil, fmt.Errorf("no PEM block found")
	}
	if key, err := x509.ParseECPrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	any, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("not a SEC1 or PKCS8 EC private key: %w", err)
	}
	key, ok := any.(*ecdsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("key is not ECDSA")
	}
	return key, nil
}

// signAndUploadSig computes an ECDSA P-256 signature over SHA-256(data) —
// ASN.1 DER, base64-encoded — and uploads it to objName's sibling ".sig"
// object (internal/selfupdate.SignatureURL is the one place that suffix rule
// is defined; reused here rather than re-derived so the naming contract has a
// single source of truth). This is exactly the scheme
// internal/selfupdate.VerifyArtifact checks, so a worker that downloads this
// binary and its .sig can verify it against the public key compiled in via
// the Dockerfile's RELEASE_PUB_KEY_PEM build arg.
//
// Never returns an error: a signing or upload failure here is logged and
// that one object's .sig is simply absent — the existing "errors here never
// prevent the worker from starting" philosophy for this whole file, extended
// to signing. A later restart retries both the binary and its signature.
// removeStaleSig deletes objName's sibling ".sig" object. FIX-GO-RELEASES-STALE-SIG
// (S-go-worker-releases.md row 12): whenever NO new signature was produced for
// freshly-uploaded bytes (signing skipped because signingKey is nil, or signing/upload
// failed inside signAndUploadSig), the PREVIOUS release's ".sig" would otherwise still
// sit beside the new bytes and internal/selfupdate.VerifyArtifact would reject the new
// binary against the old signature - worker self-updates stall until the next signed
// publish. Removing it makes a verifier see "no signature" (the documented unsigned
// behaviour) instead of a stale one. Best-effort: a removal failure is logged at Warn
// and never aborts the publish. When signing succeeds the new sig overwrites as today,
// so this is not called on that path.
func removeStaleSig(ctx context.Context, client *minio.Client, objName string, log *slog.Logger) {
	sigName := selfupdate.SignatureURL(objName)
	if err := client.RemoveObject(ctx, bucket, sigName, minio.RemoveObjectOptions{}); err != nil {
		log.Warn("stale release signature removal failed", "object", sigName, "error", err)
		return
	}
	log.Info("removed stale release signature", "object", sigName)
}

func signAndUploadSig(ctx context.Context, client *minio.Client, priv *ecdsa.PrivateKey, objName string, data []byte, log *slog.Logger) {
	digest := sha256.Sum256(data)
	der, err := ecdsa.SignASN1(rand.Reader, priv, digest[:])
	if err != nil {
		log.Warn("skipping signature: sign failed", "object", objName, "error", err)
		// No new sig was produced: drop the previous release's .sig so a verifier
		// sees "no signature", not a stale one (FIX-GO-RELEASES-STALE-SIG).
		removeStaleSig(ctx, client, objName, log)
		return
	}
	sigB64 := []byte(base64.StdEncoding.EncodeToString(der))
	sigName := selfupdate.SignatureURL(objName)
	_, err = client.PutObject(ctx, bucket, sigName, bytes.NewReader(sigB64), int64(len(sigB64)), minio.PutObjectOptions{
		ContentType: "text/plain",
	})
	if err != nil {
		log.Warn("skipping signature: upload failed", "object", sigName, "error", err)
		// The new sig never landed: same stale-sig rule as the sign-failure branch.
		removeStaleSig(ctx, client, objName, log)
		return
	}
	log.Info("uploaded release signature", "object", sigName)
}
