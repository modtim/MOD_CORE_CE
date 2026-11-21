package executor

// GC.MWL.C5/C6 unit tests. Reuses the package's existing seams:
// execCommandContext (withFakeExec, stackrunner_test.go) stands in for
// `docker run`/`docker volume inspect` without a real daemon, redirected to
// real `tar`/`gzip` against a temp dir so the tar stream itself is exercised
// end to end. checkpointHTTPDo (this file) stands in for the network leg.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// withFakeHTTP swaps checkpointHTTPDo for the duration of the test.
func withFakeHTTP(fn func(req *http.Request) (*http.Response, error)) func() {
	orig := checkpointHTTPDo
	checkpointHTTPDo = fn
	return func() { checkpointHTTPDo = orig }
}

// withFakeEnsureVolume swaps ensureCheckpointVolumeFn for the duration of the test.
func withFakeEnsureVolume(fn func(e *Executor, ctx context.Context, name string) error) func() {
	orig := ensureCheckpointVolumeFn
	ensureCheckpointVolumeFn = fn
	return func() { ensureCheckpointVolumeFn = orig }
}

func nopResponse(status int, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Body:       io.NopCloser(strings.NewReader(body)),
		Header:     make(http.Header),
	}
}

// writeFixture populates dir with a couple of files a real tar can pick up.
func writeFixture(t *testing.T, dir string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "progress.json"), []byte(`{"step":42}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "sub", "weights.bin"), []byte("binary-ish"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// gzipTarOf builds a real gzip(tar(dir)) byte stream — the shape the API's
// presigned GET is expected to return for a hydrate.
func gzipTarOf(t *testing.T, dir string) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	err := filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		rel, _ := filepath.Rel(dir, path)
		hdr, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		hdr.Name = rel
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		_, err = tw.Write(data)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// dockerCmdKind classifies a faked `docker ...` invocation by its argv so
// tests can redirect each shape to a real local command against a temp dir.
func dockerCmdKind(arg []string) string {
	joined := strings.Join(arg, " ")
	switch {
	case len(arg) >= 2 && arg[0] == "volume" && arg[1] == "inspect":
		return "volume_inspect"
	case strings.Contains(joined, "czf"):
		return "tar_czf"
	case strings.Contains(joined, "xzf"):
		return "tar_xzf"
	case strings.Contains(joined, "find /data -mindepth 1"):
		return "find"
	default:
		return "unknown"
	}
}

// ---------------------------------------------------------------------
// ExportCheckpointVolume
// ---------------------------------------------------------------------

func TestExportCheckpointVolumeTarsFixtureAndPUTs(t *testing.T) {
	srcDir := t.TempDir()
	writeFixture(t, srcDir)

	var rmSeen []string // args of every invocation, to confirm --rm on the tar container
	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		rmSeen = append(rmSeen, strings.Join(arg, " "))
		switch dockerCmdKind(arg) {
		case "volume_inspect":
			return exec.CommandContext(ctx, "true")
		case "tar_czf":
			if !argContainsCkpt(arg, "--rm") {
				t.Error("tar helper container missing --rm")
			}
			return exec.CommandContext(ctx, "tar", "czf", "-", "-C", srcDir, ".")
		default:
			t.Fatalf("unexpected docker invocation: %v", arg)
			return exec.CommandContext(ctx, "false")
		}
	})
	defer restoreExec()

	var putURL, putMethod, contentType string
	var putBody []byte
	restoreHTTP := withFakeHTTP(func(req *http.Request) (*http.Response, error) {
		putURL = req.URL.String()
		putMethod = req.Method
		contentType = req.Header.Get("Content-Type")
		body, err := io.ReadAll(req.Body)
		if err != nil {
			t.Fatal(err)
		}
		putBody = body
		return nopResponse(200, ""), nil
	})
	defer restoreHTTP()

	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	uploadURL := "https://storage.example.com/ckpt/abc.tar.gz?X-Amz-Signature=supersecret"
	if err := e.ExportCheckpointVolume(context.Background(), "modcore-ckpt-noderun-1", uploadURL); err != nil {
		t.Fatalf("ExportCheckpointVolume failed: %v", err)
	}

	if putMethod != http.MethodPut {
		t.Fatalf("want PUT, got %s", putMethod)
	}
	if putURL != uploadURL {
		t.Fatalf("upload URL mismatch: got %s want %s", putURL, uploadURL)
	}
	if contentType != "application/gzip" {
		t.Fatalf("unexpected content type %q", contentType)
	}
	if len(rmSeen) == 0 {
		t.Fatal("no docker invocations observed")
	}

	// The PUT body must be a real, decodable gzip(tar) containing the fixture.
	gz, err := gzip.NewReader(bytes.NewReader(putBody))
	if err != nil {
		t.Fatalf("PUT body is not valid gzip: %v", err)
	}
	tr := tar.NewReader(gz)
	got := map[string]string{}
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("tar read error: %v", err)
		}
		if hdr.Typeflag != tar.TypeReg {
			continue
		}
		data, _ := io.ReadAll(tr)
		got[strings.TrimPrefix(hdr.Name, "./")] = string(data)
	}
	if got["progress.json"] != `{"step":42}` {
		t.Errorf("progress.json missing/mismatched in exported tar: %+v", got)
	}
	if got["sub/weights.bin"] != "binary-ish" {
		t.Errorf("sub/weights.bin missing/mismatched in exported tar: %+v", got)
	}
}

func TestExportCheckpointVolumeNonexistentVolumeErrors(t *testing.T) {
	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		if dockerCmdKind(arg) != "volume_inspect" {
			t.Fatalf("expected only a volume inspect call, got %v", arg)
		}
		return exec.CommandContext(ctx, "sh", "-c", `echo "Error: No such volume: ghost" >&2; exit 1`)
	})
	defer restoreExec()

	httpCalled := false
	restoreHTTP := withFakeHTTP(func(req *http.Request) (*http.Response, error) {
		httpCalled = true
		return nopResponse(200, ""), nil
	})
	defer restoreHTTP()

	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	err := e.ExportCheckpointVolume(context.Background(), "ghost", "https://storage.example.com/x?sig=s")
	if err == nil {
		t.Fatal("expected an error exporting a nonexistent volume")
	}
	if httpCalled {
		t.Fatal("must not attempt a PUT when the volume does not exist")
	}
}

func TestExportCheckpointVolumeHelperReapedOnPUTFailure(t *testing.T) {
	srcDir := t.TempDir()
	writeFixture(t, srcDir)
	doneFile := filepath.Join(t.TempDir(), "tar-finished")

	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		switch dockerCmdKind(arg) {
		case "volume_inspect":
			return exec.CommandContext(ctx, "true")
		case "tar_czf":
			// Proves cmd.Wait() ran to completion (and so --rm's container
			// removal fired) even though the PUT below fails.
			script := fmt.Sprintf("tar czf - -C %s . ; touch %s", srcDir, doneFile)
			return exec.CommandContext(ctx, "sh", "-c", script)
		default:
			t.Fatalf("unexpected docker invocation: %v", arg)
			return exec.CommandContext(ctx, "false")
		}
	})
	defer restoreExec()

	restoreHTTP := withFakeHTTP(func(req *http.Request) (*http.Response, error) {
		_, _ = io.Copy(io.Discard, req.Body) // drain, like a real client would after a failed dial attempt
		return nil, fmt.Errorf("dial storage.example.com: connection refused")
	})
	defer restoreHTTP()

	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	err := e.ExportCheckpointVolume(context.Background(), "modcore-ckpt-noderun-2", "https://storage.example.com/x?sig=secretvalue")
	if err == nil {
		t.Fatal("expected an error when the PUT fails")
	}
	if strings.Contains(err.Error(), "secretvalue") {
		t.Fatalf("presigned query string leaked into error: %q", err.Error())
	}
	if _, statErr := os.Stat(doneFile); statErr != nil {
		t.Fatal("tar helper process was not reaped (cmd.Wait never completed) after the PUT failed")
	}
}

// ---------------------------------------------------------------------
// HydrateCheckpointVolume
// ---------------------------------------------------------------------

func TestHydrateCheckpointVolumeEmptyVolumeIsPopulated(t *testing.T) {
	restoreEnsure := withFakeEnsureVolume(func(e *Executor, ctx context.Context, name string) error {
		return nil
	})
	defer restoreEnsure()

	srcDir := t.TempDir()
	writeFixture(t, srcDir)
	payload := gzipTarOf(t, srcDir)

	extractDir := t.TempDir()
	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		switch dockerCmdKind(arg) {
		case "find":
			// Empty volume: nothing found.
			return exec.CommandContext(ctx, "true")
		case "tar_xzf":
			if !argContainsCkpt(arg, "--rm") {
				t.Error("untar helper container missing --rm")
			}
			return exec.CommandContext(ctx, "tar", "xzf", "-", "-C", extractDir)
		default:
			t.Fatalf("unexpected docker invocation: %v", arg)
			return exec.CommandContext(ctx, "false")
		}
	})
	defer restoreExec()

	restoreHTTP := withFakeHTTP(func(req *http.Request) (*http.Response, error) {
		if req.Method != http.MethodGet {
			t.Fatalf("want GET, got %s", req.Method)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(bytes.NewReader(payload)), Header: make(http.Header)}, nil
	})
	defer restoreHTTP()

	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	if err := e.HydrateCheckpointVolume(context.Background(), "modcore-ckpt-noderun-3", "https://storage.example.com/ckpt.tar.gz?sig=s"); err != nil {
		t.Fatalf("HydrateCheckpointVolume failed: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(extractDir, "progress.json"))
	if err != nil {
		t.Fatalf("fixture file was not extracted into the volume: %v", err)
	}
	if string(got) != `{"step":42}` {
		t.Fatalf("unexpected extracted content: %q", got)
	}
}

func TestHydrateCheckpointVolumeNonEmptyVolumeIsNoop(t *testing.T) {
	restoreEnsure := withFakeEnsureVolume(func(e *Executor, ctx context.Context, name string) error {
		return nil
	})
	defer restoreEnsure()

	httpCalled := false
	untarCalled := false
	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		switch dockerCmdKind(arg) {
		case "find":
			// Non-empty volume: find reports a hit.
			return exec.CommandContext(ctx, "echo", "/data/progress.json")
		case "tar_xzf":
			untarCalled = true
			return exec.CommandContext(ctx, "true")
		default:
			t.Fatalf("unexpected docker invocation: %v", arg)
			return exec.CommandContext(ctx, "false")
		}
	})
	defer restoreExec()

	restoreHTTP := withFakeHTTP(func(req *http.Request) (*http.Response, error) {
		httpCalled = true
		return nopResponse(200, ""), nil
	})
	defer restoreHTTP()

	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	if err := e.HydrateCheckpointVolume(context.Background(), "modcore-ckpt-noderun-4", "https://storage.example.com/ckpt.tar.gz?sig=s"); err != nil {
		t.Fatalf("expected a no-op (nil error), got: %v", err)
	}
	if httpCalled {
		t.Fatal("must not download when the volume already has content — would clobber a live retry's progress")
	}
	if untarCalled {
		t.Fatal("must not untar when the volume already has content")
	}
}

func TestHydrateCheckpointVolumeDownloadFailureIsNonFatal(t *testing.T) {
	restoreEnsure := withFakeEnsureVolume(func(e *Executor, ctx context.Context, name string) error {
		return nil
	})
	defer restoreEnsure()

	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		switch dockerCmdKind(arg) {
		case "find":
			return exec.CommandContext(ctx, "true") // empty
		default:
			t.Fatalf("unexpected docker invocation (untar should never run): %v", arg)
			return exec.CommandContext(ctx, "false")
		}
	})
	defer restoreExec()

	restoreHTTP := withFakeHTTP(func(req *http.Request) (*http.Response, error) {
		return nil, fmt.Errorf("dial storage.example.com: connection refused")
	})
	defer restoreHTTP()

	var logBuf bytes.Buffer
	e := &Executor{log: slog.New(slog.NewTextHandler(&logBuf, nil))}

	const secretQuery = "X-Amz-Signature=do-not-leak-me"
	downloadURL := "https://storage.example.com/ckpt.tar.gz?" + secretQuery

	herr := e.HydrateCheckpointVolume(context.Background(), "modcore-ckpt-noderun-5", downloadURL)
	if herr == nil {
		t.Fatal("expected a download failure to be returned")
	}
	if strings.Contains(herr.Error(), secretQuery) {
		t.Fatalf("presigned query string leaked into the returned error: %q", herr.Error())
	}

	// Mirror the executor.go wiring: hydrate failure is logged and swallowed,
	// the task proceeds cold rather than failing.
	e.log.Warn("checkpoint hydrate failed; starting cold", "volume", "modcore-ckpt-noderun-5", "err", herr)
	if strings.Contains(logBuf.String(), secretQuery) {
		t.Fatalf("presigned query string leaked into log output: %s", logBuf.String())
	}
}

func TestHydrateCheckpointVolumeEnsureFailureIsNonFatal(t *testing.T) {
	restoreEnsure := withFakeEnsureVolume(func(e *Executor, ctx context.Context, name string) error {
		return fmt.Errorf("no space left on device")
	})
	defer restoreEnsure()

	execCalled := false
	restoreExec := withFakeExec(func(ctx context.Context, name string, arg ...string) *exec.Cmd {
		execCalled = true
		return exec.CommandContext(ctx, "false")
	})
	defer restoreExec()

	e := &Executor{log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	err := e.HydrateCheckpointVolume(context.Background(), "modcore-ckpt-noderun-6", "https://storage.example.com/x?sig=s")
	if err == nil {
		t.Fatal("expected ensure-volume failure to surface as an error")
	}
	if execCalled {
		t.Fatal("must not attempt find/download when the volume could not be ensured")
	}
}

// ---------------------------------------------------------------------
// redactCheckpointURL
// ---------------------------------------------------------------------

func TestRedactCheckpointURLStripsQueryString(t *testing.T) {
	cases := map[string]string{
		"https://s3.example.com/bucket/key?X-Amz-Signature=abc123&X-Amz-Expires=900": "https://s3.example.com/bucket/key?REDACTED",
		"https://s3.example.com/bucket/key":                                          "https://s3.example.com/bucket/key",
	}
	for in, want := range cases {
		if got := redactCheckpointURL(in); got != want {
			t.Errorf("redactCheckpointURL(%q) = %q, want %q", in, got, want)
		}
	}
}

func argContainsCkpt(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}
