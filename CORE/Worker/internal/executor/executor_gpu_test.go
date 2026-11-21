package executor

import (
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestResolveProcessingImage_AllVariantsPresent(t *testing.T) {
	variants := map[string]string{
		"cuda":  "img:cuda",
		"rocm":  "img:rocm",
		"intel": "img:intel",
		"cpu":   "img:cpu",
	}

	tests := []struct {
		tags []string
		want string
	}{
		{[]string{"cuda"}, "img:cuda"},
		{[]string{"rocm"}, "img:rocm"},
		{[]string{"intel"}, "img:intel"},
		{[]string{"cpu"}, "img:cpu"},
	}

	for _, tt := range tests {
		e := testExecutor(tt.tags, "ghcr.io")
		payload := transport.TaskPayload{
			ProcessingImage: "default:latest",
			GPUVariants:     variants,
		}
		got := e.resolveProcessingImage(payload, "ghcr.io")
		if got != tt.want {
			t.Errorf("tags=%v: got %q, want %q", tt.tags, got, tt.want)
		}
	}
}

func TestResolveProcessingImage_FallbackChainOrder(t *testing.T) {
	e := testExecutor([]string{"rocm"}, "ghcr.io")

	p1 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants:     map[string]string{"rocm": "exact:match", "cpu": "cpu:fallback"},
	}
	if got := e.resolveProcessingImage(p1, "ghcr.io"); got != "exact:match" {
		t.Errorf("Step 1 (exact): got %q", got)
	}

	p2 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants:     map[string]string{"cuda": "cuda:only", "cpu": "cpu:fallback"},
	}
	if got := e.resolveProcessingImage(p2, "ghcr.io"); got != "cpu:fallback" {
		t.Errorf("Step 2 (CPU fallback): got %q", got)
	}

	p3 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants:     map[string]string{"cuda": "cuda:only"},
	}
	if got := e.resolveProcessingImage(p3, "ghcr.io"); got != "default:latest" {
		t.Errorf("Step 3 (default): got %q", got)
	}
}

func TestResolveProcessingImage_MultiTagPriority(t *testing.T) {
	e := testExecutor([]string{"tensorrt", "cuda"}, "ghcr.io")

	p1 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants: map[string]string{
			"tensorrt": "img:tensorrt",
			"cuda":     "img:cuda",
			"cpu":      "img:cpu",
		},
	}
	if got := e.resolveProcessingImage(p1, "ghcr.io"); got != "img:tensorrt" {
		t.Errorf("multi-tag should pick tensorrt first, got %q", got)
	}

	p2 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants: map[string]string{
			"cuda": "img:cuda",
			"cpu":  "img:cpu",
		},
	}
	if got := e.resolveProcessingImage(p2, "ghcr.io"); got != "img:cuda" {
		t.Errorf("multi-tag should fall to cuda when tensorrt absent, got %q", got)
	}
}

// gpu-passthrough-embedded-worker: the selected accelerator backend drives GPU
// device passthrough. A CUDA node routed to this worker arrives with
// gpu_required=false (only ever hydrated from workflow node_data), so the device
// request MUST be derived from the gpu_variant we picked, or the task runs on CPU.
func TestSelectedGPUBackend(t *testing.T) {
	tests := []struct {
		name     string
		tags     []string
		variants map[string]string
		want     string
	}{
		{
			name:     "cuda variant on cuda worker",
			tags:     []string{"cuda", "cpu"},
			variants: map[string]string{"cuda": "img:cuda"},
			want:     "cuda",
		},
		{
			name:     "rocm variant on rocm worker",
			tags:     []string{"rocm", "cpu"},
			variants: map[string]string{"rocm": "img:rocm", "cpu": "img:cpu"},
			want:     "rocm",
		},
		{
			name:     "multi-variant picks highest-priority tag",
			tags:     []string{"tensorrt", "cuda", "cpu"},
			variants: map[string]string{"cuda": "img:cuda", "cpu": "img:cpu"},
			want:     "cuda",
		},
		{
			name:     "cpu-only match is not an accelerator",
			tags:     []string{"cpu"},
			variants: map[string]string{"cuda": "img:cuda", "cpu": "img:cpu"},
			want:     "",
		},
		{
			name:     "no variants means no backend",
			tags:     []string{"cuda"},
			variants: nil,
			want:     "",
		},
		{
			name:     "worker lacks the node's accelerator",
			tags:     []string{"rocm", "cpu"},
			variants: map[string]string{"cuda": "img:cuda"},
			want:     "",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			e := testExecutor(tt.tags, "ghcr.io")
			p := transport.TaskPayload{GPUVariants: tt.variants}
			if got := e.selectedGPUBackend(p); got != tt.want {
				t.Errorf("selectedGPUBackend()=%q, want %q", got, tt.want)
			}
		})
	}
}

func TestResolveProcessingImage_UnknownTagFallsBack(t *testing.T) {
	e := testExecutor([]string{"groq"}, "ghcr.io")

	p1 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants: map[string]string{
			"cuda": "img:cuda",
			"cpu":  "img:cpu",
		},
	}
	if got := e.resolveProcessingImage(p1, "ghcr.io"); got != "img:cpu" {
		t.Errorf("unknown tag should fall back to cpu variant, got %q", got)
	}

	p2 := transport.TaskPayload{
		ProcessingImage: "default:latest",
		GPUVariants:     map[string]string{"cuda": "img:cuda"},
	}
	if got := e.resolveProcessingImage(p2, "ghcr.io"); got != "default:latest" {
		t.Errorf("unknown tag with no cpu should use default, got %q", got)
	}
}
