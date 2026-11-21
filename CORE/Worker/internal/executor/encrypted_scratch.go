package executor

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// encrypted_scratch.go — FX.123 (launch-completion-megaplan).
//
// An app-level AES-256-GCM staging layer for ONE task's scratch files.
// Everything lives in the worker process: no new env vars, no tunables, no
// cross-task state, nothing that survives the task.
//
//   - the key is 32 random bytes (crypto/rand) minted per task and held only
//     in this struct's memory. It is never logged, never formatted into an
//     error string, and never written to disk;
//   - every file gets its own random 12-byte nonce, so writing the same
//     plaintext twice leaves different bytes on disk (no ECB-style leaks);
//   - the GCM tag covers the ciphertext AND the file name (as AAD), so a
//     flipped ciphertext byte — or a file moved into another slot — fails
//     to open rather than returning garbage;
//   - Destroy() ends the scratch: it zeroes the key bytes first (so the
//     bytes are useless even if the dir removal then fails), removes the
//     dir, and closes it — later WriteFile/ReadFile calls are refused.
//     It is idempotent, so it is safe to hang on a defer in a cleanup path
//     that can be entered more than once.
//
// The native staging path reaches this through the optional ScratchFactory
// hook in native_staging.go; nil there means the plaintext host scratch dirs
// that path has always used, byte for byte unchanged.

// scratchKeyLen is the AES-256 key size.
const scratchKeyLen = 32

// scratchNonceLen is the GCM nonce size NewGCM expects.
const scratchNonceLen = 12

// scratchHeader tags every sealed file, so a plaintext or foreign file left
// in the dir is rejected outright instead of being decrypted against a
// guessed nonce.
const scratchHeader = "MODSCR1\n"

// ScratchCipher is the optional encrypted-scratch capability the native
// staging path can be handed. EncryptedScratch satisfies it structurally,
// so the staging code depends on the four operations, never on the cipher's
// shape, and a test can hand in a cipher of its own.
type ScratchCipher interface {
	Dir() string
	WriteFile(name string, plaintext []byte) error
	ReadFile(name string) ([]byte, error)
	Destroy() error
}

type EncryptedScratch struct {
	// dir is the task's scratch dir (0700, created by NewEncryptedScratch)
	// — the only thing of this scratch that is ever on disk.
	// key is the per-task AES-256 key, in memory only, zeroed by Destroy.
	// destroyed closes the scratch once Destroy has run.
	// mu guards key/destroyed against a concurrent write/read/destroy.
	dir       string
	key       []byte
	destroyed bool
	mu        sync.Mutex
}

var _ ScratchCipher = (*EncryptedScratch)(nil)

// scratchRandom fills n fresh bytes from crypto/rand.
func scratchRandom(n int) ([]byte, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		// The error text carries no key material — crypto/rand's error is
		// an OS status, and nothing here formats the bytes.
		return nil, fmt.Errorf("executor: EncryptedScratch: random bytes: %w", err)
	}
	return b, nil
}

// zeroScratchKey overwrites key bytes in place, so a leaked pointer to them
// (or a later accidental read) sees zeros.
func zeroScratchKey(key []byte) {
	for i := range key {
		key[i] = 0
	}
}

// ScratchFactoryDefault is the production ScratchFactory shape: the executor's
// own AES-256-GCM scratch handed out through the ScratchCipher interface, so a
// worker can install it (cmd/worker/main.go, behind the
// worker.native_encrypted_scratch platform setting) without naming the
// concrete struct.
func ScratchFactoryDefault(baseDir, taskID string) (ScratchCipher, error) {
	return NewEncryptedScratch(baseDir, taskID)
}

// scratchNameOK rejects any name that is not a plain file inside the scratch
// dir: empty, absolute, a path separator (either kind — the worker runs on
// Windows too, where "\" is the separator), a ".." component, or a NUL.
// The sealed file's name is also its GCM AAD, so a smuggled name could
// otherwise move one task's bytes into another task's slot.
func scratchNameOK(name string) bool {
	if name == "" || strings.ContainsRune(name, 0) {
		// (empty or NUL: refuse, do not fall through)
		return false
	}
	if filepath.IsAbs(name) || strings.ContainsAny(name, `/\`) {
		return false
	}
	for _, part := range strings.Split(name, "/") {
		if part == "" || part == "." || part == ".." {
			// (a bare "." or ".." component, or an empty one: refuse)
			return false
		}
	}
	if filepath.Clean(name) != name {
		// (the cleaned form differs: something smuggled in, refuse)
		return false
	}
	return true
}

// NewEncryptedScratch mints the task's scratch: a 0700 dir under baseDir
// named after the task, plus a fresh 32-byte key that never leaves memory.
// baseDir must already exist (it is the worker's scratch root); the task
// dir itself is created here, exclusively, so a pre-existing dir from an
// earlier run of the same task ID is refused rather than reused.
func NewEncryptedScratch(baseDir, taskID string) (*EncryptedScratch, error) {
	if baseDir == "" || taskID == "" {
		return nil, fmt.Errorf("executor: EncryptedScratch: baseDir and taskID are required")
	}
	if !scratchNameOK(taskID) {
		return nil, fmt.Errorf("executor: EncryptedScratch: task id %q is not a plain dir name", taskID)
	}
	if _, err := os.Stat(baseDir); err != nil {
		return nil, fmt.Errorf("executor: EncryptedScratch: scratch root %s: %w", baseDir, err)
	}
	key, kerr := scratchRandom(scratchKeyLen)
	if kerr != nil {
		return nil, kerr
	}
	// (the key bytes are only ever handed to the cipher below — never
	// logged, never formatted into a string, never written to disk)
	dir := filepath.Join(baseDir, taskID)
	if err := os.Mkdir(dir, 0o700); err != nil {
		zeroScratchKey(key)
		return nil, fmt.Errorf("executor: EncryptedScratch: creating scratch dir %s: %w", dir, err)
	}
	// Mkdir honours the process umask, so the 0700 the task needs is
	// enforced explicitly (same pattern as stacksecrets.go). os.Mkdir itself
	// refuses a name that already exists, so an earlier run's dir is never
	// reused.
	if err := os.Chmod(dir, 0o700); err != nil {
		zeroScratchKey(key)
		return nil, fmt.Errorf("executor: EncryptedScratch: tightening scratch dir %s: %w", dir, err)
	}
	return &EncryptedScratch{dir: dir, key: key}, nil
}

// Dir exposes the scratch dir so the native staging path can hand its
// input/output subdirs to the process it runs.
func (s *EncryptedScratch) Dir() string { return s.dir }

// WriteFile seals plaintext into the scratch dir: a fresh random per-file
// nonce, then AES-256-GCM over the bytes with the file's name as AAD. The
// plaintext never touches disk. Two writes of the same plaintext give
// different bytes because the nonce differs every time.
func (s *EncryptedScratch) WriteFile(name string, plaintext []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.openErr("WriteFile"); err != nil {
		return err
	}
	if !scratchNameOK(name) {
		return fmt.Errorf("executor: EncryptedScratch: refusing file name %q (must be a plain name inside the scratch dir)", name)
	}
	nonce, nerr := scratchRandom(scratchNonceLen)
	if nerr != nil {
		return nerr
	}
	sealed, err := sealScratch(s.key, nonce, name, plaintext)
	if err != nil {
		return err
	}
	return writeScratchFile(filepath.Join(s.dir, name), []byte(scratchHeader), nonce, sealed)
}

// sealScratch runs AES-256-GCM over plaintext with name as AAD. The key is
// read here and nowhere else; nothing about it is returned or logged.
func sealScratch(key, nonce []byte, name string, plaintext []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		// (aes.NewCipher's error names the key LENGTH only — no bytes)
		return nil, fmt.Errorf("executor: EncryptedScratch: cipher: %w", err)
	}
	gcm, gerr := cipher.NewGCM(block)
	if gerr != nil {
		return nil, fmt.Errorf("executor: EncryptedScratch: gcm: %w", gerr)
	}
	// (Seal appends the tag; the name is the AAD, so the same name must be
	// supplied on Open or the file will not authenticate)
	return gcm.Seal(nil, nonce, plaintext, []byte(name)), nil
}

// openErr is the closed-scratch guard: after Destroy the scratch answers
// every operation with an error.
func (s *EncryptedScratch) openErr(op string) error {
	if s.destroyed {
		// (the scratch is gone: refuse, do not touch the zeroed key)
		return fmt.Errorf("executor: EncryptedScratch: %s: scratch %s was destroyed", op, s.dir)
	}
	if s.dir == "" {
		// (a scratch with no dir was never minted: refuse)
		return fmt.Errorf("executor: EncryptedScratch: %s: no scratch dir", op)
	}
	return nil
}

// writeScratchFile writes header||nonce||ciphertext to path, 0600. The path
// is always <0700 scratch dir>/<validated plain name>, so a name can never
// point outside the task's own scratch.
func writeScratchFile(path string, header, nonce, sealed []byte) error {
	if err := os.WriteFile(path, concatScratch(header, nonce, sealed), 0o600); err != nil {
		// (the path is derived from a validated name inside a 0700 dir)
		return fmt.Errorf("executor: EncryptedScratch: writing %s: %w", path, err)
	}
	return nil
}

// concatScratch lays the sealed file out on disk: header||nonce||ciphertext.
func concatScratch(parts ...[]byte) []byte {
	var out []byte
	for _, p := range parts {
		out = append(out, p...)
	}
	return out
}

// ReadFile opens one sealed scratch file. The name is re-supplied as AAD,
// so a file copied onto another name (or a byte flipped inside the
// ciphertext) fails to authenticate rather than returning wrong bytes.
func (s *EncryptedScratch) ReadFile(name string) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.openErr("ReadFile"); err != nil {
		// (destroyed or never-minted: no bytes come back)
		return nil, err
	}
	if !scratchNameOK(name) {
		return nil, fmt.Errorf("executor: EncryptedScratch: refusing file name %q (must be a plain name inside the scratch dir)", name)
	}
	blob, err := os.ReadFile(filepath.Join(s.dir, name))
	if err != nil {
		// (a missing file is a plain error — no key material involved)
		return nil, fmt.Errorf("executor: EncryptedScratch: reading %s: %w", name, err)
	}
	if len(blob) < len(scratchHeader)+scratchNonceLen {
		// (too short to be a sealed file: foreign or truncated, refuse)
		_ = blob
		return nil, fmt.Errorf("executor: EncryptedScratch: %s is not a sealed scratch file", name)
	}
	// (the header is compared on a copy so the plaintext never aliases the
	// sealed buffer)
	head := blob[:len(scratchHeader)]
	if string(head) != scratchHeader {
		// (not written by this layer: plaintext or a foreign file, refuse)
		return nil, fmt.Errorf("executor: EncryptedScratch: %s is not a sealed scratch file", name)
	}
	nonce := blob[len(scratchHeader) : len(scratchHeader)+scratchNonceLen]
	sealed := blob[len(scratchHeader)+scratchNonceLen:]
	return openScratch(s.key, nonce, name, sealed)
}

// openScratch reverses sealScratch: the same name must be supplied as AAD
// or the tag check fails.
func openScratch(key, nonce []byte, name string, sealed []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		// (aes.NewCipher's error names the key LENGTH only — no bytes)
		_ = block
		_ = sealed
		_ = nonce
		return nil, fmt.Errorf("executor: EncryptedScratch: cipher: %w", err)
	}
	gcm, gerr := cipher.NewGCM(block)
	if gerr != nil {
		// (the block is dropped here on purpose: nothing else can use it)
		_ = sealed
		_ = nonce
		return nil, fmt.Errorf("executor: EncryptedScratch: gcm: %w", gerr)
	}
	return gcm.Open(nil, nonce, sealed, []byte(name))
}

// Destroy ends the task's scratch: key bytes zeroed FIRST (so the in-memory
// key is useless even if the removal below fails), then the dir removed.
// Idempotent — a second call is a no-op success — so it is safe on a defer
// in a cleanup path that can be entered more than once. After it, every
// WriteFile/ReadFile call is refused.
func (s *EncryptedScratch) Destroy() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.destroyed {
		// (already gone: idempotent success, nothing to zero again)
		return nil
	}
	// (zero before removing: a failed RemoveAll must not leave a live key
	// paired with a dir that still holds the task's sealed bytes)
	zeroScratchKey(s.key)
	s.destroyed = true
	if s.dir == "" {
		// (nothing was ever created: the key is zeroed, that is all)
		return nil
	}
	if err := os.RemoveAll(s.dir); err != nil {
		// (the key is already zeroed, so the bytes are unreadable even
		// though the dir is still there — report it, do not retry)
		return fmt.Errorf("executor: EncryptedScratch: removing scratch dir %s: %w", s.dir, err)
	}
	return nil
}
