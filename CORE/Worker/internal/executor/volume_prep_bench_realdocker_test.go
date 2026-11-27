//go:build volprepbench

package executor

// Real-docker before/after micro-benchmark for UNDER1S-VOLUME-PREP. Not part of
// the normal suite (build tag). Run on a docker host that is not a production box:
//   go test -tags volprepbench -run TestRealDockerVolumePrepBench -v ./internal/executor
// Creates and removes only its own task volumes/containers (labels + names from
// createVolume / sidecarLabels); never prunes.

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/client"
	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func benchStats(label string, d []time.Duration) {
	sort.Slice(d, func(i, j int) bool { return d[i] < d[j] })
	n := len(d)
	ms := func(x time.Duration) float64 { return float64(x.Microseconds()) / 1000 }
	fmt.Printf("%-44s n=%d min=%.0f med=%.0f p90=%.0f max=%.0f (ms)\n", label, n, ms(d[0]), ms(d[n/2]), ms(d[n*9/10]), ms(d[n-1]))
}

func TestRealDockerVolumePrepBench(t *testing.T) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	e := &Executor{docker: cli, workerName: "vpbench", instanceID: "vpbench", network: "bridge",
		log: slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))}
	ctx := context.Background()
	img := "alpine:3.20"
	legacy := func(vol string) (string, error) {
		cmd := `set -e; ` + dataSeedCmd + `; ` + `echo "prep OK: $(ls -ld /data /data/input /data/output | tr '\n' ' | ')"`
		return e.runSidecarWithOutput(ctx, img, vol, cmd, transport.MinIOCredentials{})
	}
	var oldD, newD, oldAll, newAll []time.Duration
	for i := 0; i < 30; i++ {
		for _, mode := range []string{"old", "new"} {
			t0 := time.Now()
			vol, err := e.createVolume(ctx, "vpbench")
			if err != nil {
				t.Fatal(err)
			}
			t1 := time.Now()
			var out string
			if mode == "old" {
				out, err = legacy(vol)
			} else {
				out, err = e.prepareDataVolumeAPI(ctx, img, vol)
			}
			if err != nil {
				t.Fatal(err)
			}
			t2 := time.Now()
			if i == 0 {
				fmt.Printf("%s text: %q\n", mode, strings.TrimSpace(out))
			}
			e.cleanupVolume(vol)
			if mode == "old" {
				oldD = append(oldD, t2.Sub(t1))
				oldAll = append(oldAll, time.Since(t0))
			} else {
				newD = append(newD, t2.Sub(t1))
				newAll = append(newAll, time.Since(t0))
			}
		}
	}
	benchStats("OLD prepareDataVolume (shell helper)", oldD)
	benchStats("NEW prepareDataVolume (create+PUT, no start)", newD)
	benchStats("OLD create+prep+cleanup (task volume life)", oldAll)
	benchStats("NEW create+prep+cleanup (incl. removal join)", newAll)
}
