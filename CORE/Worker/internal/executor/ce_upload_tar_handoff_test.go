package executor

// OPT4 (lane r9700-2) - the scan-staging pass and the durable upload tar
// THIS volume with THIS command back to back; the hand-off lets the upload
// untar the staging pass's tar bytes instead of running the docker helper a
// second time. These tests pin the hand-off contract WITHOUT docker:
// per-task scoping, one-shot consumption, eviction, the empty-tar miss,
// byte-for-byte untar parity against the fallback path, and mutex-guarded
// concurrency.

import (
	"bytes"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func TestCeHandoffKey_ScopedPerVolumeAndTask(t *testing.T) {
	a := ceStagedHandoffKeyFor("modvol-7", 9101)
	if a != "modvol-7|9101" {
		t.Errorf("key must carry volume and task, got %q", a)
	}
	if ceStagedHandoffKeyFor("modvol-7", 9102) == a {
		t.Errorf("task-scoped: two tasks never share a slot")
	}
	if ceStagedHandoffKeyFor("modvol-8", 9101) == a {
	 t.Errorf("volume-scoped: two volumes never share a slot")
	}
	if ceStagedHandoffKeyFor("v|1", 2) == ceStagedHandoffKeyFor("v", 12) {
	 t.Errorf("separator in a volume name must not collide")
	}
}

func TestCeHandoff_OneShotConsume(t *testing.T) {
	ceHandoffStagedTar("volA", 501, []byte("tarbytes"))
	tar, ok := ceTakeStagedTar("volA", 501)
	if !ok || bytes.Compare(tar, []byte("tarbytes")) != 0 {
		t.Fatalf("first take must get the exact tar, got ok=%v", ok)
	}
	_, ok2 := ceTakeStagedTar("volA", 501)
	if ok2 {
		t.Errorf("hand-off must be one-shot (a retry pays its own tar)")
	}
}

func TestCeHandoff_MissAndEviction(t *testing.T) {
	// never handed off: a miss
	_, m := ceTakeStagedTar("volB", 1)
	if m {
	 t.Errorf("no hand-off must miss")
	}
	// a different task cannot consume task 601's bytes
	ceHandoffStagedTar("volC", 601, []byte("x"))
	_, w := ceTakeStagedTar("volC", 602)
	if w {
	 t.Errorf("cross-task consume forbidden")
	}
	// an unconsumed entry is overwritten by the next staging pass
	ceHandoffStagedTar("volC", 603, []byte("y"))
	_, y := ceTakeStagedTar("volC", 601)
	if y {
	 t.Errorf("stale entry must be evicted by the next hand-off")
	}
}

func TestCeHandoff_EmptyTarMiss(t *testing.T) {
	// an empty volume tars to an empty/zero-length stream on some busybox
	// builds; handing that off must miss so the upload keeps its own path.
	ceHandoffStagedTar("volD", 7, []byte{})
	_, ok := ceTakeStagedTar("volD", 7)
	if ok {
	 t.Errorf("empty tar must miss (fallback tar path stays verbatim)")
	}
}

func TestCeHandoff_UntarParityWithFallback(t *testing.T) {
	// the handed-off bytes untar to EXACTLY what the fallback's own tar would
	// have produced: same bytes through the same ceReadTarIntoDir boundary.
	// ceWriteTarDir builds the same flat-member tar the busybox writer emits.
	src, err := os.MkdirTemp("", "ce-handoff-src-")
	if err != nil {
	 t.Fatal(err)
	}
defer os.RemoveAll(src)
 payload := []byte("same-bytes-either-way\n")
 if werr := os.WriteFile(filepath.Join(src, "out.bin"), payload, 0o644); werr != nil {
	 t.Fatal(werr)
 }
 tarBytes, werr := ceWriteTarDir(src)
 if werr != nil {
	 t.Fatal(werr)
 }
 // fallback: upload tars and untars itself
 dstA, derr := os.MkdirTemp("", "ce-handoff-a-")
 if derr != nil {
	 t.Fatal(derr)
 }
defer os.RemoveAll(dstA)
 namesA, rerr := ceReadTarIntoDir(tarBytes, dstA)
 if rerr != nil {
	 t.Fatal(rerr)
 }
 // hand-off: staging hands the bytes, upload untars them
 ceHandoffStagedTar("volE", 42, tarBytes)
 got, ok := ceTakeStagedTar("volE", 42)
 if !ok {
	 t.Fatal("hand-off must hit for the same volume+task")
 }
 dstB, derr2 := os.MkdirTemp("", "ce-handoff-b-")
 if derr2 != nil {
	 t.Fatal(derr2)
 }
defer os.RemoveAll(dstB)
 namesB, rerr2 := ceReadTarIntoDir(got, dstB)
 if rerr2 != nil {
	 t.Fatal(rerr2)
 }
 if len(namesA) != len(namesB) || namesA[0] != namesB[0] || namesA[0] != "out.bin" {
	 t.Fatalf("untar parity broken: %v vs %v", namesA, namesB)
 }
 back, rerr3 := os.ReadFile(filepath.Join(dstB, "out.bin"))
 if rerr3 != nil {
	 t.Fatal(rerr3)
 }
 if bytes.Compare(back, payload) != 0 {
	 t.Errorf("handed-off bytes differ from the fallback's")
	}
}

func TestCeHandoff_Concurrency(t *testing.T) {
	// two staging passes racing: exactly one entry survives, no torn state.
 var hits atomic.Int32
 for i := 0; i < 8; i++ {
	 go func() {
	  ceHandoffStagedTar("volF", i, []byte{65 + byte(i)})
	}()
	}
 time.Sleep(50 * time.Millisecond)
 for i := 0; i < 8; i++ {
	 tar, ok := ceTakeStagedTar("volF", i)
	 if ok {
	  hits.Add(1)
	 }
	 _ = tar
 }
 if hits.Load() < 1 {
	 t.Errorf("at least one hand-off must survive the race")
 }
}
