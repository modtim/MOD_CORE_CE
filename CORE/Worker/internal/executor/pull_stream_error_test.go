package executor

import (
	"strings"
	"testing"
)

// pullStreamError(imageName, reader) decodes a docker ImagePull JSON message
// stream and returns the first error it carries as "pull image <ref>: <msg>".

const progressStream = `{"status":"Pulling from library/alpine","id":"3.19"}
{"status":"Pulling fs layer","progressDetail":{},"id":"abc123"}
{"status":"Download complete","progressDetail":{},"id":"abc123"}
{"status":"Status: Downloaded newer image for alpine:3.19"}
`

// TestPullStreamError_NoError confirms a clean progress stream passes through.
func TestPullStreamError_NoError(t *testing.T) {
	if err := pullStreamError("alpine:3.19", strings.NewReader(progressStream)); err != nil {
		t.Fatalf("expected nil, got: %v", err)
	}
}

// TestPullStreamError_ErrSpelling covers the {"Err":"..."} pull-stream shape.
func TestPullStreamError_ErrSpelling(t *testing.T) {
	input := `{"status":"Pulling fs layer","id":"abc123"}
{"Err":"pull access denied, registry returned 401"}
`
	err := pullStreamError("reg.example/app:1", strings.NewReader(input))
	if err == nil {
		t.Fatal("expected an error for the Err line")
	}
	want := "pull image reg.example/app:1: pull access denied, registry returned 401"
	if err.Error() != want {
		t.Fatalf("error = %q, want %q", err.Error(), want)
	}
}

// TestPullStreamError_LowercaseErrorSpelling covers the {"error":"..."} shape
// (go-client checkResponseForError).
func TestPullStreamError_LowercaseErrorSpelling(t *testing.T) {
	err := pullStreamError("app:2", strings.NewReader(`{"error":"manifest unknown"}`+"\n"))
	if err == nil || !strings.Contains(err.Error(), "manifest unknown") {
		t.Fatalf("expected error containing 'manifest unknown', got: %v", err)
	}
}

// TestPullStreamError_ErrorDetailSpelling covers the newer
// {"ErrorDetail":{"Message":"..."}} shape and its precedence over the others.
func TestPullStreamError_ErrorDetailSpelling(t *testing.T) {
	input := `{"ErrorDetail":{"Message":"toomanyrequests: rate limit"},"Err":"older text"}` + "\n"
	err := pullStreamError("app:3", strings.NewReader(input))
	if err == nil {
		t.Fatal("expected an error for the ErrorDetail line")
	}
	if !strings.Contains(err.Error(), "toomanyrequests: rate limit") {
		t.Fatalf("expected the ErrorDetail message, got: %v", err)
	}
	if strings.Contains(err.Error(), "older text") {
		t.Fatalf("ErrorDetail must win over Err, got: %v", err)
	}
}

// TestPullStreamError_MixedOutput finds the error in the middle of a long stream.
func TestPullStreamError_MixedOutput(t *testing.T) {
	input := progressStream + `{"Err":"manifest not found"}` + "\n" + progressStream
	err := pullStreamError("app:4", strings.NewReader(input))
	if err == nil {
		t.Fatal("expected an error in the middle of the stream")
	}
	if !strings.Contains(err.Error(), "manifest not found") {
		t.Fatalf("expected 'manifest not found', got: %v", err)
	}
}

// TestPullStreamError_MultipleErrorLines confirms only the first error is returned.
func TestPullStreamError_MultipleErrorLines(t *testing.T) {
	input := `{"Err":"manifest not found"}` + "\n" + `{"Err":"retry limit reached"}` + "\n"
	err := pullStreamError("app:5", strings.NewReader(input))
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "manifest not found") {
		t.Fatalf("expected 'manifest not found', got: %v", err)
	}
	if strings.Contains(err.Error(), "retry limit reached") {
		t.Fatal("should only contain the first error")
	}
}

// TestPullStreamError_GarbledTailAfterErrorStillReturnsError makes sure an
// error already seen is not lost to a garbled remainder.
func TestPullStreamError_GarbledTailAfterErrorStillReturnsError(t *testing.T) {
	input := `{"Err":"denied"}` + "\n" + `not json at all`
	if err := pullStreamError("app:6", strings.NewReader(input)); err == nil {
		t.Fatal("expected the first error even with a garbled tail")
	}
}

// TestPullStreamError_EmptyInput ensures an empty stream does not panic and is clean.
func TestPullStreamError_EmptyInput(t *testing.T) {
	if err := pullStreamError("app:7", strings.NewReader("")); err != nil {
		t.Fatalf("expected nil for empty input, got: %v", err)
	}
}
