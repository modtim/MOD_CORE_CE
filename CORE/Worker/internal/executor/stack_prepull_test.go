package executor

import (
	"context"
	"reflect"
	"testing"
)

func TestStackImagesDistinctSorted(t *testing.T) {
	spec := `
services:
  weights:
    image: ghcr.io/modtim/hf-weights-fetcher:hfhub2.0.0-py3.11
  ingress:
    image: ghcr.io/modtim/stack-ingress-forwarder:1.0.0
  llm:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
  other:
    image: ghcr.io/modtim/hf-weights-fetcher:hfhub2.0.0-py3.11
  built:
    build: .
`
	got := stackImages(spec)
	want := []string{
		"ghcr.io/ggml-org/llama.cpp:server-cuda",
		"ghcr.io/modtim/hf-weights-fetcher:hfhub2.0.0-py3.11",
		"ghcr.io/modtim/stack-ingress-forwarder:1.0.0",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("stackImages = %v, want %v", got, want)
	}
}

func TestStackImagesBadYAML(t *testing.T) {
	if got := stackImages("services: [not a map"); got != nil {
		t.Fatalf("want nil on bad yaml, got %v", got)
	}
}

// No credentials (fetch failed, or a non-deploy op) must be a no-op so the host's
// own docker login still governs, exactly as before this change.
func TestPrePullStackImagesNoCredsIsNoop(t *testing.T) {
	e := &Executor{}
	if err := e.prePullStackImages(context.Background(),
		"services:\n  a:\n    image: registry.example.com/x/y:1\n", nil, Callbacks{}); err != nil {
		t.Fatalf("nil creds should be a no-op, got %v", err)
	}
}
