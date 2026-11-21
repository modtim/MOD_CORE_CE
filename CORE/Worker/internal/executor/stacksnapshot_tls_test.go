package executor

// add-multi-region-mesh §2.4 — the MinIO transport builder for secure
// cross-region transfers (TLS trust + mTLS client cert from Vault-templated
// files). These exercise the config logic without needing a live TLS server.

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fixturePair builds a THROWAWAY self-signed EC P-256 cert (localhost SAN)
// at test time and returns it as both a tls.Certificate (for the loopback
// server) and its PEM bytes. Test material only: never used off-box.
func fixturePair(t *testing.T) (tls.Certificate, []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		Subject:               pkix.Name{CommonName: "mod stacksnapshot tls test"},
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

// serveTLSCapped starts a loopback https server (fixture cert) whose tls.Config
// caps/floors versions as given; returns its "localhost:port" address.
func serveTLSCapped(t *testing.T, cfg *tls.Config, pair tls.Certificate) string {
	t.Helper()
	cfg.Certificates = []tls.Certificate{pair}
	lst, err := tls.Listen("tcp", "127.0.0.1:0", cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lst.Close() })
	go func() { http.Serve(lst, okHandler{}) }()
	return strings.ReplaceAll(lst.Addr().String(), "127.0.0.1", "localhost")
}

// realCAPEM is a real self-signed certificate (openssl req -x509), not bogus
// bytes — the bogus-content path is asserted by TestMinioTLSTransport_InvalidCAContent.
const realCAPEM = `-----BEGIN CERTIFICATE-----
MIIDFzCCAf+gAwIBAgIUY6GN1g7kFgycdC5M9Rwh6oG1cIAwDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQTU9EIENvcmUgdGVzdCBDQTAeFw0yNjExMDQxNjE0Mzla
Fw0yNjExMDUxNjE0MzlaMBsxGTAXBgNVBAMMEE1PRCBDb3JlIHRlc3QgQ0EwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDIb/d1Rrx2ns7uIdVQ7Vx/UXmM
91Wsi4NpsXwX3vlZh+/H3hJ3SXAXZsZnlmltX2CW4XOZ7185a6GRYIxLLGfmZjGz
N5u0wBJWv/gRV2QSarvD0SJhUhXJwwheZTsoOUT2NrTWfNm8XJx8k2JfixxHXFwO
SXzP5UDlbAAjeSvQorx0Mbk8lThb/T/fwoCo7wqE38L7IsBjQh2V8ftpTfb5yCX2
SXmViz7khgboGAHsXcEwdUDu1a/1AcPHKFjung9vXgW78YcacixeVJ3X9g2QLsLp
LJWuLkKCuBnyCkYA2xKvY7mz4JB8b6U7huZb39uo7HIU7O357W8tBRer5oP7AgMB
AAGjUzBRMB0GA1UdDgQWBBQrFIQ+KIgMY2SIX+rNhRjeYI1zWzAfBgNVHSMEGDAW
gBQrFIQ+KIgMY2SIX+rNhRjeYI1zWzAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3
DQEBCwUAA4IBAQCF+UT5Jeq+P2wSFxWLrIUpAIhj1NibEwUGHCHXKupUFb3oQqcN
6q4TAtmtSTRrnus/kRxGaSnkCCx8GQFfi6gEzWHHlkmueuMo/VZw31YAOgA9s1in
XEWZXigQj29+bGftAtDcDS/Xmf+VMv+IBB1xc8gCU9SgXeiJC/aasp+Zo7w5Oxlt
H3XF6mKELchONusAPmw5y/AFBd9Hup4o9h2p8XBb3M3VwSHAJBTN+prIDDIqtfRW
xOYqigKsgjBweLUK4Yxaxox4MKM7La6cbilWMUrcOSZ+FZogI/iXcjZdLdiM/X1h
l2749m2weOBEeoJpeeaerSj9CIyVVtSCWXlK
-----END CERTIFICATE-----
`

func clearTLSEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{"S3_CA_CERT", "S3_CLIENT_CERT", "S3_CLIENT_KEY", "S3_TLS_SKIP_VERIFY", "MINIO_CA_CERT", "MINIO_CLIENT_CERT", "MINIO_CLIENT_KEY", "MINIO_TLS_SKIP_VERIFY"} {
		os.Unsetenv(k)
	}
}

func TestMinioTLSTransport_NoCustomConfig(t *testing.T) {
	clearTLSEnv(t)
	tr, err := minioTLSTransport()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if tr != nil {
		t.Fatalf("expected nil transport (system defaults), got %T", tr)
	}
}

func TestMinioTLSTransport_SkipVerify(t *testing.T) {
	clearTLSEnv(t)
	t.Setenv("MINIO_TLS_SKIP_VERIFY", "true")
	tr, err := minioTLSTransport()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	ht, ok := tr.(*http.Transport)
	if !ok {
		t.Fatalf("expected *http.Transport, got %T", tr)
	}
	if ht.TLSClientConfig == nil || !ht.TLSClientConfig.InsecureSkipVerify {
		t.Fatalf("expected InsecureSkipVerify=true")
	}
}

func TestMinioTLSTransport_BadCAPath(t *testing.T) {
	clearTLSEnv(t)
	t.Setenv("MINIO_CA_CERT", "/nonexistent/ca.pem")
	if _, err := minioTLSTransport(); err == nil {
		t.Fatalf("expected error for unreadable CA path")
	}
}

func TestMinioTLSTransport_InvalidCAContent(t *testing.T) {
	clearTLSEnv(t)
	dir := t.TempDir()
	bad := filepath.Join(dir, "ca.pem")
	if err := os.WriteFile(bad, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MINIO_CA_CERT", bad)
	if _, err := minioTLSTransport(); err == nil {
		t.Fatalf("expected error for non-PEM CA content")
	}
}

func TestMinioTLSTransport_ClientCertNeedsBoth(t *testing.T) {
	clearTLSEnv(t)
	t.Setenv("MINIO_CLIENT_CERT", "/some/cert.pem") // key missing
	if _, err := minioTLSTransport(); err == nil {
		t.Fatalf("expected error when only the client cert (no key) is set")
	}
}

func TestMinioTLSTransport_S3NameWins(t *testing.T) {
	clearTLSEnv(t)
	dir := t.TempDir()
	good := filepath.Join(dir, "ca.pem")
	if err := os.WriteFile(good, []byte(realCAPEM), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("S3_CA_CERT", good)
	t.Setenv("MINIO_CA_CERT", "/nonexistent/old-ca.pem")
	tr, err := minioTLSTransport()
	if err != nil {
		t.Fatalf("S3_CA_CERT path must resolve, got: %v", err)
	}
	if tr == nil {
		t.Fatal("transport = nil; want a custom transport from S3_CA_CERT")
	}
}

func TestMinioTLSTransport_SkipVerifyNewName(t *testing.T) {
	clearTLSEnv(t)
	t.Setenv("S3_TLS_SKIP_VERIFY", "true")
	tr, err := minioTLSTransport()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if tr == nil {
		t.Fatal("transport = nil; want skip-verify transport from S3_TLS_SKIP_VERIFY")
	}
}

func TestMinioTLSTransport_NeitherNameUsesDefaults(t *testing.T) {
	clearTLSEnv(t)
	tr, err := minioTLSTransport()
	if err != nil || tr != nil {
		t.Fatalf("want (nil, nil) system defaults, got (%T, %v)", tr, err)
	}
}

// FIX-GO-WORKER-TLS-MIN-1-3: the BYO S3/minio transport must STAY at TLS 1.2
// (a user-chosen https S3 server may speak only 1.2); the worker API client
// pin to 1.3 (tlsconf) must NOT leak into it.
func TestMinioTLSTransport_MinVersionStaysTLS12(t *testing.T) {
	clearTLSEnv(t)
	pair, pem := fixturePair(t)
	dir := t.TempDir()
	ca := filepath.Join(dir, "ca.pem")
	if err := os.WriteFile(ca, pem, 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("S3_CA_CERT", ca)
	tr, err := minioTLSTransport()
	if err != nil {
		t.Fatalf("transport build failed: %v", err)
	}
	ht, ok := tr.(*http.Transport)
	if !ok {
		t.Fatalf("want *http.Transport, got %T", tr)
	}
	if ht.TLSClientConfig == nil {
		t.Fatal("TLSClientConfig = nil, want the 1.2-min config")
	}
	if ht.TLSClientConfig.MinVersion != tls.VersionTLS12 {
		t.Fatalf("MinVersion = %#x, want %#x (tls.VersionTLS12)", ht.TLSClientConfig.MinVersion, tls.VersionTLS12)
	}
	// A TLS-1.2-only loopback server must be ACCEPTED by this transport.
	only12 := serveTLSCapped(t, &tls.Config{MaxVersion: tls.VersionTLS12}, pair)
	c := &http.Client{Transport: tr}
	resp, err := c.Get("https://" + only12 + "/")
	if err != nil {
		t.Fatalf("TLS 1.2-only server refused (the 1.2 floor must accept it): %v", err)
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
