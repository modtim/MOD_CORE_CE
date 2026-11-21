package executor

// GC.MWL.C5/C6 — manager-work-loans checkpoint hand-back. Gate 3
// (ensureCheckpointVolume, executor.go) already gives a node run a durable
// volume that SURVIVES a retry on the SAME host. This file is the other half:
// moving that volume's bytes to/from object storage so a retry landing on a
// DIFFERENT host can pick up where the last attempt left off, instead of
// starting cold every time the manager reschedules work across the fleet.
//
// The API mints the presigned PUT/GET URLs (GC.MWL.C7); this file has no
// bucket/key logic of its own — it only tars/untars a volume and streams the
// bytes through whatever URL it was handed.
//
// Helper image: checkpointHelperImage, not minioClientImage. The minio-client
// image (ghcr.io/modtim/minio-client, FROM minio/mc) was
// checked directly (docker run + `command -v tar/curl/wget`) and ships NONE
// of them — just the static `mc` binary and coreutils/bash. alpine:3.20 is
// already a helper image this executor pulls (readResultJSON's fallback,
// stacksnapshot.go's snapshot/restore/rehearse helpers) and carries both
// `tar` and `wget` out of the box, so reusing it here is not a new registry
// pull. The actual PUT/GET against the presigned URL is done by the worker
// process itself (net/http), not by a container-side HTTP client — busybox
// wget has no --method=PUT support, and doing the HTTP leg in the Go process
// mirrors stacksnapshot.go's snapshotVolume/restoreVolume exactly: the
// container's only job is tar/untar, streamed through stdout/stdin pipes.
import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// checkpointHelperImage tars/untars a checkpoint volume's contents. See the
// package-comment above for why this is alpine, not the minio-client image
// every other sidecar in this package uses.
const checkpointHelperImage = "alpine:3.20"

// checkpointHTTPDo is a seam over the default HTTP client so tests can stub
// the network leg without a real listener — same shape as execCommandContext
// (stackrunner.go) over exec.CommandContext.
var checkpointHTTPDo = func(req *http.Request) (*http.Response, error) {
	return http.DefaultClient.Do(req)
}

// ensureCheckpointVolumeFn is a seam over (*Executor).ensureCheckpointVolume
// (executor.go, Gate 3) so HydrateCheckpointVolume's tests can stub volume
// creation without a docker daemon. A method expression, not a new field on
// Executor — this file never touches the struct executor.go owns.
var ensureCheckpointVolumeFn = (*Executor).ensureCheckpointVolume

// redactCheckpointURL strips everything from the first '?' onward. The
// presigned URL's query string IS the credential (the signature that grants
// PUT/GET access to one object for a limited time) — leaking it into a log
// line or an error message hands out that access to anyone who can read
// worker logs. Used everywhere a download/upload URL could otherwise reach
// a log.Warn/log.Info call or an error returned up to the manager.
func redactCheckpointURL(rawURL string) string {
	if i := strings.IndexByte(rawURL, '?'); i >= 0 {
		return rawURL[:i] + "?REDACTED"
	}
	return rawURL
}

// checkpointVolumeExists is a cheap existence probe via `docker volume
// inspect`, run through the same execCommandContext seam every other
// docker-CLI helper in this package uses (stackreplica.go, stacklogical.go).
// A missing volume must be a plain error here, never a create-on-demand —
// ExportCheckpointVolume has nothing to export if the volume was never made.
func checkpointVolumeExists(ctx context.Context, volumeName string) error {
	cmd := execCommandContext(ctx, "docker", "volume", "inspect", volumeName)
	var stderr bytes.Buffer
	cmd.Stdout = io.Discard
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("volume %q not found: %v: %s", volumeName, err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

// checkpointVolumeHasContent reports whether a volume already holds at least
// one file. `-print -quit` stops `find` at the first hit, so this stays cheap
// even on a checkpoint volume with a lot of files.
func checkpointVolumeHasContent(ctx context.Context, volumeName string) (bool, error) {
	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-v", volumeName+":/data:ro",
		checkpointHelperImage, "sh", "-c", "find /data -mindepth 1 -print -quit")
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return false, fmt.Errorf("%v: %s", err, strings.TrimSpace(stderr.String()))
	}
	return strings.TrimSpace(stdout.String()) != "", nil
}

// ExportCheckpointVolume tars a checkpoint volume (mounted read-only into a
// short-lived `alpine tar` helper container) and streams the tar via a plain
// HTTP PUT to uploadURL, which the API minted for this purpose (GC.MWL.C7).
//
// The helper container always runs with --rm, so dockerd removes it the
// moment the tar process exits — on the PUT-succeeded path, the PUT-failed
// path, and the tar-itself-failed path alike. cmd.Wait() is always called
// (never left pending behind an early return) so that exit — and the
// container remove it triggers — is never skipped.
func (e *Executor) ExportCheckpointVolume(ctx context.Context, volumeName, uploadURL string) error {
	volumeName = strings.TrimSpace(volumeName)
	if volumeName == "" {
		return fmt.Errorf("export checkpoint: volume name is empty")
	}
	if err := checkpointVolumeExists(ctx, volumeName); err != nil {
		return fmt.Errorf("export checkpoint volume: %w", err)
	}

	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-v", volumeName+":/data:ro",
		checkpointHelperImage, "tar", "czf", "-", "-C", "/data", ".")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("export checkpoint volume %q: tar stdout pipe: %w", volumeName, err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("export checkpoint volume %q: start tar helper: %w", volumeName, err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPut, uploadURL, stdout)
	if err != nil {
		_ = cmd.Wait()
		return fmt.Errorf("export checkpoint volume %q: build PUT request: %w", volumeName, err)
	}
	req.Header.Set("Content-Type", "application/gzip")

	resp, doErr := checkpointHTTPDo(req)
	// Reap the helper container's process (and so the container itself, via
	// --rm) on every path out of this function — regardless of how the PUT
	// went.
	waitErr := cmd.Wait()

	if doErr != nil {
		return fmt.Errorf("export checkpoint volume %q: PUT %s: %w", volumeName, redactCheckpointURL(uploadURL), doErr)
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)

	if waitErr != nil {
		return fmt.Errorf("export checkpoint volume %q: tar helper: %v: %s", volumeName, waitErr, strings.TrimSpace(stderr.String()))
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("export checkpoint volume %q: upload to %s returned status %d",
			volumeName, redactCheckpointURL(uploadURL), resp.StatusCode)
	}
	return nil
}

// downloadCheckpointVolume GETs downloadURL and untars the response body,
// streamed straight into an `alpine tar` helper container bound read-write
// to volumeName — the restore-side mirror of ExportCheckpointVolume's tar
// step, following stacksnapshot.go's restoreVolume idiom.
func downloadCheckpointVolume(ctx context.Context, volumeName, downloadURL string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, downloadURL, nil)
	if err != nil {
		return fmt.Errorf("build GET request: %w", err)
	}
	resp, err := checkpointHTTPDo(req)
	if err != nil {
		return fmt.Errorf("GET %s: %w", redactCheckpointURL(downloadURL), err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, resp.Body)
		return fmt.Errorf("GET %s: status %d", redactCheckpointURL(downloadURL), resp.StatusCode)
	}

	cmd := execCommandContext(ctx, "docker", "run", "--rm", "-i", "-v", volumeName+":/data",
		checkpointHelperImage, "tar", "xzf", "-", "-C", "/data")
	cmd.Stdin = resp.Body
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("untar into volume %q: %v: %s", volumeName, err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

// HydrateCheckpointVolume ensures volumeName exists (ensureCheckpointVolumeFn
// — same idempotent create-with-keep-label as the same-host retry path) and,
// if it is still empty, downloads and untars downloadURL into it before the
// node container starts.
//
// If the volume already has content, this is a no-op: it could only have
// content because a retry on THIS host already populated it (ensureCheckpointVolume's
// same-host case) or because a hydrate already ran for this attempt, and in
// either case downloading over it would clobber live progress — the exact
// failure checkpointing exists to prevent. "Preference, never a lock": every
// error here is returned to the caller to log, and the caller starts the node
// cold rather than fail the task — a missing/slow checkpoint download must
// never be what blocks a node run.
func (e *Executor) HydrateCheckpointVolume(ctx context.Context, volumeName, downloadURL string) error {
	volumeName = strings.TrimSpace(volumeName)
	downloadURL = strings.TrimSpace(downloadURL)
	if volumeName == "" {
		return fmt.Errorf("hydrate checkpoint: volume name is empty")
	}
	if downloadURL == "" {
		return fmt.Errorf("hydrate checkpoint volume %q: no download URL", volumeName)
	}

	if err := ensureCheckpointVolumeFn(e, ctx, volumeName); err != nil {
		return fmt.Errorf("hydrate checkpoint volume %q: ensure volume: %w", volumeName, err)
	}

	hasContent, err := checkpointVolumeHasContent(ctx, volumeName)
	if err != nil {
		return fmt.Errorf("hydrate checkpoint volume %q: inspect contents: %w", volumeName, err)
	}
	if hasContent {
		if e.log != nil {
			e.log.Info("checkpoint volume already has content; skipping hydrate so a live retry's progress is never clobbered",
				"volume", volumeName)
		}
		return nil
	}

	if err := downloadCheckpointVolume(ctx, volumeName, downloadURL); err != nil {
		return fmt.Errorf("hydrate checkpoint volume %q from %s: %w", volumeName, redactCheckpointURL(downloadURL), err)
	}
	if e.log != nil {
		e.log.Info("checkpoint volume hydrated from a prior attempt", "volume", volumeName)
	}
	return nil
}
