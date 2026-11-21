package executor

// 1900Y1q — after a host/WSL restart the tmpfs secrets dir is gone; the worker
// must flag the project so the manager re-dispatches its deploy. Daemon-free:
// stat/readDir are injected seams, no docker, no real tmpfs.

import (
	"errors"
	"os"
	"testing"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func okStat(string) (os.FileInfo, error) { return nil, nil }
func noStat(string) (os.FileInfo, error) { return nil, errors.New("absent") }

func TestSecretsMissingWith_DirMissingRequestsRedeploy(t *testing.T) {
	readMissing := func(string) ([]os.DirEntry, error) { return nil, errors.New("gone") }
	missing := secretsMissingWith("m", "d", okStat, readMissing)
	if !missing {
		t.Fatal("marker present + secrets dir gone must report missing")
	}
	if !SecretRedeployWanted(0, 2, missing) {
		t.Fatal("stack down with lost secrets must request a redeploy")
	}
	empty := func(string) ([]os.DirEntry, error) { return nil, nil }
	if !secretsMissingWith("m", "d", okStat, empty) {
		t.Fatal("empty secrets dir must count as missing")
	}
}

func TestSecretsMissingWith_DirPresentNoop(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(dir+"/s1", []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	missing := secretsMissingWith("m", dir, okStat, os.ReadDir)
	if missing || SecretRedeployWanted(0, 2, missing) {
		t.Fatal("populated secrets dir must request nothing")
	}
}

func TestSecretsMissingWith_NoMarkerOrHealthyNoop(t *testing.T) {
	gone := func(string) ([]os.DirEntry, error) { return nil, errors.New("gone") }
	if secretsMissingWith("m", "d", noStat, gone) {
		t.Fatal("a stack that never had secrets must not be flagged")
	}
	if SecretRedeployWanted(2, 2, true) {
		t.Fatal("a fully running stack must not be redeployed")
	}
}

// Reboot case: docker auto-creates EMPTY directories at the secret paths when
// a container start finds the bind source missing. The listing is then
// non-empty but holds no file, which still means the secrets were lost.
func TestSecretsMissingWith_OnlyEmptyDirsRequestsRedeploy(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(dir+"/mod_identity_token", 0o755); err != nil {
		t.Fatal(err)
	}
	if !secretsMissingWith("m", dir, okStat, os.ReadDir) {
		t.Fatal("a secrets dir holding only an empty directory must be reported missing")
	}
	if err := os.WriteFile(dir+"/other", []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if secretsMissingWith("m", dir, okStat, os.ReadDir) {
		t.Fatal("a regular file present must not be reported missing")
	}
}

func TestSecretsMissingWith_IdentitySubdirFileCounts(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(dir+"/identity", 0o700); err != nil {
		t.Fatal(err)
	}
	if !secretsMissingWith("m", dir, okStat, os.ReadDir) {
		t.Fatal("empty identity sub-dir only: missing")
	}
	if err := os.WriteFile(dir+"/identity/tok", []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if secretsMissingWith("m", dir, okStat, os.ReadDir) {
		t.Fatal("a file inside identity/ means secrets are present")
	}
}

// materialize must replace a stray empty directory sitting at a secret's name
// with the file (the dir case seen after a reboot), keeping the identity dir.
func TestClearSecretsDirKeepingBinds_ReplacesStrayDirAtSecretName(t *testing.T) {
	dir := t.TempDir()
	stray := dir + "/mod_identity_token"
	if err := os.MkdirAll(stray, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir+"/identity", 0o700); err != nil {
		t.Fatal(err)
	}
	files := []transport.StackSecretFile{{Name: "mod_identity_token", ContentB64: "dG9r"}}
	if err := clearSecretsDirKeepingBinds(dir, files); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if _, err := os.Stat(stray); !os.IsNotExist(err) {
		t.Fatal("stray directory at the secret name must be removed")
	}
	if fi, err := os.Stat(dir + "/identity"); err != nil || !fi.IsDir() {
		t.Fatal("identity dir inode must be kept")
	}
	if _, _, err := writeStackSecrets(dir, files); err != nil {
		t.Fatalf("write: %v", err)
	}
	if fi, err := os.Lstat(stray); err != nil || !fi.Mode().IsRegular() {
		t.Fatal("the secret must now be a regular file")
	}
}
