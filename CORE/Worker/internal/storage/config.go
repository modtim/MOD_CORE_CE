// Package storage centralises the worker's object-storage wiring so the
// endpoint, backend selection, and control-plane credentials are resolved in
// one place rather than parsed inline at each call site.
//
// Gate 0 (remove-minio-versitygw): the two SDK clients — release publishing
// (internal/releases) and stack snapshots (internal/executor) — previously each
// parsed MINIO_INTERNAL_URL and read MINIO_ROOT_USER/PASSWORD themselves. They
// now share these helpers. The AGPL `minio` server has since been retired in
// favour of the versitygw S3 gateway, so these helpers resolve the versitygw
// endpoint + credentials by default, matching the API's selector in
// CORE/API/utils/storage.py. Per-task tenant credentials are a separate path
// (transport/executor) retired off MinIO STS in Gate 2 — this package is only
// for the control-plane clients.
package storage

import (
	"os"
	"strings"
)

// Backend returns the active storage backend id. House default is "versitygw"
// (parity with the API's STORAGE_BACKEND selector in CORE/API/utils/storage.py);
// "minio" remains selectable for the legacy-minio rollback profile. Both
// backends speak plain S3.
func Backend() string {
	b := strings.ToLower(strings.TrimSpace(os.Getenv("STORAGE_BACKEND")))
	if b == "" {
		return "versitygw"
	}
	return b
}

// usingVersitygw reports whether the control-plane clients should target the
// versitygw gateway rather than the legacy minio server. True when
// STORAGE_BACKEND selects it explicitly, or when STORAGE_BACKEND is unset but
// versitygw root credentials are present — the shape of the embedded worker's
// environment on the control server.
func usingVersitygw() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("STORAGE_BACKEND"))) {
	case "versitygw":
		return true
	case "minio":
		return false
	}
	// Unset: infer from whether versitygw root creds are configured.
	return os.Getenv("VERSITYGW_ROOT_ACCESS_KEY") != "" && os.Getenv("VERSITYGW_ROOT_SECRET_KEY") != ""
}

// Endpoint resolves the in-cluster object-storage endpoint for the worker's
// control-plane clients (release publishing + stack snapshots), returning the
// scheme-stripped host and whether TLS is in use.
//
// The AGPL `minio` server was retired in favour of versitygw. When versitygw is
// the active backend the endpoint resolves from VERSITYGW_INTERNAL_URL (house
// default http://versitygw:7070), matching CORE/API/utils/storage.py. A
// MINIO_INTERNAL_URL still naming the retired `minio` host is ignored in that
// case — it no longer resolves on the container network and was the source of a
// boot-time "lookup minio: no such host" release-publish failure. A
// MINIO_INTERNAL_URL naming any other host is still honoured (operator override
// / BYO S3), as is the legacy http://minio:9000 default when the minio backend
// is explicitly selected for rollback.
func Endpoint() (host string, secure bool) {
	var endpoint string
	if usingVersitygw() {
		endpoint = strings.TrimSpace(os.Getenv("VERSITYGW_INTERNAL_URL"))
		if endpoint == "" {
			legacy := os.Getenv("S3_INTERNAL_URL")
			if legacy == "" {
				legacy = os.Getenv("MINIO_INTERNAL_URL")
			}
			if m := strings.TrimSpace(legacy); m != "" && !namesRetiredMinio(m) {
				endpoint = m
			}
		}
		if endpoint == "" {
			endpoint = "http://versitygw:7070"
		}
	} else {
		legacy := os.Getenv("S3_INTERNAL_URL")
		if legacy == "" {
			legacy = os.Getenv("MINIO_INTERNAL_URL")
		}
		endpoint = strings.TrimSpace(legacy)
		if endpoint == "" {
			endpoint = "http://minio:9000"
		}
	}
	secure = strings.HasPrefix(endpoint, "https://")
	host = strings.TrimPrefix(strings.TrimPrefix(endpoint, "http://"), "https://")
	return host, secure
}

// namesRetiredMinio reports whether an endpoint URL points at the retired
// `minio` server host (any scheme, port, or path).
func namesRetiredMinio(endpoint string) bool {
	h := strings.TrimPrefix(strings.TrimPrefix(endpoint, "http://"), "https://")
	if i := strings.IndexByte(h, '/'); i >= 0 {
		h = h[:i]
	}
	if i := strings.LastIndexByte(h, ':'); i >= 0 {
		h = h[:i]
	}
	return h == "minio"
}

// RootCredentials returns the control-plane storage credentials. When versitygw
// is active they come from VERSITYGW_ROOT_ACCESS_KEY / VERSITYGW_ROOT_SECRET_KEY
// (the versitygw root account, which is the API's storage service account);
// otherwise from the legacy MINIO_ROOT_USER / MINIO_ROOT_PASSWORD. ok is false
// when the selected pair is unset — only the embedded worker on the control
// server holds them in v1, so a remote worker (or an unconfigured backend)
// cleanly no-ops rather than erroring.
func RootCredentials() (accessKey, secretKey string, ok bool) {
	if usingVersitygw() {
		accessKey = os.Getenv("VERSITYGW_ROOT_ACCESS_KEY")
		secretKey = os.Getenv("VERSITYGW_ROOT_SECRET_KEY")
	} else {
		accessKey = os.Getenv("S3_ROOT_USER")
		if accessKey == "" {
			accessKey = os.Getenv("MINIO_ROOT_USER")
		}
		secretKey = os.Getenv("S3_ROOT_PASSWORD")
		if secretKey == "" {
			secretKey = os.Getenv("MINIO_ROOT_PASSWORD")
		}
	}
	return accessKey, secretKey, accessKey != "" && secretKey != ""
}
