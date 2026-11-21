package sockproxy

// sz-mac-worker.4: docker daemon socket discovery. Candidate order, the
// "exists AND is a socket" rule, and the once-per-process log line are
// driven with temp dirs and real fake sockets (net.Listen), HOME
// overridden — no real Docker needed.
import (
	"context"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

type discLog struct {
	mu    sync.Mutex
	lines []string
}

func (c *discLog) Enabled(_ context.Context, _ slog.Level) bool { return true }

func (c *discLog) Handle(_ context.Context, r slog.Record) error {
	var b strings.Builder
	b.WriteString(r.Message)
	r.Attrs(func(a slog.Attr) bool {
		b.WriteByte(' ')
		b.WriteString(a.Key)
		b.WriteByte('=')
		b.WriteString(a.Value.String())
	 return true
	})
	c.mu.Lock()
	c.lines = append(c.lines, b.String())
	c.mu.Unlock()
	return nil
}

func (c *discLog) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *discLog) WithGroup(string) slog.Handler      { return c }

func (c *discLog) Count(sub string) int {
	c.mu.Lock()
 defer c.mu.Unlock()
	var n int
 for _, l := range c.lines {
  if strings.Contains(l, sub) {
   n += 1
  }
 }
 return n
}

// fakeSocket makes a real unix socket at path (parent must exist).
func fakeSocket(t *testing.T, path string) {
	ln, err := net.Listen("unix", path)
 if err != nil {
  t.Errorf("fakeSocket %s: %v", path, err)
 }
 _ = ln
}

// fakeFile makes a plain file: must never be picked.
func fakeFile(t *testing.T, path string) {
 f, err := os.Create(path)
 if err != nil {
  t.Errorf("fakeFile %s: %v", path, err)
  return
 }
 f.Close()
}

// mkTree makes a directory tree.
func mkTree(t *testing.T, path string) {
 if err := os.MkdirAll(path, 0o755); err != nil {
  t.Errorf("mkTree %s: %v", path, err)
 }
}

// setHome points HOME at dir ("" unsets it), restoring on cleanup.
func setHome(t *testing.T, dir string) {
 orig := os.Getenv("HOME")
 if dir == "" {
  os.Unsetenv("HOME")
 } else {
  os.Setenv("HOME", dir)
 }
 t.Cleanup(func() {
  if orig == "" {
   os.Unsetenv("HOME")
  } else {
   os.Setenv("HOME", orig)
  }
 })
}

func TestCandidateOrder(t *testing.T) {
 var got []string
 got = dockerSocketCandidates("", "/home/u")
 if len(got) != 4 {
  t.Errorf("unset DOCKER_HOST: %d candidates, want 5", len(got))
 }
 if got[0] != DockerSocketDefault {
  t.Errorf("linux default must be first when DOCKER_HOST unset, got %s", got[0])
 }
 if got[1] != "/home/u/.docker/run/docker.sock" ||
  got[2] != "/home/u/.orbstack/run/docker.sock" ||
  got[3] != "/home/u/.colima/default/docker.sock" {
  t.Errorf("home candidates out of order: %v", got)
 }
 got = dockerSocketCandidates("unix:///tmp/x.sock", "/home/u")
 if len(got) != 5 || got[0] != "/tmp/x.sock" {
  t.Errorf("DOCKER_HOST unix:// must come first, got %v", got)
 }
 got = dockerSocketCandidates("tcp://localhost:2375", "/home/u")
 if len(got) != 4 || got[0] != DockerSocketDefault {
  t.Errorf("tcp:// DOCKER_HOST must be ignored, got %v", got)
 }
 got = dockerSocketCandidates("unix://", "/home/u")
 if len(got) != 4 {
  t.Errorf("empty unix:// target must be ignored, got %v", got)
 }
 got = dockerSocketCandidates("", "")
 if len(got) != 1 {
  t.Errorf("empty HOME: %d candidates, want 1", len(got))
 }
}

func TestIsSocket(t *testing.T) {
 dir := t.TempDir()
 sock := filepath.Join(dir, "s.sock")
 plain := filepath.Join(dir, "p.sock")
 fakeSocket(t, sock)
 fakeFile(t, plain)
 if !isSocket(sock) {
  t.Errorf("real socket rejected")
 }
 if isSocket(plain) {
  t.Errorf("plain file accepted as socket")
 }
 if isSocket(filepath.Join(dir, "missing")) {
  t.Errorf("missing path accepted")
 }
}

func TestResolveSkipsPlainFile(t *testing.T) {
 home := t.TempDir()
 setHome(t, home)
 mkTree(t, filepath.Join(home, ".docker", "run"))
 mkTree(t, filepath.Join(home, ".orbstack", "run"))
 fakeFile(t, filepath.Join(home, ".docker", "run", "docker.sock"))
 orb := filepath.Join(home, ".orbstack", "run", "docker.sock")
 fakeSocket(t, orb)
 if got := resolveDockerSocket("", home); got != orb {
  t.Errorf("resolve picked %s, want %s", got, orb)
 }
}

func TestResolveMacOSOnly(t *testing.T) {
 if isSocket(DockerSocketDefault) {
  return // host has the Linux default: covered by TestResolveLinuxDefault
 }
 home := t.TempDir()
 setHome(t, home)
 mkTree(t, filepath.Join(home, ".docker", "run"))
 want := filepath.Join(home, ".docker", "run", "docker.sock")
 fakeSocket(t, want)
 if got := resolveDockerSocket("", home); got != want {
  t.Errorf("macOS-only resolve picked %s, want %s", got, want)
 }
}

func TestResolveLinuxDefault(t *testing.T) {
 if !isSocket(DockerSocketDefault) {
  t.Skip("host has no /var/run/docker.sock")
 }
 home := t.TempDir()
 setHome(t, home)
 mkTree(t, filepath.Join(home, ".docker", "run"))
 fakeSocket(t, filepath.Join(home, ".docker", "run", "docker.sock"))
 if got := resolveDockerSocket("", home); got != DockerSocketDefault {
  t.Errorf("linux default must win, got %s", got)
 }
}

func TestResolveNothing(t *testing.T) {
 if isSocket(DockerSocketDefault) {
  t.Skip("host has the default socket")
 }
 home := t.TempDir()
 setHome(t, home)
 if got := resolveDockerSocket("", home); got != "" {
  t.Errorf("nothing present, got %s", got)
 }
}

func TestResolveDockerHostWins(t *testing.T) {
 home := t.TempDir()
 setHome(t, home)
 mkTree(t, filepath.Join(home, ".docker", "run"))
 fakeSocket(t, filepath.Join(home, ".docker", "run", "docker.sock"))
 p := filepath.Join(home, "custom-sock")
 fakeSocket(t, p)
 os.Setenv("DOCKER_HOST", "unix://" + p)
 t.Cleanup(func() { os.Unsetenv("DOCKER_HOST") })
 if got := ResolveDockerSocket(); got != p {
  t.Errorf("DOCKER_HOST must win, got %s", got)
 }
}

func TestDockerSocketLogsOnce(t *testing.T) {
 l := &discLog{}
 home := t.TempDir()
 setHome(t, home)
 p := filepath.Join(home, "once-sock")
 fakeSocket(t, p)
 os.Setenv("DOCKER_HOST", "unix://" + p)
 t.Cleanup(func() { os.Unsetenv("DOCKER_HOST") })
 seen := DockerSocket(slog.New(l))
 again := DockerSocket(slog.New(l))
 if seen != p || again != p {
  t.Errorf("memoized accessor returned %s/%s, want %s/%s", seen, again, p, p)
 }
 if n := l.Count("docker daemon socket resolved"); n != 1 {
  t.Errorf("resolution logged %d times, want 1", n)
 }
}
