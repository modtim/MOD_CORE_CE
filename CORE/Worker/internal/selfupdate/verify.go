package selfupdate

// VerifyArtifact checks a cosign sign-blob signature against an artefact.
//
// The release pipeline signs every binary with `cosign sign-blob --key`, which
// writes base64 of an ASN.1 DER ECDSA (P-256) signature over the SHA-256 of the
// artefact bytes, into a sibling file <artifact>.sig. This function is the
// worker's half of that ceremony: it recomputes the digest from the bytes it
// actually downloaded and asks the embedded public key whether the signature
// presented by the download channel is one the release key made for THAT
// digest. A tampered artefact, a truncated/garbled signature, or a signature
// made by a different key all fail; nothing about the artefact is trusted
// before this returns nil.
//
// Stdlib only: crypto/ecdsa, crypto/sha256, crypto/x509, encoding/base64.

import (
    "crypto/ecdsa"
    "crypto/sha256"
    "crypto/x509"
    "encoding/base64"
    "encoding/pem"
    "errors"
    "fmt"
    "strings"
)

// VerifyArtifact returns nil only when pubPEM holds a parseable ECDSA P-256
// public key, sigB64 decodes to a DER ECDSA signature, and that signature is
// valid for SHA-256(artifact) under that key. Every other combination is an
// error, including an empty or placeholder pubPEM — the caller must treat a
// missing key as "refuse", never as "nothing to verify".
func VerifyArtifact(artifact []byte, sigB64 []byte, pubPEM []byte) error {
    if !hasPEMBlock(string(pubPEM)) {
        return errors.New("no release key compiled in: refusing self-update without an embedded release public key")
    }

    pub, err := parsePublicKey(pubPEM)
    if err != nil {
        return err
    }

    sig, err := decodeSignature(sigB64)
    if err != nil {
        return err
    }

    digest := sha256.Sum256(artifact)
    if !ecdsa.VerifyASN1(pub, digest[:], sig) {
        return errors.New("release signature invalid for this artefact")
    }
    return nil
}

// parsePublicKey turns the embedded PEM text into an ecdsa.PublicKey,
// rejecting anything that is not an ECDSA key on a 256-bit curve (the curve
// cosign signs with). A key that parses but is the wrong type is a release-
// pipeline mistake, and must not be silently accepted.
func parsePublicKey(pubPEM []byte) (*ecdsa.PublicKey, error) {
    block, _ := pem.Decode(pubPEM)
    if block == nil {
        return nil, errors.New("release key: no PEM block found")
    }
    any, err := x509.ParsePKIXPublicKey(block.Bytes)
    if err != nil {
        return nil, fmt.Errorf("release key: %w", err)
    }
    pub, ok := any.(*ecdsa.PublicKey)
    if !ok {
        return nil, errors.New("release key: not an ECDSA public key")
    }
    if pub.Curve == nil || pub.Curve.Params().BitSize != 256 {
        return nil, errors.New("release key: not an ECDSA P-256 key")
    }
    return pub, nil
}

// decodeSignature undoes cosign's base64 wrapper. Strict decoding: a
// signature with stray characters or an unpadded tail is a corrupt artefact,
// not something to salvage.
func decodeSignature(sigB64 []byte) ([]byte, error) {
    trimmed := strings.TrimSpace(string(sigB64))
    if trimmed == "" {
        return nil, errors.New("no signature supplied for this artefact")
    }
    sig, err := base64.StdEncoding.DecodeString(trimmed)
    if err != nil {
        return nil, fmt.Errorf("signature decode: %w", err)
    }
    if len(sig) == 0 {
        return nil, errors.New("signature is empty")
    }
    return sig, nil
}

// hasPEMBlock is the same cheap shape check key.go uses for the embedded key,
// applied here to the pubPEM argument so VerifyArtifact can be called with any
// candidate key (tests do exactly that) and still fail closed on a placeholder.
// containsSub itself lives in key.go, same package.
func hasPEMBlock(s string) bool {
    return containsSub(s, "-----BEGIN PUBLIC KEY-----")
}
