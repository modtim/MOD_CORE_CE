package executor

// UNDER1S-OUTPUT-SCAN-AND-UPLOAD: read the finished task's /data/output through
// the Docker Engine ARCHIVE API (GET /containers/{id}/archive) on a created-but-
// never-started container that mounts the task volume read-only, instead of two
// throwaway helper RUNS (create+start+wait+logs+remove for the NB1 result-file
// read, then again for `tar cf -`). One archive read yields the whole output
// tree as a tar; the three metadata files (validation_result.json,
// export_result.json, result.json) are parsed out of that SAME tar in process,
// and the tar BYTES ride the existing OPT4 hand-off into the staging pass and
// the durable upload, so a CE completion pays ONE create+copy+remove cycle
// where it used to pay two full helper runs (or three before OPT4/NB1).
//
// Safety posture (all unchanged or stricter):
//   - the volume is bound :ro and the helper never starts (no process, no
//     network, nothing can write to the volume);
//   - the same ceStagingImage helper image is used for the create, so the
//     image is already present (pulled at volume-create) and no new image is
//     named; sidecarLabels() keeps the cleanup sweep able to reap a leak;
//   - the Go side keeps owning the untar trust boundary (ceReadTarIntoDir);
//   - symlink entries are NOT followed when reading the metadata files (the
//     old `cat` helper followed them inside the helper rootfs), so a planted
//     symlink reads as "missing", never as a helper-image file;
//   - never fails a task: every error returns (nil, err) and the caller falls
//     back to the verbatim helper path (readResultJSONFiles + docker tar).

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"io"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"
)

// ceArchiveReadEnabled is the safe switch: true = try the archive read first
// (CE posture only); false = the pre-change helper-run path, byte for byte.
// An archive error ALSO falls back at the call site, so the switch only needs
// to flip if a daemon is found where the archive endpoint misbehaves silently.
var ceArchiveReadEnabled = true

// ceOutputArchivePath is the directory (with the trailing "/.") whose CONTENTS
// the daemon tars, so entry names match `tar cf - -C /data/output .`.
const ceOutputArchivePath = "/data/output/."

// ceReadOutputTarViaArchive returns the tar of the task volume's /data/output.
// The helper container is created (never started) and removed on a background
// ctx so teardown still runs when the task ctx is canceled. The read is capped
// by resultJSONReadTimeout, the same 2 minute ceiling the helper wait had.
func (e *Executor) ceReadOutputTarViaArchive(ctx context.Context, volName, helperImage string) ([]byte, error) {
	if e == nil || e.docker == nil {
		return nil, fmt.Errorf("ce archive read: no docker client")
	}
	if helperImage == "" {
		helperImage = "alpine:3.20"
	}
	rctx, cancel := context.WithTimeout(ctx, resultJSONReadTimeout)
	defer cancel()
	resp, err := e.docker.ContainerCreate(rctx,
		&container.Config{
			Image:           helperImage,
			Entrypoint:      []string{"/bin/true"},
			Labels:          e.sidecarLabels(),
			NetworkDisabled: true,
		},
		// HELPER-CONTAINERS-LOCKDOWN: created-never-started twin of the
		// read helper: align its HostConfig with the shared never-started
		// posture (network none, cap drop ALL, no-new-privileges, limits).
		// The Config-level NetworkDisabled above already stops any network
		// at start; this stops one at create too. Nothing runs here, so
		// the read bytes cannot change.
		e.archiveReadHostConfig(volName),
		nil, nil, "",
	)
	if err != nil {
		return nil, fmt.Errorf("ce archive read: create: %w", err)
	}
	defer func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer rmCancel()
		e.docker.ContainerRemove(rmCtx, resp.ID, container.RemoveOptions{Force: true})
	}()
	rc, _, err := e.docker.CopyFromContainer(rctx, resp.ID, ceOutputArchivePath)
	if err != nil {
		return nil, fmt.Errorf("ce archive read: copy from %q: %w", volName, err)
	}
	defer rc.Close()
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, rc); err != nil {
		return nil, fmt.Errorf("ce archive read: stream %q: %w", volName, err)
	}
	return buf.Bytes(), nil
}

// ceFilesFromTar is the pure parse half: it returns, for every requested
// top-level file name, that regular file's content trimmed exactly like the
// helper-cat path (strings.TrimSpace), or "" when absent / not a regular file.
// Every requested name is always present in the map, matching
// splitHelperCatOutput's contract so the downstream parsers are unchanged.
func ceFilesFromTar(tarBytes []byte, names []string) map[string]string {
	out := make(map[string]string, len(names))
	want := make(map[string]bool, len(names))
	for _, n := range names {
		out[n] = ""
		want[n] = true
	}
	tr := tar.NewReader(bytes.NewReader(tarBytes))
	for {
		hdr, err := tr.Next()
		if err != nil {
			break
		}
		if hdr.Typeflag != tar.TypeReg && hdr.Typeflag != tar.TypeRegA {
			continue
		}
		name := strings.TrimPrefix(path.Clean(hdr.Name), "./")
		if !want[name] || strings.Contains(name, "/") {
			continue
		}
		b, rerr := io.ReadAll(tr)
		if rerr != nil {
			continue
		}
		out[name] = strings.TrimSpace(string(b))
	}
	return out
}

// --- prefetched-tar slot: the archive read parks its tar here for the staging
// pass of the SAME task (keyed volume|taskID, one-shot, mutex-guarded, dropped
// on every exit of Run). A miss means "tar it yourself" (the helper path).
var cePrefetchMu sync.Mutex
var cePrefetchKey = ""
var cePrefetchTar = []byte{}

func cePrefetchKeyFor(volName string, taskID int) string {
	return volName + "|" + strconv.Itoa(taskID)
}

func cePrefetchOutputTar(volName string, taskID int, tarBytes []byte) {
	cePrefetchMu.Lock()
	cePrefetchKey = cePrefetchKeyFor(volName, taskID)
	cePrefetchTar = tarBytes
	cePrefetchMu.Unlock()
}

// ceTakePrefetchedTar claims the parked tar for this exact volume+task once.
func ceTakePrefetchedTar(volName string, taskID int) ([]byte, bool) {
	key := cePrefetchKeyFor(volName, taskID)
	cePrefetchMu.Lock()
	defer cePrefetchMu.Unlock()
	if key != cePrefetchKey || len(cePrefetchTar) == 0 {
		return []byte{}, false
	}
	t := cePrefetchTar
	cePrefetchKey, cePrefetchTar = "", []byte{}
	return t, true
}

// ceDropPrefetchedTar releases the slot if (and only if) it still belongs to
// this task, so an early return never keeps a tenant's output bytes in memory.
func ceDropPrefetchedTar(volName string, taskID int) {
	cePrefetchMu.Lock()
	if cePrefetchKey == cePrefetchKeyFor(volName, taskID) {
		cePrefetchKey, cePrefetchTar = "", []byte{}
	}
	cePrefetchMu.Unlock()
}
