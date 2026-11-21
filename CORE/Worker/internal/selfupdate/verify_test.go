package selfupdate

// The test is the other half of the ceremony: it plays the release pipeline
// (cut a P-256 key, sign an artefact the cosign way) and then attacks the
// verifier the way a hijacked download channel would — flipped byte in the
// artefact, garbage signature, a key that is not the release key, and the
// placeholder key that ships when the pipeline step is skipped.

import (
    "crypto/ecdsa"
    "crypto/elliptic"
    "crypto/rand"
    "crypto/sha256"
    "crypto/x509"
    "encoding/base64"
    "encoding/pem"
    "fmt"
    "testing"
)

// cutKey plays the release ceremony: a fresh P-256 key and its public half in
// the PEM form the pipeline embeds into release_pub.pem.
func cutKey() (*ecdsa.PrivateKey, []byte) {
    priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
    if err != nil {
        panic(fmt.Sprintf("genkey: %v", err))
    }
    der, err := x509.MarshalPKIXPublicKey(priv.Public())
    if err != nil {
        panic(fmt.Sprintf("marshal key: %v", err))
    }
    block := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
    return priv, []byte(block)
}

// signLikeCosign is the signing half of `cosign sign-blob --key`: base64 of an
// ASN.1 DER ECDSA signature over the SHA-256 of the artefact bytes.
func signLikeCosign(priv *ecdsa.PrivateKey, artifact []byte) []byte {
    digest := sha256.Sum256(artifact)
    der, err := ecdsa.SignASN1(rand.Reader, priv, digest[:])
    if err != nil {
        panic(fmt.Sprintf("sign: %v", err))
    }
    return []byte(base64.StdEncoding.EncodeToString(der))
}

func TestAVerauthorisedArtefactPasses(t *testing.T) {
    priv, pubPEM := cutKey()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    sig := signLikeCosign(priv, artifact)
    if err := VerifyArtifact(artifact, sig, pubPEM); err != nil {
        t.Errorf("a genuine signature was refused: %v", err)
    }
}

// A hijacked bucket that swaps one byte of the binary must not verify: the
// signature is over the digest of the bytes, so any edit invalidates it.
func TestATamperedArtefactFails(t *testing.T) {
    priv, pubPEM := cutKey()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    sig := signLikeCosign(priv, artifact)
    tampered := []byte("mod-worker-linux-amd64 bytes as they shiP")
    if err := VerifyArtifact(tampered, sig, pubPEM); err == nil {
        t.Fatal("a one-byte-tampered artefact verified")
    }
}

// cosign's .sig file is text; a truncated/garbled one is a corrupt artefact.
func TestABadBase64SignatureFails(t *testing.T) {
    _, pubPEM := cutKey()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    if err := VerifyArtifact(artifact, []byte("not base64!!!"), pubPEM); err == nil {
        t.Fatal("a non-base64 signature verified")
    }
    if err := VerifyArtifact(artifact, []byte(""), pubPEM); err == nil {
        t.Fatal("an absent signature verified")
    }
}

// A signature made by a different key — the shape of a bucket where someone
// re-signed attacker bytes with their own key — must not verify against the
// release key compiled into the worker.
func TestASignatureFromTheWrongKeyFails(t *testing.T) {
    signer, _ := cutKey()
    other, otherPubPEM := cutKey()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    sig := signLikeCosign(signer, artifact)
    if err := VerifyArtifact(artifact, sig, otherPubPEM); err == nil {
        t.Fatal("a signature from an unrelated key verified")
    }
    if err := VerifyArtifact(artifact, signLikeCosign(other, artifact), otherPubPEM); err != nil {
        t.Errorf("sanity: right key/right sig refused: %v", err)
    }
}

// Fail closed: the committed release_pub.pem is a placeholder, so the key
// compiled into a repo build must refuse everything.
func TestThePlaceholderKeyRefusesEverything(t *testing.T) {
    if HasReleaseKey() {
        t.Fatal("the committed placeholder key counts as a release key")
    }
    priv, pubPEM := cutKey()
    artifact := []byte("mod-worker-linux-amd64 bytes as they ship")
    sig := signLikeCosign(priv, artifact)
    if err := VerifyArtifact(artifact, sig, []byte(ReleasePubKeyPEM)); err == nil {
        t.Fatal("the placeholder key verified a real signature")
    }
    if err := VerifyArtifact(artifact, sig, []byte("")); err == nil {
        t.Fatal("an empty key verified a real signature")
    }
    // And the same artefact/signature pair does verify once a real key is
    // compiled in, so the refusal above is about the key, not the machinery.
    if err := VerifyArtifact(artifact, sig, pubPEM); err != nil {
        t.Errorf("a real key refused a real signature: %v", err)
    }
}
