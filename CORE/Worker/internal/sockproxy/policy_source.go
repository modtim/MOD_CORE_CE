package sockproxy

import (
"errors"
"os"
"path/filepath"
"strings"
)

// normPath returns the normalized absolute host path for s, or "" when
// s cannot be normalized safely: empty, containing a NUL byte, not
// absolute (after cleaning), or carrying a ".." component anywhere.
// filepath.Clean collapses repeated slashes, drops "." components and
// strips a trailing "/", so the string that reaches the deny/allow
// matching is the one the kernel would resolve at mount time; a source
// that still needs a ".." hop is rejected outright rather than guessed
// at, which keeps the deny-list fail-closed (S-1).
func normPath(s string) string {
	if s == "" || strings.IndexByte(s, 0) >= 0 {
		return ""
	}
	for _, c := range strings.Split(s, "/") {
		if c == ".." {
			return ""
		}
	}
	n := filepath.Clean(s)
	if n == "" || n[0] != '/' {
		return ""
	}
	for _, c := range strings.Split(n, "/") {
		if c == ".." {
			return ""
		}
	}
	return n
}

// hostPathDenied reports whether the normalized host path n hits the
// deny list in any of three ways: n equals a reserved path d, n lies
// below one (d + "/"), or n is an ANCESTOR of one (d starts with n +
// "/"). The ancestor rule closes the docker-socket exposure the first
// fix left open: a bind of /var/run mounts /var/run/docker.sock inside
// the container at mount time, exactly like binding the socket itself,
// and /run, /var, /var/lib and / do the same for their own reserved
// descendants. The "/" separator keeps a name that merely starts with a
// reserved name (/varfoo against /var) allowed.
func hostPathDenied(n string) bool {
	if n == "/" {
		return true
	}
	for _, d := range deniedHostPrefixes {
		if n == d || strings.HasPrefix(n, d+"/") || strings.HasPrefix(d, n+"/") {
			return true
		}
	}
	return false
}

// checkHostPath vets one already-normalized host path: the device
// allowlist wins first (GPU passthrough under the /dev catch-all), then
// the deny list (equal, descendant, ancestor).
func checkHostPath(n string, p *Policy) (bool, string) {
	if n == "/" {
		return false, "bind of the host root /"
	}
	if p.deviceAllowed(n) {
		return true, ""
	}
	if hostPathDenied(n) {
		return false, "bind of reserved host path " + n
	}
	return true, ""
}

// notFoundError reports whether an EvalSymlinks failure simply means
// "the path is not there yet": a bind source may be created by the task
// after the create call, so a missing source keeps the cleaned-only
// check. Permission denied, symlink loops and unreadable directories
// are not of that shape and fail closed.
func notFoundError(err error) bool {
	return errors.Is(err, os.ErrNotExist)
}

// resolveDeepest resolves n through symlinks even when its tail does not
// exist: the deepest existing ancestor is EvalSymlinks'd and the missing
// components re-appended; a dangling symlink is followed by hand (depth
// capped at 40). Any error other than not-exist is returned (fail closed).
func resolveDeepest(n string, depth int) (string, error) {
	if depth > 40 {
		return "", errors.New("too many levels")
	}
	cur := n
	var tail []string
	for {
		r, err := filepath.EvalSymlinks(cur)
		if err == nil {
			return filepath.Join(append([]string{r}, tail...)...), nil
		}
		if !notFoundError(err) {
			return "", err
		}
		if fi, lerr := os.Lstat(cur); lerr == nil && fi.Mode()&os.ModeSymlink != 0 {
			t, rerr := os.Readlink(cur)
			if rerr != nil {
				return "", rerr
			}
			if !filepath.IsAbs(t) {
				t = filepath.Join(filepath.Dir(cur), t)
			}
			return resolveDeepest(filepath.Join(append([]string{filepath.Clean(t)}, tail...)...), depth+1)
		}
		if cur == "/" || cur == "." {
			return "", err
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		cur = filepath.Dir(cur)
	}
}

// validateSource denies a host bind source that is (or lies under or
// above) a reserved host path. Allowed host sources are otherwise
// unrestricted: task working dirs (/data/...), model caches, WSL GPU
// library dirs (/usr/lib/wsl) all pass, and new ones pass by default -
// the deny-list, not an allow-list, is the unit of this policy. Two
// checks run over the source:
//
// 1. the cleaned/normalized form (normPath): traversal, double slash,
// trailing slash, dot components, relative and NUL sources cannot
// borrow an allowed prefix or slip past the prefix test.
// 2. the RESOLVED form (filepath.EvalSymlinks): a tenant container that
// can write an allowed bind dir can plant an absolute symlink
// (/data/<task>/x -> /var/run/docker.sock) whose cleaned form passes;
// dockerd resolves it at mount time and mounts the socket. The same
// deny checks (ancestor AND descendant) therefore run on the resolved
// path too. When the source (or its tail) does not exist yet (the task
// may create it later, and dockerd creates a missing Binds source as a
// host directory) resolveDeepest resolves the deepest EXISTING ancestor
// through symlinks, follows a dangling symlink by hand and re-appends
// the missing components, so a symlink planted above a missing tail
// cannot hide a denied target. Any failure (permission denied, symlink
// loop, more than 40 levels) is denied fail-closed.
//
// GPU devices are matched against the device allowlist BEFORE the deny
// list so /dev/nvidia0 et al. win over the /dev catch-all - on the
// normalized path, so /dev/dri/../../etc does not win either.
func validateSource(src string, p *Policy) (bool, string) {
	if src == "" {
		return false, "bind with empty host source"
	}
	n := normPath(src)
	if n == "" {
		return false, "bind source " + src + " is not a normalized absolute host path"
	}
	if ok, reason := checkHostPath(n, p); !ok {
		return false, reason
	}
	r, err := resolveDeepest(n, 0)
	if err != nil {
		return false, "bind source " + src + " cannot be resolved (symlink loop, too many levels or unreadable path)"
	}
	rn := normPath(r)
	if rn == "" {
		return false, "bind source " + src + " resolves to a path that is not a normalized absolute host path"
	}
	if ok, reason := checkHostPath(rn, p); !ok {
		return false, "bind source " + src + " resolves to " + rn + ": " + reason
	}
	return true, ""
}

// deviceAllowed reports whether src is a GPU/device passthrough path on
// the allowlist: equal to an entry, below it, or an entry followed by
// an ASCII-digit-only suffix (/dev/nvidia0, /dev/nvidia10). The suffix
// must be ALL digits and non-empty, so /dev/nvidia0etc is not a device,
// and digits of other scripts (Arabic-Indic, fullwidth) are not device
// numbers. The path is normalized first, so a formatted source cannot
// borrow an allowlisted prefix (/dev/dri/../../etc).
func (p *Policy) deviceAllowed(src string) bool {
	n := normPath(src)
	if n == "" {
		return false
	}
	list := p.DeviceAllowlist
	if len(list) == 0 {
		list = defaultDeviceAllowlist
	}
	for _, d := range list {
		if n == d {
			return true
		}
		if strings.HasPrefix(n, d+"/") {
			return true
		}
		if len(n) > len(d) && strings.HasPrefix(n, d) && allAsciiDigits(n[len(d):]) {
			return true
		}
	}
	return false
}

// allAsciiDigits reports whether s is non-empty and made only of ASCII
// digits (codepoint comparison, so no other script's digits count).
func allAsciiDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
