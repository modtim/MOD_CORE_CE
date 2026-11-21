# internal/selfupdate — signed self-update (FX.61.p3, GSEC R-035b)

`selfUpdate` in `cmd/worker/main.go` downloads a new binary from the API. A hash
alone (GSEC R-035) only proves the bytes match what the API *says* they are; it
says nothing about who cut them. This package adds the signature layer: the
release artefacts are signed out-of-band and the worker carries the matching
public key, so a hijacked API channel — or a hijacked MinIO bucket — cannot make
the worker replace its own binary with attacker-supplied bytes.

## Key format

ECDSA over P-256, signed with `cosign sign-blob --key` (the same artefacts the
hash manifest is built from). The signature file `<artifact>.sig` sits next to
the binary at the same download location and holds base64 of an ASN.1 DER
ECDSA signature over the SHA-256 of the artefact bytes.

## The public key is embedded at build time — the release pipeline overwrites release_pub.pem

`release_pub.pem` in this directory is committed as a PLACEHOLDER (a comment
file, not a PEM block). The release pipeline overwrites it with the real
`-----BEGIN PUBLIC KEY-----` key before building the image, e.g.:

```sh
cosign gen-ecdsa-key --key=release-ecdsa.pem   # once, at key-cut time
# then, before every release build:
cp release-ecdsa.pub release_pub.pem           # into this directory
```

CE release signing: the release pipeline embeds the public key into `internal/selfupdate/release_pub.pem` before the image build (the release pipeline is not shipped in CE).

Alternative for one-off builds without touching the file: override the symbol
with ldflags, same mechanism the Makefile already uses for `main.Version`:

```sh
go build -ldflags "-X github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/selfupdate.ReleasePubKeyPEM=$PEM" ...
```

## Fail closed

With the placeholder compiled in, `VerifyArtifact` returns an error for every
input and `selfUpdate` logs "no release key compiled in" and keeps the running
binary. An empty/placeholder key is never treated as "nothing to verify".

## Stdlib only

`crypto/ecdsa` + `crypto/sha256` + `crypto/x509` (`ParsePKIXPublicKey`) +
`encoding/base64`. No new dependencies, nothing vendored.

## One check, every download path (FX.113.p5c)

`VerifyDownloadedArtifact(artifactURL, artifactPath, client, headers)` is the
whole ceremony — fetch the sibling `.sig`, read the bytes that actually landed
on disk, verify one against the other under the compiled-in key. `cmd/worker`'s
`verifyDownloadSignature` is a thin wrapper over it (log line + the loud
"no release key compiled in" case).

Anything that replaces a running executable on a machine must call this same
function: the worker binary today, and when the desktop/plugin clients ship
their update path (FX.113.p5a: silent MSI/PKG install) the `MOD-Setup-<v>.msi`,
the macOS `.pkg` payload and `MODTray-<v>.exe` too. A signature rule that only
lives inside `selfUpdate` is a rule those clients silently don't have, and
"silently unsigned" is exactly the failure this package exists to prevent.
`SignatureURL()` is the single definition of the sibling-naming contract, so
`MODTray-1.0.71.exe` signs as `MODTray-1.0.71.exe.sig` — the `.exe`/`.msi`
extensions publish.go keeps for the Windows loader/AV stay intact.

No dev-only override exists and none was added: the worker has no dev/debug
mode of its own (only `WORKER_MODE=embedded|remote`, which is not a dev flag),
and a build that skips the signing step already fails closed via the
placeholder key. A dev who wants unsigned updates builds with a real key and
signs; there is no switch that turns the check off.

## What the release pipeline must do (doc note — no CI changed here)

Per artefact, BEFORE the manifest upload (`uploadManifest` must stay last):

1. `cosign sign-blob --key <vault:release/cosign key> --signaturefile
   <artifact>.sig <artifact>` — over the artefact FILE, not the digest
   (the model-digest signing ceremony is a different one; see `CORE/API/services/model_signing.py`).
2. `PutObject` that file to the same bucket under `<object-name>.sig`
   (`SignatureURL` is the naming rule).
3. Embed the matching public key into the build: overwrite
   `internal/selfupdate/release_pub.pem`, or `-X ...ReleasePubKeyPEM=$PEM`
   in the Makefile/Dockerfile ldflags already used for `main.Version`.
4. Do it for every target the Makefile cross-builds — linux/darwin/windows
   worker binaries, and once they ship, the MSI + tray exe.

Skipping any of it is a refused update, never an unsigned one.
