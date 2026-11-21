//go:build !windows

package sockproxy

import (
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"syscall"
)

// unix-only half of the proxy (see the note in proxy.go): ensurePrivateDir
// and Server.Start reach syscall.Stat_t and syscall.Umask, which do not
// exist under GOOS=windows, so they live here behind //go:build !windows and
// proxy_windows.go carries the matching stubs. Signatures and exported names
// are identical on both sides; everything else in this package (New, ListenPath,
// serveFailed, Stop, ServeHTTP) is platform-neutral and stays in proxy.go.

// ensurePrivateDir makes dir (and only dir: its parent must already
// exist, as New documents) mode privateDirMode and owned by this
// process. A dir owned by someone else is refused: tightening or
// widening a dir that is not ours is not this proxy's job.
func ensurePrivateDir(dir string) error {
	if err := os.MkdirAll(dir, privateDirMode); err != nil {
		return err
	}
	st, err := os.Stat(dir)
	if err != nil {
		return err
	}
	if !st.IsDir() {
		return errors.New("sockproxy: listen parent path is not a directory")
	}
	// Owner check via the same syscall.Stat_t idiom the executor uses
	// (os.FileInfo carries no uid on its own).
	owner, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return errors.New("sockproxy: listen parent dir owner not verifiable")
	}
	if owner.Uid != uint32(os.Getuid()) {
		return errors.New("sockproxy: listen parent dir is not owned by us")
	}
	m := st.Mode() & 0o777
	if m != privateDirMode {
		if err := os.Chmod(dir, privateDirMode); err != nil {
			return err
		}
	}
	return nil
}

// Start begins listening on the proxy socket. The socket is created
// inside a private 0700 directory and the listen syscall runs under a
// 0177 umask (restored immediately: the worker is multi-threaded, so a
// process-wide umask change must not leak to other goroutines), which
// means the inode the kernel creates already carries the final mode
// atomically. The chmod after the listen is kept only to pin the
// documented mode; nothing is reachable by group/world either way,
// because the parent dir is owner-only until the socket mode is set.
func (s *Server) Start() error {
	// The socket's own directory must be private before the socket
	// exists; the socket path's parent is what the consumers traverse.
	dir := filepath.Dir(s.listenPath)
	if err := ensurePrivateDir(dir); err != nil {
		return err
	}
	// The umask change is process-wide and NOT thread-safe: it is held
	// only around the listen syscall and restored right away.
	umaskPrev := syscall.Umask(0o177)
	ln, lerr := net.Listen("unix", s.listenPath)
	// Restore the process umask before anything else runs: the change
	// is held ONLY around the listen call above.
	syscall.Umask(umaskPrev)
	if lerr != nil {
		return lerr
	}
	if err := os.Chmod(s.listenPath, socketMode); err != nil {
		ln.Close()
		os.Remove(s.listenPath)
		return err
	}
	s.ln = ln
	s.http = &http.Server{Handler: s}
	// Serve runs the request loop in a goroutine: a failed loop must
	// never look like a healthy proxy, so its error is classified and
	// logged (see serveFailed).
	go func() {
		if err := s.http.Serve(ln); err != nil {
			s.serveFailed(err)
		}
	}()
	return nil
}
