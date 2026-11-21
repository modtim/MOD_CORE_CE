package transport

import (
	"context"
	"errors"
	"strings"
	"testing"
)

const redisURLSentinel = "s3cr3t-SENTINEL"

// A malformed Redis URL must never put the password, the host:port or the
// scheme into the returned error text (go-redis ParseURL quotes the URL).
func TestParseRedisURLErrorDoesNotLeakURL(t *testing.T) {
	cases := map[string]string{
		"non-numeric port":  "redis://:" + redisURLSentinel + "@leakhost.example:99port/0",
		"control character":  "redis://:" + redisURLSentinel + "@leakhost.example:6379/\x7f0",
		"bad percent escape": "redis://:" + redisURLSentinel + "%zz@leakhost.example:6379/0",
	}
	for name, u := range cases {
		t.Run(name, func(t *testing.T) {
			opts, err := ParseRedisURL(u)
			if err == nil || opts != nil {
				t.Fatalf("expected parse failure, got opts=%v err=%v", opts, err)
			}
			if !errors.Is(err, ErrInvalidRedisURL) {
				t.Fatalf("expected ErrInvalidRedisURL, got a different error")
			}
			msg := err.Error()
			for _, bad := range []string{redisURLSentinel, "leakhost", "99port", "redis://", "%zz"} {
				if strings.Contains(msg, bad) {
					t.Fatalf("error text leaks %q", bad)
				}
			}
			_, derr := NewDirectTransport(u, "w", nil, "", "", nil)
			if derr == nil || strings.Contains(derr.Error(), redisURLSentinel) || strings.Contains(derr.Error(), "leakhost") {
				t.Fatalf("NewDirectTransport error leaks or is nil")
			}
		})
	}
}

func TestParseRedisURLValid(t *testing.T) {
	opts, err := ParseRedisURL("redis://:" + redisURLSentinel + "@localhost:6379/2")
	if err != nil {
		t.Fatalf("valid URL failed: %v", err)
	}
	if opts.Addr != "localhost:6379" || opts.DB != 2 || opts.Password != redisURLSentinel {
		t.Fatalf("valid URL parsed wrongly")
	}
}

// Review H4 (11-20 xeon proof): the embedded worker runs DirectTransport; main.go
// wires the boot_restore live stack-state check by type assertion, so BOTH
// transports must expose StackState or every restore fails closed.
func TestBothTransportsExposeStackState(t *testing.T) {
	type stackStater interface {
		StackState(ctx context.Context, taskID int) (bool, string, error)
	}
	var tps = []interface{}{(*DirectTransport)(nil), (*APITransport)(nil)}
	for _, tp := range tps {
		if _, ok := tp.(stackStater); !ok {
			t.Fatalf("%T does not implement StackState", tp)
		}
	}
}
