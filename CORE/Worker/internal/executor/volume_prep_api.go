package executor

// UNDER1S-VOLUME-PREP: the per-task /data seed through the Docker Engine API
// instead of a throwaway shell container.
//
// prepareDataVolume used to run `mkdir -p /data/input /data/output; chmod 0777
// ...` in a helper container (create/start/wait/logs/remove). Measured on gpu5
// (docker 29.1.3, alpine:3.20, n=30): that costs 350 ms median, i.e. the
// docker cold-container floor (`docker run --rm alpine true` is 350 ms there),
// for work that is three mkdirs. The same result is reached with a helper
// container that is created but NEVER started plus one archive PUT of a tiny
// tar that carries the three directories with mode 0777 and uid/gid 0: 75 ms
// median including the volume create. The container only exists so the daemon
// has a mount namespace to extract into; it is removed in the background and
// cleanupVolume joins that removal before it removes the volume.
//
// The PUT target is "/" with entries data/, data/input/, data/output/: a PUT
// to "/data" with a "./" entry leaves the existing volume root at 0755
// (measured), which would differ from the chmod 0777 /data the node contract
// has always had. Mode, owner, labels, bind and "no files" are asserted by the
// tests; the three directories are stat-ed back after the PUT and any mode
// other than 0777 fails the prep, so the log line still reports real state.

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
)

// dataSeedDirs is the seed layout, in the order the legacy ls printed it.
var dataSeedDirs = []string{"/data", "/data/input", "/data/output"}

// dataSeedTar builds the archive extracted at "/" of the prep container.
func dataSeedTar(now time.Time) (*bytes.Buffer, error) {
	var b bytes.Buffer
	tw := tar.NewWriter(&b)
	for _, d := range dataSeedDirs {
		if err := tw.WriteHeader(&tar.Header{
			Name: strings.TrimPrefix(d, "/") + "/", Typeflag: tar.TypeDir,
			Mode: 0o777, Uid: 0, Gid: 0, Uname: "root", Gname: "root", ModTime: now,
		}); err != nil {
			return nil, err
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	return &b, nil
}

// lsLine renders one `ls -ld` line the way the busybox helper printed it
// (mode, links, owner, group, size, mtime, path), from the stat the daemon
// returned after the PUT. Links are the directory link counts of this layout.
func lsLine(mode os.FileMode, links int, size int64, mtime time.Time, path string) string {
	return fmt.Sprintf("%s %4d %-8s %-8s %9d %s %s", mode.String(), links, "root", "root", size,
		mtime.UTC().Format("Jan _2 15:04"), path)
}

// prepareDataVolumeAPI seeds the volume without starting any container. It
// returns the same "prep OK: ..." text the shell helper echoed.
func (e *Executor) prepareDataVolumeAPI(ctx context.Context, helperImage, volName string) (string, error) {
	e.waitPrepRemoval(volName) // a retry never leaves the previous attempt's removal untracked
	resp, err := e.docker.ContainerCreate(ctx,
		&container.Config{
			Image:      helperImage,
			Entrypoint: []string{"/bin/sh"},
			Cmd:        []string{"-c", "true"},
			Labels:     e.sidecarLabels(),
		},
		// HELPER-CONTAINERS-LOCKDOWN: this container is created but NEVER
		// started (the seed is one archive PUT into its mount namespace), so
		// it must never be attached to a network: the shared never-started
		// posture (helper_posture.go prepHostConfig) gives network none +
		// cap drop ALL + no-new-privileges + safety limits, with the rw bind
		// the PUT extracts into. Zero behaviour change: nothing runs.
		e.prepHostConfig(volName),
		&network.NetworkingConfig{}, nil, "")
	if err != nil {
		return "", fmt.Errorf("create prep container: %w", err)
	}
	id := resp.ID
	// Removal is off the critical path (measured 37 ms median). cleanupVolume
	// waits for it, so the volume is never removed while this container exists.
	done := make(chan struct{})
	e.prepRemovals.Store(volName, done)
	defer func() {
		go func() {
			defer close(done)
			rmCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			e.docker.ContainerRemove(rmCtx, id, container.RemoveOptions{Force: true})
		}()
	}()

	tarBuf, err := dataSeedTar(time.Now())
	if err != nil {
		return "", fmt.Errorf("build seed archive: %w", err)
	}
	if err := e.docker.CopyToContainer(ctx, id, "/", tarBuf, container.CopyToContainerOptions{}); err != nil {
		return "", fmt.Errorf("seed /data: %w", err)
	}
	lines := make([]string, len(dataSeedDirs))
	errs := make([]error, len(dataSeedDirs))
	var wg sync.WaitGroup
	for i, d := range dataSeedDirs {
		wg.Add(1)
		go func(i int, d string) {
			defer wg.Done()
			st, serr := e.docker.ContainerStatPath(ctx, id, d)
			if serr != nil {
				errs[i] = serr
				return
			}
			if st.Mode != os.ModeDir|0o777 {
				errs[i] = fmt.Errorf("%s has mode %s, want drwxrwxrwx", d, st.Mode)
				return
			}
			links := 2
			if d == "/data" {
				links = 4
			}
			lines[i] = lsLine(st.Mode, links, st.Size, st.Mtime, d)
		}(i, d)
	}
	wg.Wait()
	for _, serr := range errs {
		if serr != nil {
			return "", fmt.Errorf("verify /data seed: %w", serr)
		}
	}
	return "prep OK: " + strings.Join(lines, " ") + " ", nil
}

// waitPrepRemoval blocks (bounded) until the prep container removal for volName
// has finished, then forgets it.
func (e *Executor) waitPrepRemoval(volName string) {
	v, ok := e.prepRemovals.LoadAndDelete(volName)
	if !ok {
		return
	}
	select {
	case <-v.(chan struct{}):
	case <-time.After(15 * time.Second):
	}
}
