package selfupdate

// The download half of the ceremony, shared by every artefact that replaces
// a running executable on a machine: the worker binary (selfUpdate) and, once
// the desktop/plugin clients ship their installer + tray-app update path
// (FX.113.p5a), the MOD-Setup MSI, the macOS PKG and the MODTray exe.
//
// WHY IT LIVES HERE AND NOT IN cmd/worker. The worker-binary self-update is
// one call site; the desktop update paths are others. A signature check that
// only exists inside selfUpdate is a check the desktop clients silently don't
// have, and "silently unsigned" is exactly the failure this whole package
// exists to prevent. One function, every caller.
//
// Fail closed in every direction, and never partially: nothing about the
// artefact is trusted before this returns nil, and the caller is the only
// thing allowed to touch the running executable (chmod / exec / rename),
// which it does only after this returns nil.

import (
    "errors"
    "fmt"
    "io"
    "net/http"
    "os"
    "strings"
)

// SignatureURL is where a release artefact's signature lives: the sibling
// object one byte-string longer than the artefact it signs. The release
// pipeline writes it there with `cosign sign-blob` (see README.md), so the
// naming rule is a contract between the two halves of the ceremony, not a
// choice the download channel can make for itself.
func SignatureURL(artifactURL string) string {
    return artifactURL + ".sig"
}

// VerifyDownloadedArtifact checks the bytes that are actually sitting at
// artifactPath against the signature fetched from beside artifactURL, under
// the public key compiled into this build. It returns nil only when that
// signature is the release key's signature of SHA-256(those exact bytes).
//
// headers carries whatever the channel needs to fetch at all (the worker's
// bootstrap token); it is forwarded verbatim and never consulted for
// trust — a signature from a key the build was not compiled with is refused
// whatever the headers say.
func VerifyDownloadedArtifact(artifactURL, artifactPath string, client *http.Client, headers map[string]string) error {
    if !HasReleaseKey() {
        return errors.New("no release key compiled in: refusing an unverified artefact")
    }

    sigB64, err := fetchSignature(artifactURL, client, headers)
    if err != nil {
        return err
    }

    artifact, err := os.ReadFile(artifactPath)
    if err != nil {
        return fmt.Errorf("read artefact for signature check: %w", err)
    }

    if err := VerifyArtifact(artifact, sigB64, []byte(ReleasePubKeyPEM)); err != nil {
        return fmt.Errorf("release signature check: %w", err)
    }
    return nil
}

// fetchSignature pulls the sibling .sig. A non-200 is a refusal, not a
// "no signature published, so nothing to verify" pass: an artefact whose
// signature the pipeline forgot to upload is an unsigned artefact, and an
// unsigned artefact never replaces a running executable.
func fetchSignature(artifactURL string, client *http.Client, headers map[string]string) ([]byte, error) {
    sigURL := SignatureURL(artifactURL)
    req, err := http.NewRequest("GET", sigURL, nil)
    if err != nil {
        return nil, fmt.Errorf("signature request: %w", err)
    }
    for k, v := range headers {
        req.Header.Set(k, v)
    }
    resp, err := client.Do(req)
    if err != nil {
        return nil, fmt.Errorf("signature download: %w", err)
    }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("no signature beside the artefact (HTTP %d) at %s", resp.StatusCode, sigURL)
    }
    body, err := io.ReadAll(resp.Body)
    if err != nil {
        return nil, fmt.Errorf("signature read: %w", err)
    }
    if strings.TrimSpace(string(body)) == "" {
        return nil, errors.New("signature beside the artefact is empty")
    }
    return body, nil
}
