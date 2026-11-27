//go:build hlfixrealdocker

package executor

// HELPER-CONTAINERS-LOCKDOWN FIX 1 + FIX 2: real-docker proof, behind a build
// tag (never part of the normal suite; the normal suite asserts the flags).
// Run on a docker host that is not a production box (cpu4 for this task):
//   docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
//     -v <clone>/CORE/Worker:/src -v <cache-vol>:/gocache -w /src \
//     -e HL_GW_IMAGE=versity/versitygw:latest \
//     -e HL_MC_IMAGE=ghcr.io/modtim/minio-client:latest \
//     golang:1.25 go test -tags hlfixrealdocker -run TestHelperLockdownRealDocker -v ./internal/executor
//
// What it proves end-to-end through the PRODUCTION paths (docker SDK +
// Executor.sidecarHostConfig / resultReadHostConfig / mcRemovePrefixCmd):
//  1. a NON-ROOT tenant volume (uid 1000, umask 077 -> files mode 0600, incl.
//     names with spaces/unicode/semicolons) uploads 5/5 under the NEW flags,
//     and every object key equals the sha256 of the source bytes;
//  2. the SAME upload with the CapAdd removed fails (rc != 0, 0 uploaded):
//     DAC_READ_SEARCH is load-bearing, not decoration;
//  3. the result-JSON read helper returns the 0600 files' bytes under the NEW
//     flags, and returns "" for them with the CapAdd removed;
//  4. the cleanup (mcRemovePrefixCmd, now `mc rm -r --force`) removes exactly
//     this task's scan-staging prefix, leaves a SIBLING task's prefix and the
//     durable objects untouched, and stays best-effort (rc 0) when the prefix
//     is already gone; the OLD command (no --force) removes NOTHING and exits
//     1 (the `|| true` hid that rc, which is how the bug stayed invisible).
// Everything it creates is named hlfix-gotest-* and removed in t.Cleanup.

import (
	"bytes"
	"context"
	"fmt"
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/volume"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

const hlRunID = "hlfix-gotest"
const hlBucket = "hlfix-bucket"

func hlName(part string) string { return "hlfix-" + hlRunID + "-" + part }

// hlSeedFiles: five tenant outputs, deliberately awkward names (the flat glob
// + quoting in buildUploadScriptBody must survive them).
func hlSeedFiles() []string {
	return []string{"plain.txt", "with space.txt", "uni-é-中文.txt",
		"semi;dollar.txt", "big.bin"}
}

// hlPayload is the exact bytes this test seeds into /data/output/<name>.
func hlPayload(name string) string {
	if name == "big.bin" {
		return strings.Repeat("x", 4096)
	}
	return "payload-" + name + "-payload"
}

// hlSha is the sha256 hex of the payload: the object key the upload script
// must produce is <prefix><hash>.
func hlSha(name string) string {
	h := sha256.Sum256([]byte(hlPayload(name)))
	return hex.EncodeToString(h[:])
}

func hlSkipUnlessDocker(t *testing.T) (*client.Client, string, string) {
	cli, err := client.NewClientWithOpts(client.FromEnv,
		client.WithAPIVersionNegotiation())
	if err != nil {
		t.Skipf("no docker env: %v", err)
	}
	if _, err := cli.Ping(context.Background()); err != nil {
		t.Skipf("no docker socket: %v", err)
	}
	gwImg := os.Getenv("HL_GW_IMAGE")
	mcImg := os.Getenv("HL_MC_IMAGE")
	if gwImg == "" || mcImg == "" {
		t.Skip("set HL_GW_IMAGE + HL_MC_IMAGE (throwaway gateway + worker mc image)")
	}
	for _, img := range []string{gwImg, mcImg, "alpine:3.20"} {
		if _, _, ierr := cli.ImageInspectWithRaw(context.Background(), img); ierr != nil {
			t.Skipf("image %s not present locally (no pulls)", img)
		}
	}
	return cli, gwImg, mcImg
}

// hlRealClient unwraps the concrete Client the Executor field needs.
func hlRealClient(c any) *client.Client {
	if x, ok := c.(*client.Client); ok {
		return x
	}
	if x, ok := c.(client.Client); ok {
		return &x
	}
	return nil
}

// hlWait drains the SDK wait pair and returns the exit code.
func hlWait(cli *client.Client, ctx context.Context, id string) int {
	statusCh, errCh := cli.ContainerWait(ctx, id, container.WaitConditionNotRunning)
	select {
	case e := <-errCh:
		_ = e
		return -1
	case s := <-statusCh:
		return int(s.StatusCode)
	}
}

// hlOut returns a container's demuxed stdout+stderr.
func hlOut(cli *client.Client, ctx context.Context, id string) string {
	reader, err := cli.ContainerLogs(ctx, id, container.LogsOptions{
		ShowStdout: true, ShowStderr: true})
	if err != nil {
		return ""
	}
	var so, se bytes.Buffer
	stdcopy.StdCopy(&so, &se, reader)
	reader.Close()
	return so.String() + se.String()
}

// hlRun runs one container with an explicit HostConfig (the same SDK sequence
// runSidecarWithOutput uses), so a MUTANT posture - the production builder's
// output with one guard stripped - is measured against the real daemon.
func hlRun(cli *client.Client, hc *container.HostConfig, img, cmd string,
	env []string, name string) (int, string) {
	ctx := context.Background()
	resp, err := cli.ContainerCreate(ctx,
		&container.Config{Image: img, Entrypoint: []string{"/bin/sh"},
			Cmd: []string{"-c", cmd}, Env: env},
		hc, &network.NetworkingConfig{}, nil, name)
	if err != nil {
		return -2, fmt.Sprintf("create: %v", err)
	}
	if err := cli.ContainerStart(ctx, resp.ID, container.StartOptions{}); err != nil {
		return -3, "start: " + err.Error()
	}
	return hlWait(cli, ctx, resp.ID), hlOut(cli, ctx, resp.ID)
}

// hlRunUser is hlRun with the container running as the tenant uid (the seed
// leg: files must land 0600 owned by uid 1000, which is the case that regressed
// under cap drop ALL).
func hlRunUser(cli *client.Client, hc *container.HostConfig, img, cmd string,
	env []string, name string) (int, string) {
	ctx := context.Background()
	resp, err := cli.ContainerCreate(ctx,
		&container.Config{Image: img, Entrypoint: []string{"/bin/sh"},
			Cmd: []string{"-c", cmd}, Env: env, User: "1000:1000"},
		hc, &network.NetworkingConfig{}, nil, name)
	if err != nil {
		return -2, fmt.Sprintf("create: %v", err)
	}
	if serr := cli.ContainerStart(ctx, resp.ID, container.StartOptions{}); serr != nil {
		return -3, fmt.Sprintf("start: %v", serr)
	}
	return hlWait(cli, ctx, resp.ID), hlOut(cli, ctx, resp.ID)
}

// hlWithoutCapAdd strips the single CapAdd from any posture (mutation probe).
func hlWithoutCapAdd(hc *container.HostConfig) *container.HostConfig {
	m := *hc
	m.CapAdd = []string{}
	return &m
}

// hlKeys lists object keys under the bucket from an `mc ls -r` listing. The
// worker's mc prints one line per object as `[ts] <size> STANDARD <key>`; keys
// this test produces are hex-based (no spaces), so the token after STANDARD is
// the key.
func hlKeys(out string) []string {
	var keys []string
	for _, line := range strings.Split(out, "\n") {
		if i := strings.Index(line, "STANDARD "); i >= 0 {
			keys = append(keys, strings.TrimSpace(line[i+len("STANDARD "):]))
		}
	}
	return keys
}

// HLSENTINEL-PART2

func TestHelperLockdownRealDocker(t *testing.T) {
	cli, gwImg, mcImg := hlSkipUnlessDocker(t)
	ctx := context.Background()
	e := &Executor{docker: cli, workerName: hlName("w"), instanceID: hlRunID,
		network: hlName("net"),
		log: slog.New(slog.NewTextHandler(os.Stderr,
			&slog.HandlerOptions{Level: slog.LevelError}))}

	// --- every resource below is hlfix-gotest-* and removed in t.Cleanup ---
	var names []string
	note := func(n string) { names = append(names, n) }
	t.Cleanup(func() {
		for _, n := range names {
			_ = cli.ContainerRemove(context.Background(), n,
				container.RemoveOptions{Force: true})
		}
		for _, v := range []string{hlName("data"), hlName("rdata"), hlName("gwdata")} {
			_ = cli.VolumeRemove(context.Background(), v, true)
		}
		_ = cli.NetworkRemove(context.Background(), hlName("net"))
	})

	if _, err := cli.NetworkCreate(ctx, hlName("net"), network.CreateOptions{}); err != nil {
		t.Fatalf("create network: %v", err)
	}
	for _, v := range []string{hlName("data"), hlName("rdata"), hlName("gwdata")} {
		if _, err := cli.VolumeCreate(ctx, volume.CreateOptions{Name: v}); err != nil {
			t.Fatalf("create volume %s: %v", v, err)
		}
	}

	// --- throwaway versitygw (posix backend, own volume, on the net) ------
	ak, sk := "hlfixak", "hlfixsk0123456789"
	gw, err := cli.ContainerCreate(ctx,
		&container.Config{Image: gwImg,
			Env: []string{"ROOT_ACCESS_KEY_ID=" + ak,
				"ROOT_SECRET_ACCESS_KEY=" + sk},
			Cmd: []string{"--access", ak, "--secret", sk, "--port", ":7070",
				"--region", "us-east-1", "posix", "/data"}},
		&container.HostConfig{Binds: []string{hlName("gwdata") + ":/data:rw"}},
		&network.NetworkingConfig{EndpointsConfig: map[string]*network.EndpointSettings{
			hlName("net"): &network.EndpointSettings{}}},
		nil, hlName("gw"))
	if err != nil {
		t.Fatalf("create gateway: %v", err)
	}
	note(hlName("gw"))
	if err := cli.ContainerStart(ctx, gw.ID, container.StartOptions{}); err != nil {
		t.Fatalf("start gateway: %v", err)
	}

	creds := &transport.Credentials{MinIO: transport.MinIOCredentials{AccessKey: ak, SecretKey: sk,
			Endpoint: "http://" + hlName("gw") + ":7070",
			MCHost: "http://" + ak + ":" + sk + "@" + hlName("gw") + ":7070"}}

	// The gateway must answer before any mc leg runs; the tenant bucket is
	// normally created by the API, so this test creates it through the same
	// production sidecar posture it will upload with.
	{	var ready bool
		for i := 0; i < 30 && !ready; i++ {		
			if err := e.runSidecar(ctx, mcImg, hlName("data"),
				"mc mb myminio/"+hlBucket+" >/dev/null 2>&1; mc ls myminio/"+hlBucket+" >/dev/null",
				creds.MinIO); err == nil {				
				ready = true
				break
			}
			time.Sleep(2*time.Second)
		}
		if !ready {				
			t.Fatalf("gateway never became ready: %s", hlOut(cli, ctx, gw.ID))
		}
	}

	// --- dirs 0777, then the FILE seed AS uid 1000 with umask 077 ---------
	{
		code, out := hlRun(cli, &container.HostConfig{
			Binds: []string{hlName("data") + ":/data:rw"}},
			"alpine:3.20",
			"mkdir -p /data/output && chmod 777 /data /data/output && echo DIRS-OK",
			[]string{}, hlName("dirs"))
		note(hlName("dirs"))
		if code != 0 {
			t.Fatalf("dirs: rc=%d %s", code, out)
		}
		var parts []string
		for _, f := range hlSeedFiles() {
			parts = append(parts, "printf '%s' '"+hlPayload(f)+"' > '/data/output/"+f+"'")
		}
		code, out = hlRunUser(cli, &container.HostConfig{
			Binds: []string{hlName("data") + ":/data:rw"}},
			"alpine:3.20", "umask 077; "+strings.Join(parts, "; ")+"; ls -l /data/output",
			[]string{}, hlName("seed"))
		note(hlName("seed"))
		if code != 0 {
			t.Fatalf("non-root seed: rc=%d %s", code, out)
		}
		// The precondition of the regression: five private (0600) files owned
		// by uid 1000, which a root container with cap drop ALL cannot read.
		if c := strings.Count(out, "-rw-------"); c != 5 {
			t.Fatalf("seed must produce five 0600 files, got %d:\n%s", c, out)
		}
	}

	// Same seed on the read-helper volume (the read helper binds it ro).
	{
		code, out := hlRun(cli, &container.HostConfig{
			Binds: []string{hlName("rdata") + ":/data:rw"}},
			"alpine:3.20",
			"mkdir -p /data/output && chmod 777 /data /data/output && echo DIRS-OK",
			[]string{}, hlName("dirs2"))
		note(hlName("dirs2"))
		if code != 0 {
			t.Fatalf("dirs2: rc=%d %s", code, out)
		}
		var parts []string
		for _, f := range resultFileReadNames {
			parts = append(parts, "printf '%s' '{\"f\":\""+f+"\"}' > '/data/output/"+f+"'")
		}
		code, out = hlRunUser(cli, &container.HostConfig{
			Binds: []string{hlName("rdata") + ":/data:rw"}},
			"alpine:3.20", "umask 077; "+strings.Join(parts, "; ")+"; ls -l /data/output",
			[]string{}, hlName("seed2"))
		note(hlName("seed2"))
		if code != 0 {
			t.Fatalf("non-root seed2: rc=%d %s", code, out)
		}
	}

	// === FIX 1 (a): the PRODUCTION sidecar posture uploads 5/5 ============
	// The exact staging script (buildUploadScript over the scan-staging prefix)
	// run through runSidecarWithOutput, i.e. sidecarHostConfig on the wire:
	// cap drop ALL + CapAdd DAC_READ_SEARCH + read-only rootfs + /root/.mc
	// tmpfs + limits, on the task network with the rw /data bind.
	task := &transport.Task{ID: 9101, Payload: transport.TaskPayload{
		MinioBucket: hlBucket,
	}}
	{
		out, err := e.runSidecarWithOutput(ctx, mcImg, hlName("data"),
			buildUploadScript(stagingUploadParamsFromTask(task)), creds.MinIO)
		if err != nil {
			t.Fatalf("FIX1 upload under the NEW flags failed: %v\n%s", err, out)
		}
		if n := strings.Count(out, "UPLOAD,"); n != 5 {
			t.Fatalf("FIX1 upload manifest lines = %d, want 5:\n%s", n, out)
		}
		// Every key must be exactly scan-staging/9101/<sha256 of the bytes>.
		var want []string
		for _, f := range hlSeedFiles() {
			want = append(want, "scan-staging/9101/"+hlSha(f))
		}
		got := hlKeys(mcLs(e, ctx, mcImg, creds, hlName("data"), ""))
		sortHL(want); sortHL(got)
		if strings.Join(want, "\n") != strings.Join(got, "\n") {
			t.Fatalf("FIX1 object keys mismatch:\n want %v\n got  %v", want, got)
		}
	}

	// === FIX 1 (b): the SAME upload with the CapAdd removed MUST FAIL =====
	// Mutation proof against the real daemon: DAC_READ_SEARCH is what makes the
	// 0600 read possible; without it the closer's failure returns (sha256sum
	// Permission denied, rc 1, nothing uploaded).
	{
		hc := hlWithoutCapAdd(e.sidecarHostConfig(hlName("data")))
		code, out := hlRun(cli, hc, mcImg,
			buildUploadScript(stagingUploadParamsFromTask(task)),
			[]string{"MC_HOST_myminio="+creds.MinIO.MCHost,
				"MINIO_ENDPOINT="+creds.MinIO.Endpoint,
				"MINIO_ACCESS="+creds.MinIO.AccessKey,
				"MINIO_SECRET="+creds.MinIO.SecretKey},
			hlName("mutant"))
		note(hlName("mutant"))
		if code == 0 {
			t.Fatalf("mutation probe: upload WITHOUT CapAdd DAC_READ_SEARCH still "+
				"succeeded (the 0600 regression would be unfixed):\n%s", out)
		}
		if !strings.Contains(out, "Permission denied") {
			t.Fatalf("mutation probe: expected a Permission denied failure, got:\n%s", out)
		}
	}

	// === FIX 1 (c): the result-JSON read helper reads the 0600 files =======
	{
		got := e.readResultJSONFiles(ctx, hlName("rdata"), "alpine:3.20",
			resultFileReadNames)
		for _, f := range resultFileReadNames {
			if got[f] != "{\"f\":\""+f+"\"}" {
				t.Fatalf("read helper under the NEW flags: %s = %q, want the 0600 file's bytes",
					f, got[f])
			}
		}
		// Mutation: same read path, CapAdd stripped -> every 0600 file reads "".
		hc := hlWithoutCapAdd(e.resultReadHostConfig(hlName("rdata")))
		code, out := hlRun(cli, hc, "alpine:3.20",
			"for f in /data/output/*; do cat \"$f\" 2>/dev/null; done; echo DONE",
			[]string{}, hlName("mutread"))
		note(hlName("mutread"))
		if code != 0 || strings.Contains(out, "\"f\":") {
			t.Fatalf("mutation probe: read helper WITHOUT CapAdd must NOT read the "+
				"0600 files (rc=%d):\n%s", code, out)
		}
	}

	// === FIX 2 (a): the OLD cleanup command removes NOTHING ==============
	// Seed a second task's prefix + a durable object so the prefix scoping is
	// observable, then run the OLD (pre-fix) command text verbatim.
	{
		seed := "echo sib > /data/seed.txt; mc cp /data/seed.txt myminio/"+hlBucket+
			"/scan-staging/9102/sib.txt >/dev/null; mc cp /data/seed.txt myminio/"+hlBucket+
			"/durable/hash1.txt >/dev/null; echo SEEDED"
		code, out := hlRun(cli, e.sidecarHostConfig(hlName("data")),
			mcImg, seed,
			[]string{"MC_HOST_myminio="+creds.MinIO.MCHost}, hlName("seedobj"))
		note(hlName("seedobj"))
		if code != 0 {
			t.Fatalf("seed sibling/durable objects: rc=%d %s", code, out)
		}
		before := hlKeys(mcLs(e, ctx, mcImg, creds, hlName("data"), ""))
		if len(before) != 7 {
			t.Fatalf("expected 5 staged + 1 sibling + 1 durable objects, got %v", before)
		}
		// The OLD text, exactly as it shipped before FIX 2.
		oldCmd := "mc rm -r myminio/"+hlBucket+"/scan-staging/9101/ || true"
		code, out = hlRun(cli, e.sidecarHostConfig(hlName("data")),
			mcImg, oldCmd,
			[]string{"MC_HOST_myminio="+creds.MinIO.MCHost}, hlName("oldrm"))
		note(hlName("oldrm"))
		if code != 0 {
			t.Fatalf("old rm must be rc 0 (the || true hides the failure): rc=%d %s", code, out)
		}
		if !strings.Contains(out, "--force") {
			t.Fatalf("old rm must print the --force refusal, got:\n%s", out)
		}
		after := hlKeys(mcLs(e, ctx, mcImg, creds, hlName("data"), ""))
		if len(after) != len(before) {
			t.Fatalf("OLD cleanup removed something (it must remove NOTHING): before=%d after=%d",
				len(before), len(after))
		}
	}

	// === FIX 2 (b): the PRODUCTION cleanup removes exactly this task's prefix
	{
		if err := e.cleanupStagedOutputs(ctx, mcImg, hlName("data"), creds, task); err != nil {
			t.Fatalf("cleanupStagedOutputs under the fix: %v", err)
		}
		after := hlKeys(mcLs(e, ctx, mcImg, creds, hlName("data"), ""))
		for _, k := range after {
			if strings.HasPrefix(k, "scan-staging/9101/") {
				t.Fatalf("staged prefix still present after the fix: %v", after)
			}
		}
		var have bool
		for _, k := range after {
			if k == "scan-staging/9102/sib.txt" || k == "durable/hash1.txt" {
				have = true
			}
		}
		if !have {
			t.Fatalf("the fix must NOT touch a sibling task's prefix or durable objects: %v", after)
		}
	}

	// === FIX 2 (c): best-effort semantics stay (empty prefix -> rc 0) =====
	{
		if err := e.cleanupStagedOutputs(ctx, mcImg, hlName("data"), creds, task); err != nil {
			t.Fatalf("second cleanup of an already-empty prefix must stay best-effort: %v", err)
		}
	}

	// === FIX 2 (d): the folded cleanup keeps the upload's exit status =====
	{
		folded := durableUploadParamsFromTask(task)
		suffix := stagingCleanupFoldSuffix(folded)
		if !strings.Contains(suffix, "mc rm -r --force myminio/"+hlBucket+"/scan-staging/9101/ || true") {
			t.Fatalf("fold must carry the fixed --force command, got:\n%s", suffix)
		}
		// Run the folded script for real: the durable objects land, the staging
		// prefix is gone, and the container's exit status is the upload's.
		out, err := e.runSidecarWithOutput(ctx, mcImg, hlName("data"),
			buildUploadScript(folded), creds.MinIO)
		if err != nil {
			t.Fatalf("folded durable upload: %v", err)
		}
		if n := strings.Count(out, "UPLOAD,"); n != 5 {
			t.Fatalf("folded upload manifest lines = %d, want 5", n)
		}
		var staged []string
		for _, k := range hlKeys(mcLs(e, ctx, mcImg, creds, hlName("data"), "")) {
			if strings.HasPrefix(k, "scan-staging/9101/") {
				staged = append(staged, k)
			}
		}
		if len(staged) != 0 {
			t.Fatalf("folded cleanup must remove the staging prefix, still there: %v", staged)
		}
	}
	t.Log("HELPER-CONTAINERS-LOCKDOWN real-docker: upload_0600=5/5, capadd mutation fails, "+
		"cleanup removes own prefix only, sibling + durable survive, best-effort rc 0")
}

// mcLs runs `mc ls -r <bucket>` through the production sidecar path and returns
// the raw listing.
func mcLs(e *Executor, ctx context.Context, mcImg string,
	creds *transport.Credentials, volName, extra string) string {
	out, err := e.runSidecarWithOutput(ctx, mcImg, volName,
		"mc ls -r myminio/"+hlBucket+" "+extra, creds.MinIO)
	if err != nil {
		return "\x00mc ls failed: "+err.Error()+"\x00"
	}
	return out
}

func sortHL(v []string) {
	for i := 1; i < len(v); i++ {
		for j := i; j > 0 && v[j-1] > v[j]; j-- {
			t := v[j-1]; v[j-1] = v[j]; v[j] = t
		}
	}
}
