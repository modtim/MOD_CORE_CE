package tunables

import (
	"encoding/json"
	"math"
	"testing"
)

func TestBool_APIValueWins(t *testing.T) {
	t.Setenv("TEST_BOOL_ENV", "false")
	s := New()
	s.Apply(map[string]interface{}{"worker.prefer_local_images": true})
	if !s.Bool("worker.prefer_local_images", "TEST_BOOL_ENV", false) {
		t.Error("API value (true) should win over env (false)")
	}
}

func TestBool_FallsBackToEnvWhenUnset(t *testing.T) {
	t.Setenv("TEST_BOOL_ENV", "1")
	s := New() // no Apply — nothing set from the server
	if !s.Bool("worker.prefer_local_images", "TEST_BOOL_ENV", false) {
		t.Error("should fall back to env var when no API value is set")
	}
}

func TestBool_FallsBackToDefaultWhenNeitherSet(t *testing.T) {
	s := New()
	if s.Bool("worker.prefer_local_images", "TEST_BOOL_ENV_UNSET_XYZ", false) {
		t.Error("should return default when neither API nor env is set")
	}
	if !s.Bool("worker.prefer_local_images", "TEST_BOOL_ENV_UNSET_XYZ", true) {
		t.Error("default=true should be returned when neither API nor env is set")
	}
}

func TestBool_APIStringValueIsParsed(t *testing.T) {
	s := New()
	// A server that encodes bools as strings should still parse.
	s.Apply(map[string]interface{}{"worker.prefer_local_images": "true"})
	if !s.Bool("worker.prefer_local_images", "", false) {
		t.Error("string \"true\" from the API should parse as true")
	}
}

func TestBool_UnknownKeyIsIgnored(t *testing.T) {
	s := New()
	s.Apply(map[string]interface{}{"some.future.key": true})
	if s.Bool("worker.prefer_local_images", "", true) != true {
		t.Error("an unrelated/unknown key must not affect a different key's resolution")
	}
}

func TestString_APIValueWins(t *testing.T) {
	t.Setenv("TEST_STR_ENV", "off")
	s := New()
	s.Apply(map[string]interface{}{"worker.egress_proxy_mode": "enforce"})
	got := s.String("worker.egress_proxy_mode", "TEST_STR_ENV", "enforce", "off", "enforce")
	if got != "enforce" {
		t.Errorf("API value should win: got %q", got)
	}
}

func TestString_FallsBackToEnvWhenUnset(t *testing.T) {
	t.Setenv("TEST_STR_ENV", "off")
	s := New()
	got := s.String("worker.egress_proxy_mode", "TEST_STR_ENV", "enforce")
	if got != "off" {
		t.Errorf("should fall back to env var: got %q", got)
	}
}

func TestString_FallsBackToDefaultWhenNeitherSet(t *testing.T) {
	s := New()
	got := s.String("worker.egress_proxy_mode", "TEST_STR_ENV_UNSET_XYZ", "enforce")
	if got != "enforce" {
		t.Errorf("should fall back to default: got %q", got)
	}
}

func TestString_UnrecognizedAPIValueDegradesToEnvDefault(t *testing.T) {
	s := New()
	// A future manager sends an enum value this binary doesn't know about —
	// must not apply it verbatim, must fall through as if absent.
	s.Apply(map[string]interface{}{"worker.egress_proxy_mode": "quarantine"})
	got := s.String("worker.egress_proxy_mode", "", "enforce", "off", "enforce")
	if got != "enforce" {
		t.Errorf("unrecognized API value should degrade to default: got %q", got)
	}
}

func TestString_CaseInsensitiveAllowedMatch(t *testing.T) {
	s := New()
	s.Apply(map[string]interface{}{"worker.egress_proxy_mode": "OFF"})
	got := s.String("worker.egress_proxy_mode", "", "enforce", "off", "enforce")
	if got != "off" {
		t.Errorf("allowed match should be case-insensitive: got %q", got)
	}
}

func TestApply_ClearsPreviousSnapshot(t *testing.T) {
	s := New()
	s.Apply(map[string]interface{}{"worker.prefer_local_images": true})
	if !s.Bool("worker.prefer_local_images", "", false) {
		t.Fatal("precondition: value should be true after first Apply")
	}
	// A subsequent heartbeat with no tunables (operator cleared the DB row)
	// must clear the override, not leave the stale value in effect.
	s.Apply(nil)
	if s.Bool("worker.prefer_local_images", "", false) {
		t.Error("Apply(nil) should clear a previously-set override")
	}
}

func TestApply_ConcurrentReadWrite(t *testing.T) {
	s := New()
	done := make(chan struct{})
	go func() {
		for i := 0; i < 1000; i++ {
			s.Apply(map[string]interface{}{"worker.prefer_local_images": i%2 == 0})
		}
		close(done)
	}()
	for i := 0; i < 1000; i++ {
		_ = s.Bool("worker.prefer_local_images", "", false)
	}
	<-done
}

func TestSnapshot_ReturnsCopyNotAliased(t *testing.T) {
	s := New()
	s.Apply(map[string]interface{}{"k": "v"})
	snap := s.Snapshot()
	snap["k"] = "mutated"
	if s.Bool("k", "", false) != false {
		// Bool on a non-bool string "v" falls to default anyway; the real
		// assertion is that mutating the returned map didn't touch internal
		// state — verified via String() below instead.
	}
	if got := s.String("k", "", ""); got != "v" {
		t.Errorf("mutating the Snapshot() result must not affect the store: got %q", got)
	}
}

// LOW #8 (S-go-worker-auth): a server-pushed number outside the int32 range
// (or NaN/Inf) must not become an implementation-defined int; Int keeps def.
func TestInt_OutOfRangeAndNonFiniteKeepDefault(t *testing.T) {
	s := New()
	cases := map[string]interface{}{
		"k.huge":    float64(1e300),
		"k.neghuge": float64(-1e300),
		"k.nan":     math.NaN(),
		"k.inf":     math.Inf(1),
		"k.i64":     int64(math.MaxInt32) + 1,
		"k.num":     json.Number("99999999999"),
	}
	s.Apply(cases)
	for k := range cases {
		if got := s.Int(k, 42); got != 42 {
			t.Errorf("Int(%s) = %d, want default 42", k, got)
		}
	}
}

func TestInt_InRangeValuesStillResolve(t *testing.T) {
	s := New()
	s.Apply(map[string]interface{}{
		"k.f": float64(7), "k.neg": float64(-3), "k.i": 9,
		"k.s": " 11 ", "k.n": json.Number("13"),
	})
	want := map[string]int{"k.f": 7, "k.neg": -3, "k.i": 9, "k.s": 11, "k.n": 13}
	for k, w := range want {
		if got := s.Int(k, 42); got != w {
			t.Errorf("Int(%s) = %d, want %d", k, got, w)
		}
	}
}
