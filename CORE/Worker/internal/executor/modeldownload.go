package executor

import (
	"os"
	"path/filepath"
	"strings"
)

// ModelDownload is one model weight file currently being fetched on this host.
//
// Why this exists: a managed-AI stack on a rung this deployment has never
// served must download its weights before it binds a port, and llama.cpp's
// `-hf` fetch writes NOTHING to the container log while it does. From outside
// the container, a 1.5 GB download and a hung process are indistinguishable —
// which is how a correct first deploy came to be reported to an operator as
// "the stacks exist but are not running", and a QA pass was abandoned on a
// system that was working.
//
// Inferring it from "running but not answering" was the first attempt and is
// not good enough: it says the same words when the process is genuinely wedged.
// The number is on disk the whole time, so report the number.
type ModelDownload struct {
	// Repo as HuggingFace names it, recovered from the cache directory —
	// e.g. "ibm-granite/granite-3.3-2b-instruct-GGUF".
	Repo string `json:"repo"`
	// Bytes on disk so far. The TOTAL is deliberately not computed here: it
	// lives behind a network call the manager can make once and cache, and a
	// worker that guessed it would have to guess again every beat.
	Bytes int64 `json:"bytes"`
}

// hfCachePath is where the managed-AI stacks bind their weight cache. Same
// default as MANAGED_AI_HF_CACHE_PATH on the manager side; overridden together.
func hfCachePath() string {
	if p := strings.TrimSpace(os.Getenv("MANAGED_AI_HF_CACHE_PATH")); p != "" {
		return p
	}
	return "/var/lib/mod/hf-cache"
}

// repoFromCacheDir turns HuggingFace's on-disk directory name back into the
// repo id: "models--ibm-granite--granite-3.3-2b-instruct-GGUF" becomes
// "ibm-granite/granite-3.3-2b-instruct-GGUF".
//
// Only the FIRST separator is a slash — model names legitimately contain
// hyphens, and splitting on every "--" would mangle them.
func repoFromCacheDir(dir string) string {
	name := strings.TrimPrefix(dir, "models--")
	if name == dir {
		return dir
	}
	if i := strings.Index(name, "--"); i >= 0 {
		return name[:i] + "/" + name[i+2:]
	}
	return name
}

// ScanCachedModels reports repos with at least one COMPLETE weight blob.
//
// Needed so the manager can tell "a download is about to start" from "this is
// already on disk and will be serving in seconds". Without it the UI has to
// wait for bytes to appear before it can say anything, which leaves roughly
// half a minute of silence after Apply — the container is being recreated and
// the server has not yet resolved the repo — and that silence is exactly what
// reads as broken.
//
// Same nil/empty contract as ScanModelDownloads: nil is "cannot observe".
func ScanCachedModels() []string {
	hub := filepath.Join(hfCachePath(), "hub")
	entries, err := os.ReadDir(hub)
	if err != nil {
		return nil
	}
	out := []string{}
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "models--") {
			continue
		}
		files, err := os.ReadDir(filepath.Join(hub, e.Name(), "blobs"))
		if err != nil {
			continue
		}
		for _, f := range files {
			n := f.Name()
			// A complete blob is one with neither partial suffix. Its name is
			// the content hash, which is why there is nothing else to match on.
			if strings.HasSuffix(n, ".downloadInProgress") ||
				strings.HasSuffix(n, ".incomplete") {
				continue
			}
			if info, err := f.Info(); err == nil && info.Size() > 0 {
				out = append(out, repoFromCacheDir(e.Name()))
				break
			}
		}
	}
	return out
}

// ScanModelDownloads reports weight downloads in flight on this host.
//
// Returns nil when the cache directory is absent — that is "nothing to say",
// and a worker with no managed-AI stacks must not be mistaken for one whose
// download vanished. An empty (non-nil) slice means "I looked and nothing is
// downloading", which is a different and useful claim: it is what lets the
// manager stop showing a progress bar.
func ScanModelDownloads() []ModelDownload {
	root := hfCachePath()
	hub := filepath.Join(root, "hub")
	entries, err := os.ReadDir(hub)
	if err != nil {
		return nil
	}

	out := []ModelDownload{}
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "models--") {
			continue
		}
		blobs := filepath.Join(hub, e.Name(), "blobs")
		files, err := os.ReadDir(blobs)
		if err != nil {
			continue
		}
		var total int64
		var found bool
		for _, f := range files {
			// llama.cpp names the partial file "<sha256>.downloadInProgress";
			// huggingface_hub uses "<sha256>.incomplete". Both appear in this
			// cache because both tools write to it, and a bar that only
			// understood one would go blank depending which rung was pulling.
			n := f.Name()
			if !strings.HasSuffix(n, ".downloadInProgress") &&
				!strings.HasSuffix(n, ".incomplete") {
				continue
			}
			info, err := f.Info()
			if err != nil {
				continue
			}
			total += info.Size()
			found = true
		}
		if found {
			out = append(out, ModelDownload{
				Repo:  repoFromCacheDir(e.Name()),
				Bytes: total,
			})
		}
	}
	return out
}
