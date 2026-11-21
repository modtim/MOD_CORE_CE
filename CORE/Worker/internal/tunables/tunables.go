// Package tunables holds worker config pushed live from the API's
// platform_settings registry (GS.PSR.7 — "deliver worker tunables from the
// API instead of per-box env recreate").
//
// The heartbeat response carries a `tunables` map of {registry key: value},
// populated ONLY for keys the operator has actually set in the DB (see
// routers/worker_management.py:_resolve_worker_tunables on the API side). An
// absent key means "nothing changed" — the reader falls through to its local
// env var and then its built-in default, exactly as before this landed.
//
// Store is the single process-wide holder of the latest snapshot. It is
// replaced wholesale on every heartbeat (Apply), so a value set/cleared in
// the Dashboard reaches every reader within two heartbeat intervals without
// a worker restart. Readers call the typed getters (Bool/String) with the
// same registry key the API used to register the setting; an unrecognized
// key the server sends (newer manager talking to an older worker binary) is
// stored but never read by anyone — inert, not an error.
package tunables

import (
	"encoding/json"
	"math"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
)

// Store is a thread-safe, hot-swappable snapshot of server-pushed worker
// tunables. The zero value is not usable — construct with New().
type Store struct {
	v atomic.Value // map[string]interface{}
}

// New returns an empty Store (every getter falls back to env/default until
// the first heartbeat response arrives).
func New() *Store {
	s := &Store{}
	s.v.Store(map[string]interface{}{})
	return s
}

// Apply replaces the live tunables snapshot with m. Safe to call from any
// goroutine (the heartbeat sender calls it after decoding each response);
// safe to read from any goroutine concurrently via Bool/String.
//
// m may be nil (server sent no tunables this beat, or an older manager that
// predates this field) — that clears back to "nothing set", which is the
// correct read: a value the operator un-set in the DB must stop applying,
// not linger from a stale snapshot.
func (s *Store) Apply(m map[string]interface{}) {
	cp := make(map[string]interface{}, len(m))
	for k, val := range m {
		cp[k] = val
	}
	s.v.Store(cp)
}

func (s *Store) get(key string) (interface{}, bool) {
	m, _ := s.v.Load().(map[string]interface{})
	v, ok := m[key]
	return v, ok
}

// Bool resolves key with precedence: server-pushed tunable > local env var
// (envVar, skipped when "") > def. Accepts either a JSON bool or a string
// parseable by strconv.ParseBool from the server (the API's registry stores
// bools as native JSON booleans, but the type is not assumed here in case a
// future caller feeds it a decoded string map instead).
func (s *Store) Bool(key, envVar string, def bool) bool {
	if raw, ok := s.get(key); ok {
		switch t := raw.(type) {
		case bool:
			return t
		case string:
			if b, err := strconv.ParseBool(strings.TrimSpace(t)); err == nil {
				return b
			}
		}
	}
	if envVar != "" {
		if v := strings.TrimSpace(os.Getenv(envVar)); v != "" {
			if b, err := strconv.ParseBool(v); err == nil {
				return b
			}
		}
	}
	return def
}

// Int resolves a numeric key with precedence: server-pushed tunable (JSON
// number, int, or string parseable by strconv.Atoi) > def. There is no env
// fallback: a box-local env for a numeric threshold would be a second
// configuration surface, and the platform-settings registry is the only
// behavioural source this codebase uses for worker tunables.
func (s *Store) Int(key string, def int) int {
	raw, ok := s.get(key)
	if !ok {
		return def
	}
	switch t := raw.(type) {
	case float64:
		// Out-of-range / non-finite floats are implementation-defined when
		// converted to int in Go: treat them as unusable and keep def.
		if math.IsNaN(t) || math.IsInf(t, 0) || t < math.MinInt32 || t > math.MaxInt32 {
			return def
		}
		return int(t)
	case int:
		return t
	case int64:
		if t < math.MinInt32 || t > math.MaxInt32 {
			return def
		}
		return int(t)
	case json.Number:
		if v, err := t.Int64(); err == nil && v >= math.MinInt32 && v <= math.MaxInt32 {
			return int(v)
		}
	case string:
		if v, err := strconv.Atoi(strings.TrimSpace(t)); err == nil {
			return v
		}
	}
	return def
}

// String resolves key with precedence: server-pushed tunable (if it is a
// string, non-empty, and — when allowed is non-empty — a case-insensitive
// match for one of the allowed values) > local env var (envVar, skipped
// when "") > def.
//
// allowed exists so a server ahead of this binary (a new enum value it does
// not understand yet) degrades to the local default instead of applying an
// arbitrary string straight into worker logic — the same "unknown keys are
// ignored" contract extended to unknown VALUES for enum-typed settings.
func (s *Store) String(key, envVar, def string, allowed ...string) string {
	if raw, ok := s.get(key); ok {
		if str, ok := raw.(string); ok {
			str = strings.TrimSpace(str)
			if str != "" {
				if len(allowed) == 0 {
					return str
				}
				for _, a := range allowed {
					if strings.EqualFold(a, str) {
						return strings.ToLower(str)
					}
				}
			}
		}
	}
	if envVar != "" {
		if v := strings.TrimSpace(os.Getenv(envVar)); v != "" {
			return v
		}
	}
	return def
}

// Snapshot returns a copy of the current tunables map, for status/debug
// surfaces (e.g. the local management UI). Never nil.
func (s *Store) Snapshot() map[string]interface{} {
	m, _ := s.v.Load().(map[string]interface{})
	cp := make(map[string]interface{}, len(m))
	for k, v := range m {
		cp[k] = v
	}
	return cp
}
