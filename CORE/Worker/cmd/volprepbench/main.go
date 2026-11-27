// volprepbench: UNDER1S-VOLUME-PREP micro-benchmark. Measures every way to get a
// per-task volume with /data/input and /data/output present, on a docker host.
// Creates and removes only objects named mcw-vpb-*; never prunes.
package main

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/image"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/volume"
	"github.com/docker/docker/client"
)

const helper = "alpine:3.20"
const seedCmd = `mkdir -p /data/input /data/output; chmod 0777 /data /data/input /data/output`

var cli *client.Client
var lbl = map[string]string{"mcw-vpb": "1"}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "ERR:", err)
		os.Exit(1)
	}
}

func seedTar() *bytes.Buffer {
	var b bytes.Buffer
	tw := tar.NewWriter(&b)
	for _, n := range []string{"data/", "data/input/", "data/output/"} {
		tw.WriteHeader(&tar.Header{Name: n, Typeflag: tar.TypeDir, Mode: 0777, Uid: 0, Gid: 0, ModTime: time.Now()})
	}
	tw.Close()
	return &b
}

func vname(mode string, i int) string { return fmt.Sprintf("mcw-vpb-%s-%d-%d", mode, os.Getpid(), i) }

func mkCtr(ctx context.Context, vol string) string {
	r, err := cli.ContainerCreate(ctx, &container.Config{Image: helper, Entrypoint: []string{"/bin/sh"}, Cmd: []string{"-c", seedCmd}, Labels: lbl},
		&container.HostConfig{Binds: []string{vol + ":/data:rw"}}, &network.NetworkingConfig{}, nil, "")
	must(err)
	return r.ID
}

func rmCtr(id string) { cli.ContainerRemove(context.Background(), id, container.RemoveOptions{Force: true}) }
func rmVol(n string)  { cli.VolumeRemove(context.Background(), n, true) }

func today(ctx context.Context, vol string) {
	id := mkCtr(ctx, vol)
	defer rmCtr(id)
	must(cli.ContainerStart(ctx, id, container.StartOptions{}))
	st, er := cli.ContainerWait(ctx, id, container.WaitConditionNotRunning)
	select {
	case <-st:
	case e := <-er:
		must(e)
	}
	rd, err := cli.ContainerLogs(ctx, id, container.LogsOptions{ShowStdout: true, ShowStderr: true})
	must(err)
	rd.Close()
}

func put(ctx context.Context, vol string) {
	id := mkCtr(ctx, vol)
	defer rmCtr(id)
	must(cli.CopyToContainer(ctx, id, "/", seedTar(), container.CopyToContainerOptions{}))
}

func create(ctx context.Context, name string, opts map[string]string) {
	vo := volume.CreateOptions{Name: name, Labels: lbl}
	if opts != nil {
		vo.Driver = "local"
		vo.DriverOpts = opts
	}
	_, err := cli.VolumeCreate(ctx, vo)
	must(err)
}

func stats(label string, d []time.Duration) {
	sort.Slice(d, func(i, j int) bool { return d[i] < d[j] })
	n := len(d)
	f := func(x time.Duration) string { return fmt.Sprintf("%.0f", float64(x.Microseconds())/1000) }
	fmt.Printf("%-34s n=%d min=%s med=%s p90=%s max=%s (ms)\n", label, n, f(d[0]), f(d[n/2]), f(d[(n*9)/10]), f(d[n-1]))
}

func main() {
	var err error
	cli, err = client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	must(err)
	ctx := context.Background()
	n := 30
	if len(os.Args) > 2 {
		n, _ = strconv.Atoi(os.Args[2])
	}
	mode := os.Args[1]
	if mode == "verify" {
		for _, m := range []string{"today", "put"} {
			v := vname("ver"+m, 0)
			create(ctx, v, nil)
			if m == "today" {
				today(ctx, v)
			} else {
				put(ctx, v)
			}
			fmt.Println("==", m)
			fmt.Println(run(ctx, v, "", "ls -ldn /data /data/input /data/output; ls -la /data/input /data/output | wc -l; find /data -type f | wc -l"))
			fmt.Println("nonroot 1000:", run(ctx, v, "1000:1000", "touch /data/output/x && mkdir -p /data/output/sub && echo write-ok"))
			rmVol(v)
		}
		return
	}
	var tot, a, b, c, d []time.Duration
	for i := 0; i < n; i++ {
		v := vname(mode, i)
		switch mode {
		case "floor": // docker run --rm alpine true via API (no volume)
			t := time.Now()
			r, e := cli.ContainerCreate(ctx, &container.Config{Image: helper, Cmd: []string{"true"}, Labels: lbl}, &container.HostConfig{}, nil, nil, "")
			must(e)
			must(cli.ContainerStart(ctx, r.ID, container.StartOptions{}))
			st, _ := cli.ContainerWait(ctx, r.ID, container.WaitConditionNotRunning)
			<-st
			rmCtr(r.ID)
			tot = append(tot, time.Since(t))
			continue
		case "volonly":
			t := time.Now()
			create(ctx, v, nil)
			tot = append(tot, time.Since(t))
		case "tmpfs":
			t := time.Now()
			create(ctx, v, map[string]string{"type": "tmpfs", "device": "tmpfs", "o": "mode=0777"})
			tot = append(tot, time.Since(t))
		case "today":
			t := time.Now()
			create(ctx, v, nil)
			t1 := time.Now()
			today(ctx, v)
			a = append(a, t1.Sub(t))
			b = append(b, time.Since(t1))
			tot = append(tot, time.Since(t))
		case "put":
			t := time.Now()
			create(ctx, v, nil)
			t1 := time.Now()
			put(ctx, v)
			a = append(a, t1.Sub(t))
			b = append(b, time.Since(t1))
			tot = append(tot, time.Since(t))
		case "putonly": // PUT into an existing created container (node-container reuse case)
			create(ctx, v, nil)
			id := mkCtr(ctx, v)
			t := time.Now()
			must(cli.CopyToContainer(ctx, id, "/", seedTar(), container.CopyToContainerOptions{}))
			tot = append(tot, time.Since(t))
			rmCtr(id)
		case "split": // phases of the PUT path: ctr create / put / stat x3 / ctr remove
			create(ctx, v, nil)
			t0 := time.Now()
			id := mkCtr(ctx, v)
			t1 := time.Now()
			must(cli.CopyToContainer(ctx, id, "/", seedTar(), container.CopyToContainerOptions{}))
			t2 := time.Now()
			for _, pth := range []string{"/data", "/data/input", "/data/output"} {
				_, e := cli.ContainerStatPath(ctx, id, pth)
				must(e)
			}
			t3 := time.Now()
			rmCtr(id)
			t4 := time.Now()
			a = append(a, t1.Sub(t0))
			b = append(b, t2.Sub(t1))
			c = append(c, t3.Sub(t2))
			d = append(d, t4.Sub(t3))
			tot = append(tot, t4.Sub(t0))
		case "inspect": // cached-image check cost
			t := time.Now()
			_, _, e := cli.ImageInspectWithRaw(ctx, helper)
			must(e)
			tot = append(tot, time.Since(t))
			continue
		case "par": // volume+put in parallel with an image inspect + a second volume-independent call
			t := time.Now()
			var wg sync.WaitGroup
			wg.Add(2)
			go func() { defer wg.Done(); cli.ImageList(ctx, image.ListOptions{}) }()
			go func() { defer wg.Done(); create(ctx, v, nil); put(ctx, v) }()
			wg.Wait()
			tot = append(tot, time.Since(t))
		}
		t := time.Now()
		rmVol(v)
		_ = t
	}
	stats(mode+" total", tot)
	if mode == "split" {
		stats("  ctr create", a)
		stats("  PUT tar", b)
		stats("  3x stat", c)
		stats("  ctr remove", d)
	} else if len(a) > 0 {
		stats(mode+"  [volume create]", a)
		stats(mode+"  [prep]", b)
	}
	_ = strings.TrimSpace
}

func run(ctx context.Context, vol, user, cmd string) string {
	r, err := cli.ContainerCreate(ctx, &container.Config{Image: helper, User: user, Entrypoint: []string{"/bin/sh"}, Cmd: []string{"-c", cmd}, Labels: lbl},
		&container.HostConfig{Binds: []string{vol + ":/data:rw"}}, nil, nil, "")
	must(err)
	defer rmCtr(r.ID)
	must(cli.ContainerStart(ctx, r.ID, container.StartOptions{}))
	st, _ := cli.ContainerWait(ctx, r.ID, container.WaitConditionNotRunning)
	<-st
	rd, _ := cli.ContainerLogs(ctx, r.ID, container.LogsOptions{ShowStdout: true, ShowStderr: true})
	var b bytes.Buffer
	b.ReadFrom(rd)
	rd.Close()
	return b.String()
}
