package selfupdate

// The release signing key is compiled into the worker, not fetched.
//
// WHY EMBEDDED. The worker asks the API for the next binary and for the bytes
// that claim to be its signature. Anything the worker learns over that same
// channel can be forged by whoever owns the channel — a stolen MinIO bucket, a
// rogue proxy, a replayed manifest — so a key downloaded at update time proves
// nothing. The only trust anchor that survives is one that existed before the
// download: the public key baked into the very binary that is doing the
// downloading. It cannot be talked into believing a key it was not built with.
//
// The key pairs the private key the release pipeline signs with
// (`cosign sign-blob --key`), so verification here and signing there are two
// halves of one ceremony, and the worker never needs the private half.

import _ "embed"

// ReleasePubKeyPEM is the release public key compiled into this build, read
// from release_pub.pem at build time. The release pipeline overwrites that
// file with the real `-----BEGIN PUBLIC KEY-----` ECDSA P-256 PEM before
// cutting a release (see README.md in this directory).
//
// One-off builds can override this symbol without touching the file, using the
// same ldflags mechanism the Makefile already uses for main.Version:
//   go build -ldflags "-X github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/selfupdate.ReleasePubKeyPEM=$PEM" ...
//
// The committed file is a placeholder comment, so a build that skips the
// pipeline step compiles a key that HasReleaseKey() rejects and every
// self-update is refused. Fail closed, never "nothing to verify".
//go:embed release_pub.pem
var ReleasePubKeyPEM string

// HasReleaseKey reports whether this build carries a usable release public key.
// It is deliberately a cheap shape check (does the embedded text contain a
// PUBLIC KEY PEM block), not a parse: the point is only to keep the "no key at
// all" case loud and explicit at the top of the update path. Anything that is
// present but malformed is caught properly by VerifyArtifact.
func HasReleaseKey() bool {
    return containsPEMBlock(ReleasePubKeyPEM)
}

func containsPEMBlock(s string) bool {
    return containsSub(s, "-----BEGIN PUBLIC KEY-----")
}

func containsSub(s, sub string) bool {
    if len(sub) > len(s) {
        return false
    }
    for i := 0; i <= len(s)-len(sub); i++ {
        if s[i:i+len(sub)] == sub {
            return true
        }
    }
    return false
}
