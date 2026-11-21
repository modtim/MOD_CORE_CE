// Package watcher implements the on-prem filesystem watch trigger source
// (add-workflow-triggers §6.2). It polls one or more configured directories and
// POSTs created/modified/deleted events to the API's /triggers/fs-event endpoint,
// which fires matching file_event triggers (source=fs).
//
// Stdlib-only (no fsnotify dependency) — a periodic walk that diffs file
// modification times. Less instantaneous than inotify but dependency-free and
// reliable for folder/file-type watching. Activated only when WATCH_PATHS is set.
package watcher

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type fsEvent struct {
	Path  string `json:"path"`  // the watched root
	Key   string `json:"key"`   // relative path under the root
	Event string `json:"event"` // created|modified|deleted
}

// Config is read from the environment.
type Config struct {
	Paths    []string      // WATCH_PATHS (comma-separated)
	APIURL   string        // WATCH_API_URL (e.g. http://api:80) — POSTs to {APIURL}/triggers/fs-event
	Token    string        // TRIGGER_FS_TOKEN (X-Worker-Token)
	Interval time.Duration // WATCH_INTERVAL_SECONDS (default 10s)
}

func ConfigFromEnv() Config {
	interval := 10 * time.Second
	if s := os.Getenv("WATCH_INTERVAL_SECONDS"); s != "" {
		if d, err := time.ParseDuration(s + "s"); err == nil && d > 0 {
			interval = d
		}
	}
	var paths []string
	for _, p := range strings.Split(os.Getenv("WATCH_PATHS"), ",") {
		if p = strings.TrimSpace(p); p != "" {
			paths = append(paths, p)
		}
	}
	api := os.Getenv("WATCH_API_URL")
	if api == "" {
		api = os.Getenv("API_BASE_URL")
	}
	api = strings.TrimRight(api, "/")
	// API is single-mounted under /v1 only (no unprefixed backward-compat) —
	// see internal/transport/api.go's apiPrefix.
	if api != "" && !strings.HasSuffix(api, "/v1") {
		api += "/v1"
	}
	return Config{Paths: paths, APIURL: api,
		Token: os.Getenv("TRIGGER_FS_TOKEN"), Interval: interval}
}

// Start launches the watcher loop if WATCH_PATHS is configured. No-op otherwise.
func Start(ctx context.Context) {
	cfg := ConfigFromEnv()
	if len(cfg.Paths) == 0 || cfg.APIURL == "" {
		return
	}
	log.Printf("fs-watcher: watching %v every %s -> %s/triggers/fs-event",
		cfg.Paths, cfg.Interval, cfg.APIURL)
	go run(ctx, cfg)
}

func run(ctx context.Context, cfg Config) {
	client := &http.Client{Timeout: 10 * time.Second}
	// seen[root][relkey] = modUnixNano; first pass seeds without firing.
	seen := map[string]map[string]int64{}
	first := true
	t := time.NewTicker(cfg.Interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			for _, root := range cfg.Paths {
				scanRoot(client, cfg, root, seen, first)
			}
			first = false
		}
	}
}

func scanRoot(client *http.Client, cfg Config, root string, seen map[string]map[string]int64, first bool) {
	cur := map[string]int64{}
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info == nil || info.IsDir() {
			return nil
		}
		rel, rerr := filepath.Rel(root, p)
		if rerr != nil {
			return nil
		}
		cur[rel] = info.ModTime().UnixNano()
		return nil
	})
	prev := seen[root]
	if prev == nil {
		prev = map[string]int64{}
	}
	if !first {
		for rel, mt := range cur {
			if old, ok := prev[rel]; !ok {
				post(client, cfg, fsEvent{Path: root, Key: rel, Event: "created"})
			} else if mt != old {
				post(client, cfg, fsEvent{Path: root, Key: rel, Event: "modified"})
			}
		}
		for rel := range prev {
			if _, ok := cur[rel]; !ok {
				post(client, cfg, fsEvent{Path: root, Key: rel, Event: "deleted"})
			}
		}
	}
	seen[root] = cur
}

func post(client *http.Client, cfg Config, ev fsEvent) {
	body, _ := json.Marshal(ev)
	req, err := http.NewRequest(http.MethodPost, cfg.APIURL+"/triggers/fs-event", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if cfg.Token != "" {
		req.Header.Set("X-Worker-Token", cfg.Token)
	}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("fs-watcher: post %s/%s failed: %v", ev.Path, ev.Key, err)
		return
	}
	_ = resp.Body.Close()
}
