package tlsconf

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// writeCA writes a throwaway self-signed CA cert into a temp dir and
// returns (path, *x509.Certificate).
func writeCA(t *testing.T) (string, *x509.Certificate) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "R-059 test internal CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	path := filepath.Join(t.TempDir(), "ca-bundle.pem")
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return path, cert
}

func TestLoadInternalCA_Unset(t *testing.T) {
	resetForTest()
	t.Setenv(EnvVar, "")
	if p := LoadInternalCA(); p != nil {
		t.Fatalf("LoadInternalCA() with unset env = %v, want nil", p)
	}
	if c := HTTPClientTLSConfig(); c != nil {
		t.Fatalf("HTTPClientTLSConfig() with unset env = %v, want nil", c)
	}
}

func TestLoadInternalCA_MissingFile(t *testing.T) {
	resetForTest()
	t.Setenv(EnvVar, filepath.Join(t.TempDir(), "does-not-exist.pem"))
	if p := LoadInternalCA(); p != nil {
		t.Fatalf("LoadInternalCA() with missing file = %v, want nil", p)
	}
	if c := HTTPClientTLSConfig(); c != nil {
		t.Fatalf("HTTPClientTLSConfig() with missing file = %v, want nil", c)
	}
}

func TestLoadInternalCA_Unparseable(t *testing.T) {
	resetForTest()
	path := filepath.Join(t.TempDir(), "junk.pem")
	if err := os.WriteFile(path, []byte("not a pem file"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EnvVar, path)
	if p := LoadInternalCA(); p != nil {
		t.Fatalf("LoadInternalCA() with junk PEM = %v, want nil", p)
	}
}

func TestLoadInternalCA_ValidAndConfig(t *testing.T) {
	resetForTest()
	path, cert := writeCA(t)
	t.Setenv(EnvVar, "  "+path+"  ") // whitespace around the path must be trimmed

	pool := LoadInternalCA()
	if pool == nil {
		t.Fatal("LoadInternalCA() = nil, want pool with the test CA")
	}
	cfg := HTTPClientTLSConfig()
	if cfg == nil || cfg.RootCAs == nil {
		t.Fatal("HTTPClientTLSConfig() = nil/empty RootCAs, want system pool + internal CA")
	}
	// The self-signed CA must verify against the returned RootCAs.
	if _, err := cert.Verify(x509.VerifyOptions{
		Roots:     cfg.RootCAs,
		KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny},
	}); err != nil {
		t.Fatalf("CA does not verify against returned RootCAs: %v", err)
	}
	// Calling again must return the same cached instances (log-once +
	// single build per process).
	if p2 := LoadInternalCA(); p2 != pool {
		t.Fatal("LoadInternalCA() is not cached")
	}
	// The root-CA pool is cached; the *tls.Config is built per call (so an
	// opt-down caller never receives a cached 1.3 config). Config identity is
	// no longer required; the RootCAs pool must be the cached instance.
	if c2 := HTTPClientTLSConfig(); c2 == nil || c2.RootCAs != cfg.RootCAs {
		t.Fatal("HTTPClientTLSConfig() RootCAs is not the cached pool")
	}
}

func TestSetClientTLS_NilConfigLeavesClientUnchanged(t *testing.T) {
	resetForTest()
	t.Setenv(EnvVar, "")
	c := &http.Client{}
	SetClientTLS(c)
	if c.Transport != nil {
		t.Fatal("SetClientTLS must not touch the client when the env is unset")
	}
	SetClientTLS(nil) // must not panic
}

func TestSetClientTLS_WithBundle(t *testing.T) {
	resetForTest()
	path, _ := writeCA(t)
	t.Setenv(EnvVar, path)
	c := &http.Client{}
	SetClientTLS(c)
	if c.Transport == nil {
		t.Fatal("SetClientTLS must set a Transport when the env names a readable bundle")
	}
}

// --- TLS 1.3 pin (FIX-GO-WORKER-TLS-MIN-1-3) --------------------------------
//
// fixturePair builds a THROWAWAY self-signed EC P-256 cert (localhost SAN)
// at test time and returns it as both a tls.Certificate (for the loopback
// server) and its PEM bytes (for the internal-CA bundle the client trusts).
// Test material only: never used off-box, verifies only localhost over TLS.
func fixturePair(t *testing.T) (tls.Certificate, []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		Subject:               pkix.Name{CommonName: "mod tlsconf pin test"},
		DNSNames:              []string{"localhost"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key, Leaf: leaf}, pemBytes
}

// okHandler answers any request with 200 "ok".
type okHandler struct{}

func (okHandler) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	w.Write([]byte("ok"))
}

// serveLoopbackTLS starts a loopback https server whose tls.Config is cfg
// (fixture cert) and returns its "host:port" address (localhost form); the
// handler answers 200 "ok".
func serveLoopbackTLS(t *testing.T, cfg *tls.Config, pair tls.Certificate) string {
	t.Helper()
	if cfg == nil {
		t.Fatal("nil server tls.Config")
	}
	cfg.Certificates = []tls.Certificate{pair}
	lst, err := tls.Listen("tcp", "127.0.0.1:0", cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lst.Close() })
	go func() { http.Serve(lst, okHandler{}) }()
	addr := lst.Addr().String()
	if addr == "" {
		t.Fatal("listener address empty")
	}
	return strings.ReplaceAll(addr, "127.0.0.1", "localhost")
}

// writeFixtureBundle points the bootstrap env at a temp file holding the
// fixture CA, so HTTPClientTLSConfig()/SetClientTLS build a client whose
// RootCAs trust the loopback fixture cert.
func writeFixtureBundle(t *testing.T, pemBytes []byte) {
	t.Helper()
	// resetForTest is called by the caller's own reset; this helper only
	// swaps the env+file so tests pair it with their own reset ordering.
	path := filepath.Join(t.TempDir(), "ca-bundle.pem")
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(EnvVar, path)
}

func TestHTTPClientTLSConfig_MinVersionPinnedToTLS13(t *testing.T) {
	resetForTest()
	_, pem := fixturePair(t)
	writeFixtureBundle(t, pem)
	cfg := HTTPClientTLSConfig()
	if cfg == nil {
		t.Fatal("HTTPClientTLSConfig() = nil, want the pinned config")
	}
	if cfg.MinVersion != tls.VersionTLS13 {
		t.Fatalf("MinVersion = %#x, want %#x (tls.VersionTLS13)", cfg.MinVersion, tls.VersionTLS13)
	}
}

// TestConstructorTable_MinVersionExplicit: every tlsconf constructor must
// report the expected MinVersion (no reliance on the Go default). The mutant
// (MCW_OVERRIDE of tlsconf.go with buildTLSConfig's MinVersion removed) makes
// this table red.
func TestConstructorTable_MinVersionExplicit(t *testing.T) {
	resetForTest()
	_, pem := fixturePair(t)
	writeFixtureBundle(t, pem)
	// (1) the default internal-endpoint config: TLS 1.3.
	d := HTTPClientTLSConfig()
	if d == nil {
		t.Fatal("HTTPClientTLSConfig() = nil, want the pinned config")
	}
	// (2) the documented opt-down: TLS 1.2 for 1.2-only peers (S3/BYO class).
	o := HTTPClientTLSConfigMinVersion(TL12MinVersion)
	if o == nil {
		t.Fatal("HTTPClientTLSConfigMinVersion(TL12MinVersion) = nil, want the 1.2 config")
	}
	// (3) an explicit 1.3 opt-down must also report 1.3.
	e := HTTPClientTLSConfigMinVersion(DefaultMinVersion)
	if e == nil {
		t.Fatal("HTTPClientTLSConfigMinVersion(DefaultMinVersion) = nil")
	}
	// The opt-down config must differ from the default config (no cached
	// 1.3 config leaking to a 1.2 caller).
	if o.MinVersion != TL12MinVersion {
		t.Fatalf("opt-down MinVersion = %#x, want %#x", o.MinVersion, TL12MinVersion)
	}
	if d.MinVersion != DefaultMinVersion {
		t.Fatalf("default MinVersion = %#x, want %#x", d.MinVersion, DefaultMinVersion)
	}
	if e.MinVersion != DefaultMinVersion {
		t.Fatalf("explicit 1.3 MinVersion = %#x, want %#x", e.MinVersion, DefaultMinVersion)
	}
	// RootCAs must be the shared cached pool in all three.
	if d.RootCAs != o.RootCAs || o.RootCAs != e.RootCAs {
		t.Fatal("RootCAs pool not shared across constructors")
	}
	// No constructor may report 0 (the Go default = policy not stated).
	if d.MinVersion == 0 || o.MinVersion == 0 || e.MinVersion == 0 {
		t.Fatal("a constructor left MinVersion at the Go default")
	}
}

func TestPinnedClient_RefusesTL12OnlyServerAcceptsTL13(t *testing.T) {
	resetForTest()
	pair, pem := fixturePair(t)
	writeFixtureBundle(t, pem)
	c := &http.Client{}
	SetClientTLS(c)
	if c.Transport == nil {
		t.Fatal("SetClientTLS left the client untouched; want the pinned transport")
	}
	// (b1) a server limited to TLS 1.2 must be REFUSED by the pinned client.
	only12 := serveLoopbackTLS(t, &tls.Config{MaxVersion: tls.VersionTLS12}, pair)
	_, err12 := c.Get("https://" + only12 + "/")
	if err12 == nil {
		t.Fatal("TLS 1.2-only server was ACCEPTED; the 1.3 pin must refuse it")
	}
	msg := err12.Error()
	if !strings.Contains(msg, "protocol version") && !strings.Contains(msg, "unsupported") {
		t.Fatalf("refusal error does not mention the protocol version: %s", msg)
	}
	// (b2) a TLS 1.3 server must be ACCEPTED.
	only13 := serveLoopbackTLS(t, &tls.Config{MinVersion: tls.VersionTLS13}, pair)
	resp, err13 := c.Get("https://" + only13 + "/")
	if err13 != nil {
		t.Fatalf("TLS 1.3 server refused: %s", err13.Error())
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "ok" {
		t.Fatalf("body = %q, want ok", string(b))
	}
}

// --- S-go-worker-auth LOW findings #5, #6, #7 ---

func captureSlog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(old) })
	return &buf
}

type noopRT struct{}

func (noopRT) RoundTrip(*http.Request) (*http.Response, error) { return nil, io.EOF }

func TestSetClientTLS_WarnsWhenDefaultTransportNotHTTPTransport(t *testing.T) {
	resetForTest()
	path, _ := writeCA(t)
	t.Setenv(EnvVar, path)
	buf := captureSlog(t)
	old := http.DefaultTransport
	http.DefaultTransport = noopRT{}
	t.Cleanup(func() { http.DefaultTransport = old })
	c := &http.Client{}
	SetClientTLS(c)
	if c.Transport != nil {
		t.Fatal("client must stay untouched (fail closed) when the transport type is unknown")
	}
	if !strings.Contains(buf.String(), "not *http.Transport") {
		t.Fatalf("expected a warning, got %q", buf.String())
	}
}

func TestMergeRoots_WarnsOnSystemPoolErrorAndBadBundle(t *testing.T) {
	buf := captureSlog(t)
	pool := mergeRoots(nil, io.ErrUnexpectedEOF, []byte("not pem"))
	if pool == nil {
		t.Fatal("must fail closed to an empty pool, never nil")
	}
	out := buf.String()
	if !strings.Contains(out, "system cert pool unavailable") || !strings.Contains(out, "could not be appended") {
		t.Fatalf("expected both warnings, got %q", out)
	}
}

func TestMergeRoots_QuietOnSuccess(t *testing.T) {
	buf := captureSlog(t)
	path, _ := writeCA(t)
	pemBytes, _ := os.ReadFile(path)
	mergeRoots(x509.NewCertPool(), nil, pemBytes)
	if buf.Len() != 0 {
		t.Fatalf("no warning expected, got %q", buf.String())
	}
}

func TestHTTPClientTLSConfig_ReturnsDistinctConfigPerCall(t *testing.T) {
	resetForTest()
	path, _ := writeCA(t)
	t.Setenv(EnvVar, path)
	a, b := HTTPClientTLSConfig(), HTTPClientTLSConfig()
	if a == nil || b == nil || a == b {
		t.Fatal("each call must return its own *tls.Config")
	}
	a.MinVersion = tls.VersionTLS10
	if b.MinVersion != DefaultMinVersion {
		t.Fatal("mutating one config must not change another")
	}
}
