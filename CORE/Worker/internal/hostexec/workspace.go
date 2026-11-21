package hostexec

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// RunFunc is the injectable callback used by Workspace to execute git commands
// without invoking a shell. argv[0] is the binary, remaining elements are args.
type RunFunc func(ctx context.Context, argv []string, cwd string) (exitCode int, out []byte, err error)

// Workspace manages a root directory and tenant-scoped subdirectories used by
// the worker-host-exec flow (hx.14a).
type Workspace struct {
	Root string
}

// NewWorkspace creates a Workspace rooted at the given absolute path.  The
// root directory is created with mode 0750 if it does not already exist.
func NewWorkspace(root string) (*Workspace, error) {
	root = filepath.Clean(root)
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("hostexec: workspace root must be absolute, got %q", root)
	}
	if err := os.MkdirAll(root, 0750); err != nil {
		return nil, fmt.Errorf("hostexec: creating workspace root %q: %w", root, err)
	}
	return &Workspace{Root: root}, nil
}

// TenantDir returns the path for tenantID under the workspace root.
// tenantID 0 is treated as "platform" rather than "t0".
func (w *Workspace) TenantDir(tenantID int64) (string, error) {
	dirName := fmt.Sprintf("t%d", tenantID)
	if tenantID == 0 {
		dirName = "platform"
	}
	tDir := filepath.Join(w.Root, dirName)
	if err := os.MkdirAll(tDir, 0750); err != nil {
		return "", fmt.Errorf("hostexec: creating tenant dir %q: %w", tDir, err)
	}
	return tDir, nil
}

// Resolve resolves a relative path inside tenantDir into an absolute path
// and enforces jail constraints: no ".." escapes, no absolute rel, and no
// symlink-escape through the deepest existing parent.
func (w *Workspace) Resolve(tenantDir, rel string) (string, error) {
	if filepath.IsAbs(rel) {
		return "", fmt.Errorf("hostexec: resolve: rel must not be absolute, got %q", rel)
	}
	if strings.Contains(rel, "..") {
		return "", fmt.Errorf("hostexec: resolve: rel must not contain \"..\", got %q", rel)
	}
	cleaned := filepath.Clean(filepath.Join(tenantDir, rel))
	// Make sure the cleaned path still starts under tenantDir.
	if !strings.HasPrefix(cleaned, tenantDir+string(os.PathSeparator)) && cleaned != tenantDir {
		return "", fmt.Errorf("hostexec: resolve: path escapes tenantDir")
	}

	// Symlink-escape check: walk the deepest existing parent of rel and
	// verify that resolving symlinks on it still keeps us under tenantDir.
	// We need to walk from tenantDir down, resolving each component that is
	// a symlink, to catch symlink chains that redirect outside.
	checkBase := tenantDir
	components := strings.Split(rel, string(os.PathSeparator))
	for i, comp := range components {
		if comp == "" {
			continue
		}
		checkBase = filepath.Join(checkBase, comp)
		if i == len(components)-1 {
			continue // last component may not exist yet
		}
		info, err := os.Lstat(checkBase)
		if err != nil {
			break // component doesn't exist yet — stop check here
		}
		if info.Mode()&os.ModeSymlink != 0 {
			resolved, err := os.Readlink(checkBase)
			if err != nil {
				return "", fmt.Errorf("hostexec: resolve: reading symlink %q: %w", checkBase, err)
			}
			if filepath.IsAbs(resolved) {
				resolved = filepath.Dir(resolved)
			} else {
				resolved = filepath.Join(filepath.Dir(checkBase), resolved)
			}
			resolved = filepath.Clean(resolved)
			if !strings.HasPrefix(resolved, tenantDir+string(os.PathSeparator)) && resolved != tenantDir {
				return "", fmt.Errorf("hostexec: resolve: symlink %q escapes tenantDir", checkBase)
			}
		}
	}
	// Also check the target path itself: if it exists and is a symlink, make
	// sure the final resolved path is still under tenantDir.
	if _, err := os.Stat(cleaned); err == nil {
		// Path exists — verify via EvalSymlinks.
		evaluated, err := filepath.EvalSymlinks(cleaned)
		if err != nil {
			return "", fmt.Errorf("hostexec: resolve: evaluating symlinks for %q: %w", cleaned, err)
		}
		evaluated = filepath.Clean(evaluated)
		if !strings.HasPrefix(evaluated, tenantDir+string(os.PathSeparator)) && evaluated != tenantDir {
			return "", fmt.Errorf("hostexec: resolve: path escapes tenantDir through symlinks")
		}
	}
	return cleaned, nil
}

// EnsureRepo ensures a git repository exists at repoDir inside tenantDir.
// If the directory does not exist, it clones from url; otherwise it runs
// git fetch --prune followed by git checkout --detach <ref>.  The url must
// be https://, ssh://, or a local absolute path under the workspace Root.
func (w *Workspace) EnsureRepo(ctx context.Context, tenantDir, name, url, ref string, run RunFunc) (string, error) {
	repoDir := filepath.Join(tenantDir, name)
	if run == nil {
		return "", fmt.Errorf("hostexec: EnsureRepo: run function is required")
	}
	if !isAllowedURL(url, w.Root) {
		// The rejected URL can itself carry a private token: scrub it.
		return "", fmt.Errorf("hostexec: EnsureRepo: url %q must be https://, ssh://, or a local path under workspace root", scrubURL(url))
	}
	if err := validateDirName(name); err != nil {
		return "", fmt.Errorf("hostexec: EnsureRepo: %w", err)
	}

	if _, err := os.Stat(repoDir); os.IsNotExist(err) {
		// Clone the repo.
		args := []string{"clone", "--mirror", url, repoDir}
		code, out, err := run(ctx, args, tenantDir)
		if err != nil {
			// SECURITY: scrub the URL and cap the raw git output: git echoes
			// the clone URL (with any private token) verbatim on stderr and
			// this error text reaches the task log.
			return "", fmt.Errorf("hostexec: EnsureRepo: clone %q: exit=%d output=%s err=%s", scrubURL(url), code, capGitOutput(out), errText(err))
		}
		return repoDir, nil
	}

	// Repo exists — fetch and checkout.
	fetchArgs := []string{"-C", repoDir, "fetch", "--prune", "origin"}
	code, out, err := run(ctx, fetchArgs, tenantDir)
	if err != nil {
		return "", fmt.Errorf("hostexec: EnsureRepo: fetch %q: exit=%d output=%s err=%s", scrubURL(url), code, capGitOutput(out), errText(err))
	}

	checkoutArgs := []string{"-C", repoDir, "checkout", "--detach", ref}
	code, out, err = run(ctx, checkoutArgs, tenantDir)
	if err != nil {
		return "", fmt.Errorf("hostexec: EnsureRepo: checkout %q: exit=%d output=%s err=%s", scrubURL(ref), code, capGitOutput(out), errText(err))
	}
	return repoDir, nil
}

// EnsureWorktree ensures a detached worktree named wtName under repoDir.
// wtName must match [A-Za-z0-9._-]{1,64}.
func (w *Workspace) EnsureWorktree(ctx context.Context, repoDir, wtName, ref string, run RunFunc) (string, error) {
	if run == nil {
		return "", fmt.Errorf("hostexec: EnsureWorktree: run function is required")
	}
	if err := validateDirName(wtName); err != nil {
		return "", fmt.Errorf("hostexec: EnsureWorktree: %w", err)
	}

	wtPath := filepath.Join(repoDir, "worktrees", wtName)
	if _, err := os.Stat(wtPath); os.IsNotExist(err) {
		// Create a new detached worktree.
		args := []string{"-C", repoDir, "worktree", "add", "--detach", wtPath, ref}
		code, out, err := run(ctx, args, repoDir)
		if err != nil {
			// wtName is validated ([A-Za-z0-9._-]) so it cannot carry a URL,
			// but git's output can echo the remote URL: scrub and cap it.
			return "", fmt.Errorf("hostexec: EnsureWorktree: create worktree %q: exit=%d output=%s err=%s", wtName, code, capGitOutput(out), errText(err))
		}
		return wtPath, nil
	}

	// Worktree exists — checkout the ref.
	args := []string{"-C", wtPath, "checkout", "--detach", ref}
	code, out, err := run(ctx, args, repoDir)
	if err != nil {
		return "", fmt.Errorf("hostexec: EnsureWorktree: checkout worktree %q: exit=%d output=%s err=%s", wtName, code, capGitOutput(out), errText(err))
	}
	return wtPath, nil
}

// isAllowedURL checks that url is https://, ssh://, or an absolute path
// under the given workspace root.
func isAllowedURL(url string, root string) bool {
	if strings.HasPrefix(url, "https://") || strings.HasPrefix(url, "ssh://") {
		return true
	}
	if !filepath.IsAbs(url) {
		return false
	}
	absURL, err := filepath.Abs(url)
	if err != nil {
		return false
	}
	absRoot := filepath.Clean(root)
	// Check that absURL is under absRoot (or equals it).
	if absURL == absRoot {
		return true
	}
	if strings.HasPrefix(absURL, absRoot+string(filepath.Separator)) {
		return true
	}
	return false
}

// Error-text scrubbing (SECURITY): git echoes the clone URL verbatim on
// stderr ("fatal: unable to access 'https://user:TOKEN@host/repo.git/'"), and
// these error strings reach the TASK LOG, so a private-clone credential must
// never be copied into them.  The helpers work on the TEXT (not just the one
// url variable) so URLs quoted inside git's stderr are caught too.

// maxGitErrBytes caps raw git output embedded in an error string.
const maxGitErrBytes = 512

const truncatedSuffix = "...(truncated)"

// urlUserPattern matches URL userinfo: scheme://user:pass@host or
// scheme://token@host.  The userinfo must end before the first '/', so an
// '@' that appears in a path is left alone, and scheme-less ssh forms
// (git@host:path) are untouched.
var urlUserPattern = regexp.MustCompile(`(?i)([a-z][a-z0-9.+-]*://)(?:[^/@\s]+:[^/@\s]*@|[^/@\s]+@)`)

// credQueryPattern matches obvious credential query values (token=,
// access_token=, password=, passwd=, key=).
var credQueryPattern = regexp.MustCompile(`(?i)([?&](?:access_token|token|passwd|password|key)=)[^\s&'"]*`)

// scrubURL replaces every URL userinfo and credential query value in s with a
// fixed placeholder.  Unexported: used only for error/log text in this package.
func scrubURL(s string) string {
	s = urlUserPattern.ReplaceAllString(s, "$1***@")
	return credQueryPattern.ReplaceAllString(s, "$1"+redactedValuePlaceholder)
}

// capGitOutput scrubs raw git output first (so a URL cut mid-way cannot leak),
// trims whitespace, and caps it at maxGitErrBytes with truncatedSuffix when
// cut.  The cut never splits a UTF-8 sequence.
func capGitOutput(b []byte) string {
	s := strings.TrimSpace(scrubURL(string(b)))
	if len(s) <= maxGitErrBytes {
		return s
	}
	cut := maxGitErrBytes
	// Walk back off a UTF-8 continuation byte at the cut point.
	for cut > 0 && (int(s[cut]) & 0xC0) == 0x80 {
		cut--
	}
	return s[:cut] + truncatedSuffix
}

// errText is a nil-safe scrubbed rendering of an error's message; the message
// is capped too so a huge RunFunc error cannot bloat the task log.
func errText(err error) string {
	if err == nil {
		return ""
	}
	return capGitOutput([]byte(err.Error()))
}

var validDirName = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

func validateDirName(name string) error {
	if !validDirName.MatchString(name) {
		return fmt.Errorf("hostexec: invalid dir name %q (must match [A-Za-z0-9._-]{1,64})", name)
	}
	return nil
}
