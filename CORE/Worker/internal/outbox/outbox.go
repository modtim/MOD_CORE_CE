// Package outbox holds task results the manager could not be told about yet.
//
// A worker that finishes a task while the manager is unreachable used to log
// the failed report and drop the result on the floor. The work itself was not
// lost — uploadSidecar puts the outputs in MinIO BEFORE the report is sent, so
// the expensive artifact is already durable — but the small piece of metadata
// that says "this task finished, here is where its outputs are" went nowhere.
// The manager eventually requeued the task and the whole job ran again.
//
// That is what this fixes, and it is cheap precisely because the outputs are
// already safe: an entry is a few hundred bytes of JSON, not gigabytes.
//
// Entries are written to disk so they survive a worker restart. If the
// configured directory is not on a persistent mount the queue is still correct,
// just in-memory-lifetime — which already covers the common case, a worker that
// stays up while the manager restarts.
package outbox

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// syncFile is the fsync seam. Add needs a real fsync before the rename that
// commits an entry (row 4: without it a power loss can leave the renamed
// .json zero-length, and List skips a zero-length file silently, so the
// queued result vanishes). Tests override this variable to count the calls.
var syncFile = func(f *os.File) error { return f.Sync() }

// Kind identifies which report an entry replays.
type Kind string

const (
	KindComplete Kind = "complete"
	KindFailed   Kind = "failed"
)

// Entry is one undelivered report.
type Entry struct {
	Kind      Kind            `json:"kind"`
	TaskID    int             `json:"task_id"`
	Payload   json.RawMessage `json:"payload"`
	CreatedAt time.Time       `json:"created_at"`
	Attempts  int             `json:"attempts"`

	// file is where this entry lives. Not serialized — it is the identity
	// used to update or delete the entry, and is set on read.
	file string
}

// Age reports how long this result has been waiting to be delivered.
func (e Entry) Age() time.Duration { return time.Since(e.CreatedAt) }

// Store is a durable FIFO of undelivered reports.
type Store struct {
	dir string
	mu  sync.Mutex
	log *slog.Logger
}

// defaultLog keeps the store usable (and its warnings silent-but-present)
// when nobody wired it to the worker's logger.
var defaultLog = slog.New(slog.NewTextHandler(io.Discard, nil))

// New opens (creating if needed) an outbox rooted at dir.
func New(dir string) (*Store, error) {
	if strings.TrimSpace(dir) == "" {
		return nil, fmt.Errorf("outbox: empty directory")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("outbox: create %s: %w", dir, err)
	}
	s := &Store{dir: dir, log: defaultLog}
	// A crash between the temp write and the rename in Add/Bump leaves a
	// *.json.tmp that List ignores and nothing else ever cleans: it would
	// sit in the dir forever. Sweep those leftovers on open — only files
	// matching this dir's own temp pattern, never anything else.
	s.sweepTempLeftovers()
	return s, nil
}

// sweepTempLeftovers deletes *.json.tmp files this outbox left behind. Best-
// effort: a leftover is noise, not a reason to refuse the queue.
func (s *Store) sweepTempLeftovers() {
	names, err := os.ReadDir(s.dir)
	if err != nil {
		return
	}
	for _, n := range names {
		if n.IsDir() || !strings.HasSuffix(n.Name(), ".json.tmp") {
			continue
		}
		_ = os.Remove(filepath.Join(s.dir, n.Name()))
	}
}

// SetLog points the store's warnings at the worker's logger.
func (s *Store) SetLog(log *slog.Logger) {
	if log != nil {
		s.log = log
	}
}

// Dir returns the backing directory.
func (s *Store) Dir() string { return s.dir }

// Add appends an entry. Written to a temp file and renamed so a crash
// mid-write can never leave a half-parsed entry behind.
func (s *Store) Add(kind Kind, taskID int, payload any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("outbox: marshal payload: %w", err)
	}
	e := Entry{
		Kind:      kind,
		TaskID:    taskID,
		Payload:   raw,
		CreatedAt: time.Now().UTC(),
	}
	body, err := json.Marshal(e)
	if err != nil {
		return fmt.Errorf("outbox: marshal entry: %w", err)
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Nanosecond prefix keeps readback in submission order.
	name := fmt.Sprintf("%d-%d-%s.json", time.Now().UTC().UnixNano(), taskID, kind)
	final := filepath.Join(s.dir, name)
	tmp := final + ".tmp"
	if err := writeSynced(tmp, body); err != nil {
		return fmt.Errorf("outbox: write: %w", err)
	}
	if err := os.Rename(tmp, final); err != nil {
		os.Remove(tmp)
		return fmt.Errorf("outbox: commit: %w", err)
	}
	// The rename itself has to survive a power loss too: the entry only
	// exists once the directory remembers it.
	if err := syncDir(s.dir); err != nil {
		s.log.Warn("outbox: entry committed without a dir fsync",
			"file", final, "error", err)
	}
	return nil
}

// writeSynced writes body to path and fsyncs it before the caller renames it
// into place. Without the fsync a power loss can leave the renamed .json
// zero-length on disk, and List skips a zero-length file silently, so the
// queued result would be gone with nothing to show for it.
func writeSynced(path string, body []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY | os.O_CREATE | os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(body); err != nil {
		f.Close()
		return err
	}
	if err := syncFile(f); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// syncDir fsyncs the outbox directory so the rename that committed an entry
// is durable as well as the bytes.
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return syncFile(d)
}

// List returns pending entries oldest first. Unreadable or corrupt files are
// skipped rather than failing the whole drain — one bad entry must not wedge
// delivery of every other result. A bad entry is QUARANTINED (renamed to
// *.bad) the first time it is seen, so it stops being re-read on every List
// forever and the dir does not silently accumulate unparseable files.
func (s *Store) List() ([]Entry, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	names, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, fmt.Errorf("outbox: read dir: %w", err)
	}
	var files []string
	for _, n := range names {
		if n.IsDir() || !strings.HasSuffix(n.Name(), ".json") {
			continue
		}
		files = append(files, n.Name())
	}
	sort.Strings(files)

	out := make([]Entry, 0, len(files))
	for _, f := range files {
		full := filepath.Join(s.dir, f)
		b, err := os.ReadFile(full)
		if err != nil {
			quarantine(s, full, "unreadable")
			continue
		}
		var e Entry
		if err := json.Unmarshal(b, &e); err != nil {
			quarantine(s, full, "undecodable")
			continue
		}
		e.file = full
		out = append(out, e)
	}
	return out, nil
}

// quarantine moves one unparseable entry aside (to *.bad) and logs it. Called
// from List, which already holds the store lock, so this takes no lock of its
// own. Best-effort: a failed rename leaves the entry skipped, which is the
// old behaviour, not a crash.
func quarantine(s *Store, path, why string) {
	if err := os.Rename(path, path + ".bad"); err != nil {
		s.log.Warn("outbox: could not quarantine bad entry", "file", path,
			"reason", why, "error", err)
		return
	}
	s.log.Warn("outbox: quarantined unreadable entry", "file", path + ".bad",
		"reason", why)
}

// Remove deletes a delivered (or permanently rejected) entry.
func (s *Store) Remove(e Entry) error {
	if e.file == "" {
		return fmt.Errorf("outbox: entry has no backing file")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.Remove(e.file); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("outbox: remove: %w", err)
	}
	return nil
}

// Bump records another failed delivery attempt. Best-effort: if the counter
// cannot be persisted the entry still stays queued, which is the behaviour
// that matters.
func (s *Store) Bump(e Entry) {
	if e.file == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	e.Attempts++
	body, err := json.Marshal(e)
	if err != nil {
		return
	}
	tmp := e.file + ".tmp"
	if err := os.WriteFile(tmp, body, 0o600); err != nil {
		return
	}
	if err := os.Rename(tmp, e.file); err != nil {
		os.Remove(tmp)
	}
}

// Len reports how many results are waiting.
func (s *Store) Len() int {
	entries, err := s.List()
	if err != nil {
		return 0
	}
	return len(entries)
}
