package selfupdate

import (
    "crypto/ecdsa"
    "crypto/elliptic"
    "crypto/rand"
    "crypto/sha256"
    "crypto/x509"
    "encoding/base64"
    "encoding/pem"
    "fmt"
    "net/http"
    "net/http/httptest"
    "os"
    "path/filepath"
    "testing"
)

// These tests drive the whole download ceremony — HTTP fetch of the sibling
// .sig, read-back of the bytes that actually landed on disk, verify against
// the compiled-in key — rather than VerifyArtifact's maths (verify_test.go
// covers that). The point is the shape of a hijacked channel: whatever the
// channel serves, the only thing that decides whether the caller may swap a
// running executable is the key this binary was built with.

func newReleaseKeyPair() (*ecdsa.PrivateKey, []byte) {
    priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
    if err != nil {
        panic(fmt.Sprintf("genkey: %v", err))
    }
    der, err := x509.MarshalPKIXPublicKey(priv.Public())
    if err != nil {
        panic(fmt.Sprintf("marshal key: %v", err))
    }
    return priv, pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
}

func blobSig(priv *ecdsa.PrivateKey, artifact []byte) []byte {
    digest := sha256.Sum256(artifact)
    der, err := ecdsa.SignASN1(rand.Reader, priv, digest[:])
    if err != nil {
        panic(fmt.Sprintf("sign: %v", err))
    }
    return []byte(base64.StdEncoding.EncodeToString(der))
}

// stageArtefact writes candidate bytes to a temp path the way a download does
// and returns it, so every test verifies what is really on disk.
func stageArtefact(t *testing.T, content []byte) string {
    dir := t.TempDir()
    path := filepath.Join(dir, "mod-worker-linux-amd64")
    if err := os.WriteFile(path, content, 0755); err != nil {
        t.Fatalf("stage artefact: %v", err)
    }
    return path
}

// compileInReleaseKey makes this test process look like a release build: the
// pipeline swaps the compiled-in key either by overwriting release_pub.pem
// before the build or by the ldflags -X override on the same symbol, and
// assigning here is the runtime shape of that second route. It restores the
// committed placeholder afterwards so the fail-closed test still sees a repo
// build. Nothing in production code can reach this — the symbol has no setter.
func compileInReleaseKey(t *testing.T, pubPEM []byte) {
    committed := ReleasePubKeyPEM
    ReleasePubKeyPEM = string(pubPEM)
    t.Cleanup(func() { ReleasePubKeyPEM = committed })
}

// serveSig stands a channel out that answers every request with `body`
// (status `code`), and records the Authorization header it saw so the header
// forwarding can be asserted.
func serveSig(t *testing.T, code int, body []byte) (*httptest.Server, *http.Client, []string) {
    seen := make([]string, 1)
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        seen[0] = r.Header.Get("Authorization")
        w.WriteHeader(code)
        w.Write(body)
    }))
    t.Cleanup(srv.Close)
    return srv, &http.Client{}, seen
}

func TestFetchAndVerify_GoodSignatureApplies(t *testing.T) {
    priv, pubPEM := newReleaseKeyPair()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    sig := blobSig(priv, artifact)

    compileInReleaseKey(t, pubPEM)
    srv, client, seen := serveSig(t, http.StatusOK, sig)
    path := stageArtefact(t, artifact)
    headers := map[string]string{"Authorization": "Bearer bootstrap-token"}

    if err := VerifyDownloadedArtifact(srv.URL+"/mod-worker-linux-amd64", path, client, headers); err != nil {
        t.Fatalf("a genuine artefact+signature was refused: %v", err)
    }
    if seen[0] != "Bearer bootstrap-token" {
        t.Fatalf("channel headers not forwarded to the .sig fetch, saw: %q", seen[0])
    }
    // Sanity: the key that signed is the key this test says the pipeline
    // embeds, so the pass above is about the ceremony, not a lucky key.
    if err := VerifyArtifact(artifact, sig, pubPEM); err != nil {
        t.Errorf("sanity: right key/right sig refused: %v", err)
    }
}

func TestFetchAndVerify_TamperedPayloadRefused(t *testing.T) {
    priv, pubPEM := newReleaseKeyPair()
    genuine := []byte("mod-worker-linux-amd64 bytes as they ship")
    sig := blobSig(priv, genuine)

    // With the signing key compiled in, the refusal below is about the bytes,
    // not about the placeholder key.
    compileInReleaseKey(t, pubPEM)
    srv, client, _ := serveSig(t, http.StatusOK, sig)
    tampered := []byte("mod-worker-linux-amd64 bytes as they shiP")
    path := stageArtefact(t, tampered)

    if err := VerifyDownloadedArtifact(srv.URL+"/mod-worker-linux-amd64", path, client, nil); err == nil {
        t.Fatal("a one-byte-tampered artefact verified against the untouched signature")
    }
}

func TestFetchAndVerify_MissingSignatureRefused(t *testing.T) {
    // A release-shaped build, so the refusal below is about the missing .sig
    // and not the placeholder key.
    _, pubPEM := newReleaseKeyPair()
    compileInReleaseKey(t, pubPEM)
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    path := stageArtefact(t, artifact)

    // The channel serves the artefact fine but never got a .sig uploaded
    // beside it: a 404, and an empty 200, both refuse.
    srv404, client404, _ := serveSig(t, http.StatusNotFound, nil)
    if err := VerifyDownloadedArtifact(srv404.URL+"/mod-worker-linux-amd64", path, client404, nil); err == nil {
        t.Fatal("an artefact with a 404 .sig verified")
    }
    srvEmpty, clientEmpty, _ := serveSig(t, http.StatusOK, nil)
    if err := VerifyDownloadedArtifact(srvEmpty.URL+"/mod-worker-linux-amd64", path, clientEmpty, nil); err == nil {
        t.Fatal("an artefact with an empty .sig verified")
    }
}

func TestFetchAndVerify_BadSignatureRefused(t *testing.T) {
    _, pubPEM := newReleaseKeyPair()
    compileInReleaseKey(t, pubPEM)
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    path := stageArtefact(t, artifact)

    for _, bad := range []string{"not base64!!!", "c291d", "   "} {
        srv, client, _ := serveSig(t, http.StatusOK, []byte(bad))
        if err := VerifyDownloadedArtifact(srv.URL+"/mod-worker-linux-amd64", path, client, nil); err == nil {
            t.Fatalf("a corrupt signature %q verified", bad)
        }
    }

    // A signature made by someone else's key — the shape of a bucket where
    // attacker bytes were re-signed with their own key.
    other, _ := newReleaseKeyPair()
    srv, client, _ := serveSig(t, http.StatusOK, blobSig(other, artifact))
    if err := VerifyDownloadedArtifact(srv.URL+"/mod-worker-linux-amd64", path, client, nil); err == nil {
        t.Fatal("a signature from an unrelated key verified")
    }
}

func TestFetchAndVerify_FailsClosedWithThePlaceholderKey(t *testing.T) {
    if HasReleaseKey() {
        t.Fatal("the committed placeholder key counts as a release key")
    }
    priv, _ := newReleaseKeyPair()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    srv, client, _ := serveSig(t, http.StatusOK, blobSig(priv, artifact))
    path := stageArtefact(t, artifact)
    if err := VerifyDownloadedArtifact(srv.URL+"/mod-worker-linux-amd64", path, client, nil); err == nil {
        t.Fatal("the placeholder build applied a signed artefact — must refuse until a real key is compiled in")
    }
}

func TestSignatureURL_KeepsArtefactExtension(t *testing.T) {
    // The .sig is a sibling, appended: a windows/mac artefact keeps its
    // extension (the loader/AV contract in publish.go) and still signs as
    // <name>.exe.sig / <name>.msi.sig.
    for _, want := range []struct{
        artifact, sig string
    }{
        {"https://api/v1/workers/download/linux/amd64", "https://api/v1/workers/download/linux/amd64.sig"},
        {"https://minio/worker-releases/MODTray-1.0.71.exe", "https://minio/worker-releases/MODTray-1.0.71.exe.sig"},
        {"https://minio/worker-releases/MOD-Setup-1.0.71.msi", "https://minio/worker-releases/MOD-Setup-1.0.71.msi.sig"},
    } {
        if got := SignatureURL(want.artifact); got != want.sig {
            t.Errorf("SignatureURL(%s) = %s, want %s", want.artifact, got, want.sig)
        }
    }
}
