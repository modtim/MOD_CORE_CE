// Package tlsconf is the worker half of GSEC FX.13 (R-059): trusting the
// platform's internal service CA when the worker talks to the API (and
// other internal endpoints) over TLS.
//
// Bootstrap env MOD_INTERNAL_CA_BUNDLE (see EnvVar) names the bind-mounted,
// read-only platform CA bundle. Everything in this package is gated on that
// var: unset ⇒ both exported functions return nil ⇒ callers leave their
// http clients untouched, which is byte-identical to the pre-R-059
// plaintext / default-root behaviour.
package tlsconf

import (
	"crypto/tls"
	"crypto/x509"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
)

// EnvVar is the bootstrap env naming the platform internal CA bundle. It is
// connection identity (compose env), not a runtime tunable — there is
// deliberately no DB-backed alternative.
const EnvVar = "MOD_INTERNAL_CA_BUNDLE"

// BundleContainerPath is where the worker bind-mounts the bundle into node
// containers (read-only, only when set — see the executor's API_URL block).
const BundleContainerPath = "/etc/mod/internal-ca-bundle.pem"

// TLS policy (CNSA 2.0 client requirement): every *tls.Config this package
// builds sets MinVersion EXPLICITLY — no constructor may rely on the Go stdlib
// default. DefaultMinVersion is the worker policy for internal hops (API /
// Vault / …); the Caddy edge and the platform settings lock allow TLS 1.3 in
// every mode (see DIAG-TLS13-CE-ENDPOINTS-COMPAT). TL12MinVersion is the
// DOCUMENTED opt-down for the one peer class that may speak only TLS 1.2: the
// user-configurable S3/BYO transport. That hop lives in
// executor/stacksnapshot.go and keeps TLS 1.2 on its own — the 1.3 pin must
// NOT leak into it.
var (
	// DefaultMinVersion is the TLS floor for internal-endpoint clients.
	// Declared var (not const) so tests can exercise the table; production
	// never mutates it.
	DefaultMinVersion uint16 = tls.VersionTLS13
	// TL12MinVersion is the opt-down floor for 1.2-capable peers (S3/BYO).
	// Declared var (not const) so tests can exercise the table; production
	// never mutates it.
	TL12MinVersion uint16 = tls.VersionTLS12
)

var (
	loadOnce  sync.Once
	bundlePEM []byte // raw PEM; nil when unset / unreadable / unparseable

	poolOnce sync.Once
	cPool    *x509.CertPool

	// The tls.Config itself is built per call (buildTLSConfig), never cached, so
	// every constructor sets MinVersion explicitly; only the root-CA pool is cached.
	cfgOnce   sync.Once
	cRootPool *x509.CertPool // system+internal roots
)

// load reads the configured bundle at most once per process. When the path
// is set but unreadable or holds no parseable certs it logs once and leaves
// bundlePEM nil (fallback to default behaviour). Unset logs nothing.
func load() {
	loadOnce.Do(func() {
		path := strings.TrimSpace(os.Getenv(EnvVar))
		if path == "" {
			return // unset: inert default
		}
		pem, err := os.ReadFile(path)
		if err != nil {
			slog.Warn("R-059: "+EnvVar+" is set but unreadable; falling back to default TLS roots",
				"path", path, "err", err)
			return
		}
		if !x509.NewCertPool().AppendCertsFromPEM(pem) {
			slog.Warn("R-059: "+EnvVar+" contains no parseable certificates; falling back to default TLS roots",
				"path", path)
			return
		}
		bundlePEM = pem
	})
}

// LoadInternalCA returns a pool holding the internal CA certs from
// MOD_INTERNAL_CA_BUNDLE, or nil when the var is unset/unreadable (logging
// at most once in the latter case).
func LoadInternalCA() *x509.CertPool {
	load()
	if bundlePEM == nil {
		return nil
	}
	poolOnce.Do(func() {
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(bundlePEM)
		cPool = pool
	})
	return cPool
}

// HTTPClientTLSConfig returns a *tls.Config for an http client talking to
// internal (API / Vault / …) endpoints: RootCAs = system pool + internal
// CA, MinVersion = DefaultMinVersion (TLS 1.3, explicit — never the Go
// default). Returns nil when MOD_INTERNAL_CA_BUNDLE is unset/unreadable —
// callers must then NOT set any transport, keeping the client byte-identical
// to today's behaviour.
func HTTPClientTLSConfig() *tls.Config {
	return HTTPClientTLSConfigMinVersion(DefaultMinVersion)
}

// HTTPClientTLSConfigMinVersion is the documented opt-down: same internal-CA
// trust, but the caller picks the TLS floor (e.g. TL12MinVersion for the
// user-configurable S3/BYO peer, which may speak only TLS 1.2 — see
// DIAG-TLS13-CE-ENDPOINTS-COMPAT). MinVersion is always set explicitly.
func HTTPClientTLSConfigMinVersion(minVersion uint16) *tls.Config {
	load()
	if bundlePEM == nil {
		return nil
	}
	cfgOnce.Do(func() {
		sys, err := x509.SystemCertPool()
		sys = mergeRoots(sys, err, bundlePEM)
		cRootPool = sys
	})
	// The root-CA pool is cached once; the *tls.Config is built per call so an
	// opt-down (1.2) caller never receives a cached 1.3 config, and no caller
	// mutating its config can change another client.
	return buildTLSConfig(cRootPool, minVersion)
}

// mergeRoots returns sys (or an empty pool when the system pool is unavailable,
// failing closed) with the internal CA bundle appended, warning on either
// failure instead of swallowing it.
func mergeRoots(sys *x509.CertPool, sysErr error, bundle []byte) *x509.CertPool {
	if sysErr != nil || sys == nil {
		slog.Warn("R-059: system cert pool unavailable; trusting only the internal CA bundle", "err", sysErr)
		sys = x509.NewCertPool()
	}
	if !sys.AppendCertsFromPEM(bundle) {
		slog.Warn("R-059: internal CA bundle could not be appended to the root pool")
	}
	return sys
}

// buildTLSConfig is the ONLY tls.Config constructor in this package: it
// always sets MinVersion explicitly from minVersion (never the Go default).
func buildTLSConfig(rootCAs *x509.CertPool, minVersion uint16) *tls.Config {
	return &tls.Config{RootCAs: rootCAs, MinVersion: minVersion}
}

// SetClientTLS wires internal-CA trust into client when (and only when)
// MOD_INTERNAL_CA_BUNDLE names a readable bundle; otherwise it leaves the
// client untouched. Safe to call with a nil client or a nil config.
func SetClientTLS(client *http.Client) {
	if client == nil {
		return
	}
	tc := HTTPClientTLSConfig()
	if tc == nil {
		return
	}
	if tr, ok := http.DefaultTransport.(*http.Transport); ok {
		cloned := tr.Clone()
		cloned.TLSClientConfig = tc
		client.Transport = cloned
		return
	}
	// Fail closed: the client keeps its default roots, so the internal CA is
	// NOT trusted and TLS to internal endpoints will fail. Say so.
	slog.Warn("R-059: http.DefaultTransport is not *http.Transport; internal CA not applied to client")
}

// resetForTest clears the process caches so tests can re-exercise the
// load path with a different env. Test-only.
func resetForTest() {
	loadOnce = sync.Once{}
	poolOnce = sync.Once{}
	cfgOnce = sync.Once{}
	bundlePEM = nil
	cPool = nil
	cRootPool = nil
}
