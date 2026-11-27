package executor

// UNDER1S-OUTPUT-HARBOR-PAID: the registry (Harbor/paid) posture twin of the
// metadata read in ce_archive_read.go. The completion leg used to pay ONE
// throwaway helper RUN (create+start+wait+logs+remove, the NB1 merged read in
// readResultJSONFiles) to cat three small JSON files. This reads the same files
// through the Docker Engine ARCHIVE API instead: a created-but-never-started
// helper binds the task volume :ro, and one archive GET per metadata file
// returns a tiny single-entry tar that is parsed in process.
//
// Deliberate differences from the full-tree read the CE posture uses:
//   - PER-FILE archive GETs, not one GET of the whole /data/output tree: the
//     registry posture's later legs (the stage-for-scan copy and the durable
//     upload) read the VOLUME directly through their own helpers, so there is no
//     tar consumer here and pulling a 100 MB output tree into worker RAM just to
//     look at three small files would be pure cost (the closer's F-2: the CE
//     prefetch holds the tar twice at its peak). Nothing is parked or retained.
//   - results are IDENTICAL to the helper read, never "stricter": a file that
//     is absent reads "" (the helper's `cat` missing-file contract), and ANYTHING
//     else that is not a plain small regular file (a symlink, a directory, a
//     size claim over the cap, an unreadable entry, any daemon error) returns an
//     error so the caller runs the verbatim helper read, which keeps following a
//     symlink exactly as before. (The CE posture reads a symlinked result.json
//     as missing because its whole tree is refused at staging anyway; here the
//     helper-copy legs would still accept that tree, so changing the answer
//     would change what a task reports.)
//   - same safety envelope as the helper it replaces: ceStagingImage helper
//     (already present), :ro bind, sidecarLabels, NetworkDisabled (stricter than
//     the cat helper), the 2 minute resultJSONReadTimeout, teardown on a
//     background ctx so it runs when the task ctx is already canceled, and the
//     never-fail-the-task rule (the caller falls back, never aborts).

import (
	"archive/tar"
	"context"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/errdefs"
)

// harborArchiveReadEnabled is the safe switch for the registry posture: true =
// try the archive read first; false = the helper-run read, byte for byte. An
// archive error ALSO falls back at the call site.
var harborArchiveReadEnabled = true

// archiveMetaMaxBytes caps one metadata file read through the archive. Real
// result files are a few KB; anything over the cap is handed to the helper read
// (which has no cap) so a large file is never silently truncated.
const archiveMetaMaxBytes = 32 << 20

// readResultFilesViaArchive returns, for every requested name, the trimmed file
// content ("" when the file does not exist). Every name is present in the map,
// matching splitHelperCatOutput's contract. A non-nil error means "use the
// helper read": the caller must discard the map.
func (e *Executor) readResultFilesViaArchive(ctx context.Context, volName, helperImage string,
	names []string) (map[string]string, error) {
	if e == nil || e.docker == nil {
		return nil, fmt.Errorf("archive read: no docker client")
	}
	for _, n := range names {
		if n == "" || n == "." || n == ".." || strings.ContainsAny(n, "/\\") {
			return nil, fmt.Errorf("archive read: bad name %q", n)
		}
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
		// HELPER-CONTAINERS-LOCKDOWN: same never-started posture as the
		// ce_archive_read twin (network none, cap drop ALL, no-new-privi-
		// leges, safety limits on the create; nothing ever runs here).
		e.archiveReadHostConfig(volName),
		nil, nil, "",
	)
	if err != nil {
		return nil, fmt.Errorf("archive read: create: %w", err)
	}
	defer func() {
		rmCtx, rmCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer rmCancel()
		e.docker.ContainerRemove(rmCtx, resp.ID, container.RemoveOptions{Force: true})
	}()
	out := make(map[string]string, len(names))
	for _, n := range names {
		out[n] = ""
		rc, stat, cerr := e.docker.CopyFromContainer(rctx, resp.ID, "/data/output/"+n)
		if cerr != nil {
			if errdefs.IsNotFound(cerr) {
				continue // absent: same as `cat` on a missing file
			}
			return nil, fmt.Errorf("archive read: copy %q: %w", n, cerr)
		}
		body, rerr := archiveSingleRegularFile(rc, stat.Mode, n)
		rc.Close()
		if rerr != nil {
			return nil, rerr
		}
		out[n] = strings.TrimSpace(body)
	}
	return out, nil
}

// archiveSingleRegularFile is the pure parse half: it accepts ONLY a tar holding
// exactly one regular file named want, no larger than archiveMetaMaxBytes, whose
// stat did not report a link/dir/device. Anything else is an error (-> helper
// read) so the answer can never differ from the old `cat`.
func archiveSingleRegularFile(r io.Reader, statMode os.FileMode, want string) (string, error) {
	if statMode&os.ModeSymlink != 0 || statMode&os.ModeDir != 0 || !statMode.IsRegular() {
		return "", fmt.Errorf("archive read: %q is not a plain file (mode %v)", want, statMode)
	}
	tr := tar.NewReader(r)
	hdr, err := tr.Next()
	if err != nil {
		return "", fmt.Errorf("archive read: %q: %w", want, err)
	}
	if hdr.Typeflag != tar.TypeReg && hdr.Typeflag != tar.TypeRegA {
		return "", fmt.Errorf("archive read: %q entry type %q is not a regular file", want, string(hdr.Typeflag))
	}
	if hdr.Name != want {
		return "", fmt.Errorf("archive read: entry %q is not the requested %q", hdr.Name, want)
	}
	if hdr.Size < 0 || hdr.Size > archiveMetaMaxBytes {
		return "", fmt.Errorf("archive read: %q claims %d bytes (cap %d)", want, hdr.Size, archiveMetaMaxBytes)
	}
	b, err := io.ReadAll(io.LimitReader(tr, hdr.Size+1))
	if err != nil {
		return "", fmt.Errorf("archive read: %q body: %w", want, err)
	}
	if int64(len(b)) != hdr.Size {
		return "", fmt.Errorf("archive read: %q short or long body (%d of %d)", want, len(b), hdr.Size)
	}
	if _, nerr := tr.Next(); nerr != io.EOF {
		return "", fmt.Errorf("archive read: %q archive holds more than one entry", want)
	}
	return string(b), nil
}
