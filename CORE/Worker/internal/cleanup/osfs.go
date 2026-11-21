package cleanup

import (
	"context"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
)

// modelTouchMarker is the sidecar the executor writes (touchModel) with the
// last-used time of each model dir; Atime resolution mirrors
// listModelDirs in internal/executor/modelcache_gc.go: marker mtime first,
// directory mtime otherwise.
const modelTouchMarker = ".mod-last-used"

// modelDirPrefix is the HF-cache layout prefix every eviction unit starts
// with ("models--org--name").
const modelDirPrefix = "models--"

// nativeSubdir is where the native backend keeps its per-task scratch dirs
// (NewNativeBackend is constructed with filepath.Join(scratch, "native")).
const nativeSubdir = "native"

// OSFS is the production FS implementation. workRoot is the worker's own
// task work root (/var/lib/mod-worker or the temp fallback); the scan is
// positively scoped to numeric-named children of workRoot and of
// workRoot/native, which is what makes it safe: no other work-root child
// ("stacks", "outbox", seccomp profiles, ...) is shaped like a task ID.
type OSFS struct {
	workRoot string
	log      *slog.Logger
}

// NewOSFS builds the OS FS adapter. A nil/empty workRoot disables the task
// dir scan (ListTaskDirs returns empty); model operations are unaffected.
func NewOSFS(workRoot string, log *slog.Logger) *OSFS {
	if log == nil {
		log = slog.Default()
	}
	return &OSFS{workRoot: workRoot, log: log}
}

// numericTaskName reports whether name looks like a queue task ID (all
// digits, non-empty, bounded length). Task IDs are ints; anything else
// under the work root is out of scope by shape.
func numericTaskName(name string) bool {
	if name == "" || len(name) > 20 {
		return false
	}
	for _, r := range name {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// ListTaskDirs enumerates orphan-task candidates: numeric-named directories
// directly under the work root (wslc task dirs) and under workRoot/native
// (native task scratch dirs). ModifiedAt is the directory mtime; Size is a
// best-effort walk (errors mid-walk are ignored, matching the executor's
// dirSizeBytes).
func (f *OSFS) ListTaskDirs(ctx context.Context) ([]TaskDir, error) {
	if f.workRoot == "" {
		return nil, nil
	}
	var out []TaskDir
	for _, dir := range []string{f.workRoot, filepath.Join(f.workRoot, nativeSubdir)} {
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		for _, e := range entries {
			if !e.IsDir() || !numericTaskName(e.Name()) {
				continue
			}
			path := filepath.Join(dir, e.Name())
			info, err := e.Info()
			if err != nil {
				continue
			}
			out = append(out, TaskDir{
				Path:       path,
				TaskID:     e.Name(),
				ModifiedAt: info.ModTime(),
				Size:       dirSizeBytes(path),
			})
		}
	}
	return out, nil
}

// RemoveTaskDir deletes a task dir after re-resolving it inside the work
// root (a symlink planted between list and delete must not redirect the
// delete elsewhere).
func (f *OSFS) RemoveTaskDir(ctx context.Context, path string) error {
	if f.workRoot == "" {
		return os.ErrNotExist
	}
	real, err := safeWithinRoot(f.workRoot, path)
	if err != nil {
		return err
	}
	return os.RemoveAll(real)
}

// ListModelDirs enumerates eviction units under root: children matching the
// models-- prefix. Atime resolves the touch marker first, then the dir
// mtime — identical to the executor's listModelDirs.
func (f *OSFS) ListModelDirs(ctx context.Context, root string) ([]ModelDir, error) {
	if root == "" {
		return nil, nil
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var out []ModelDir
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), modelDirPrefix) {
			continue
		}
		path := filepath.Clean(filepath.Join(root, e.Name()))
		info, err := e.Info()
		if err != nil {
			continue
		}
		lastUsed := info.ModTime()
		if mi, merr := os.Stat(filepath.Join(path, modelTouchMarker)); merr == nil {
			lastUsed = mi.ModTime()
		}
		out = append(out, ModelDir{
			Path:  path,
			Size:  dirSizeBytes(path),
			Atime: lastUsed,
		})
	}
	return out, nil
}

// RemoveModelDir deletes one model dir after re-resolving it inside root.
func (f *OSFS) RemoveModelDir(ctx context.Context, root, path string) error {
	if root == "" {
		return os.ErrPermission
	}
	real, err := safeWithinRoot(root, path)
	if err != nil {
		return err
	}
	return os.RemoveAll(real)
}

// safeWithinRoot refuses to operate on anything that resolves (after
// symlink evaluation) outside root.
func safeWithinRoot(root, path string) (string, error) {
	cleanRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", err
	}
	real = filepath.Clean(real)
	cleanRoot = filepath.Clean(cleanRoot)
	if real != cleanRoot && !strings.HasPrefix(real, cleanRoot+string(os.PathSeparator)) {
		return "", os.ErrPermission
	}
	return real, nil
}

// dirSizeBytes sums regular-file sizes under path; errors mid-walk are
// ignored (a file may be removed concurrently).
func dirSizeBytes(path string) int64 {
	var total int64
	_ = filepath.WalkDir(path, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.Type().IsRegular() {
			if info, ierr := d.Info(); ierr == nil {
				total += info.Size()
			}
		}
		return nil
	})
	return total
}
