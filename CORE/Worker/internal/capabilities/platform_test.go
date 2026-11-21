package capabilities

import (
	"os"
	"runtime"
	"testing"
)

// A host that enumerates no accelerator, or only CPU, must never claim it can
// run accelerated containers. This is the conservative direction: a missing tag
// means "don't place GPU work here", which is correct to be wrong in.
func TestNoAcceleratorMeansNoGPURuntime(t *testing.T) {
	for _, detected := range [][]string{nil, {}, {"cpu"}} {
		p := &Profile{detectedAccelerators: detected}
		if hasContainerGPUSupport(p) {
			t.Fatalf("detectedAccelerators=%v should not report GPU runtime support", detected)
		}
		for _, tag := range derivePlatformTags(p) {
			if tag == TagGPURuntime || tag == TagBareMetalGPU {
				t.Fatalf("detectedAccelerators=%v leaked tag %q", detected, tag)
			}
		}
	}
}

// CUDA without the container runtime hook enumerates a GPU no container can
// reach — the same "looks capable, isn't" failure as WSL2.
func TestCUDAWithoutContainerToolkitIsNotGPURuntime(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("path probe is linux-specific")
	}
	p := &Profile{detectedAccelerators: []string{"cuda", "cpu"}}
	got := hasContainerGPUSupport(p)
	want := false
	for _, path := range []string{
		"/usr/bin/nvidia-container-runtime",
		"/usr/bin/nvidia-container-runtime-hook",
		"/usr/bin/nvidia-container-toolkit",
	} {
		if fileExists(path) {
			want = true
			break
		}
	}
	if got != want {
		t.Fatalf("hasContainerGPUSupport=%v, expected %v based on toolkit presence", got, want)
	}
}

// Non-NVIDIA accelerators pass devices through directly, so detection alone is
// the test — there is no runtime hook to look for.
func TestPassthroughAcceleratorsCountAsGPURuntime(t *testing.T) {
	for _, kind := range []string{"rocm", "intel", "npu"} {
		p := &Profile{detectedAccelerators: []string{kind, "cpu"}}
		if !hasContainerGPUSupport(p) {
			t.Fatalf("%s should count as GPU runtime capable", kind)
		}
	}
}

// bare-metal-gpu must never be emitted without gpu-runtime: claiming native
// access on a host that cannot run accelerated containers at all is incoherent.
func TestBareMetalImpliesGPURuntime(t *testing.T) {
	p := &Profile{detectedAccelerators: []string{"rocm"}}
	tags := derivePlatformTags(p)
	var hasRuntime, hasBare bool
	for _, tg := range tags {
		if tg == TagGPURuntime {
			hasRuntime = true
		}
		if tg == TagBareMetalGPU {
			hasBare = true
		}
	}
	if hasBare && !hasRuntime {
		t.Fatal("bare-metal-gpu emitted without gpu-runtime")
	}
}

// The WSL probe must agree with the kernel this test is actually running on —
// pinning the behaviour that motivated the whole capability guard.
func TestWSLDetectionMatchesKernel(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("WSL detection is linux-only")
	}
	inWSL := isWSL()
	p := &Profile{detectedAccelerators: []string{"rocm"}}
	var bare bool
	for _, tg := range derivePlatformTags(p) {
		if tg == TagBareMetalGPU {
			bare = true
		}
	}
	if inWSL && bare {
		t.Fatal("WSL kernel must not be tagged bare-metal-gpu — enumeration works there but kernel submit fails")
	}
	if !inWSL && !bare {
		t.Fatal("bare-metal linux with an accelerator should be tagged bare-metal-gpu")
	}
}

func TestPlatformTagsAreMergedIntoHardwareTags(t *testing.T) {
	merged := mergeHardwareTags([]string{"cuda", "cpu", TagGPURuntime}, "")
	var found bool
	for _, tg := range merged {
		if tg == TagGPURuntime {
			found = true
		}
	}
	if !found {
		t.Fatal("platform tags must survive the merge into hardware_tags")
	}
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
