package storage

import "testing"

// clearStorageEnv wipes every env var the resolvers read so each case
// starts from a known-empty baseline regardless of the host shell.
func clearStorageEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{
		"STORAGE_BACKEND",
		"VERSITYGW_INTERNAL_URL",
		"VERSITYGW_ROOT_ACCESS_KEY",
		"VERSITYGW_ROOT_SECRET_KEY",
		"MINIO_INTERNAL_URL",
		"MINIO_ROOT_USER",
		"MINIO_ROOT_PASSWORD",
		"S3_INTERNAL_URL",
		"S3_ROOT_USER",
		"S3_ROOT_PASSWORD",
	} {
		t.Setenv(k, "")
	}
}

func TestEndpoint_HealsStaleMinioWhenVersitygwActive(t *testing.T) {
	// The regression: the embedded worker container still carries a stale
	// MINIO_INTERNAL_URL naming the retired `minio` server, but versitygw
	// root creds are present. Resolution must ignore the dead minio host and
	// fall back to the versitygw house default rather than erroring on boot.
	clearStorageEnv(t)
	t.Setenv("MINIO_INTERNAL_URL", "http://minio:9000")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "mod-storage-svc")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "secret")

	host, secure := Endpoint()
	if host != "versitygw:7070" {
		t.Errorf("host = %q; want versitygw:7070 (stale minio must be ignored)", host)
	}
	if secure {
		t.Error("secure = true; want false for http://versitygw:7070")
	}

	ak, sk, ok := RootCredentials()
	if !ok || ak != "mod-storage-svc" || sk != "secret" {
		t.Errorf("RootCredentials = (%q,%q,%v); want versitygw root creds", ak, sk, ok)
	}
}

func TestEndpoint_PrefersExplicitVersitygwURL(t *testing.T) {
	clearStorageEnv(t)
	t.Setenv("STORAGE_BACKEND", "versitygw")
	t.Setenv("VERSITYGW_INTERNAL_URL", "https://s3.internal:7070")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "svc")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "secret")

	host, secure := Endpoint()
	if host != "s3.internal:7070" {
		t.Errorf("host = %q; want s3.internal:7070", host)
	}
	if !secure {
		t.Error("secure = false; want true for https endpoint")
	}
}

func TestEndpoint_VersitygwHonoursNonMinioOverride(t *testing.T) {
	// A MINIO_INTERNAL_URL naming a real (non-minio) host is still honoured
	// even under versitygw — e.g. an operator override or BYO S3.
	clearStorageEnv(t)
	t.Setenv("STORAGE_BACKEND", "versitygw")
	t.Setenv("MINIO_INTERNAL_URL", "http://objects.example:9000")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "svc")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "secret")

	if host, _ := Endpoint(); host != "objects.example:9000" {
		t.Errorf("host = %q; want objects.example:9000", host)
	}
}

func TestEndpoint_LegacyMinioRollback(t *testing.T) {
	// Explicit minio backend (legacy-minio rollback profile): preserve the
	// historical minio endpoint + creds byte-for-byte.
	clearStorageEnv(t)
	t.Setenv("STORAGE_BACKEND", "minio")
	t.Setenv("MINIO_INTERNAL_URL", "http://minio:9000")
	t.Setenv("MINIO_ROOT_USER", "admin")
	t.Setenv("MINIO_ROOT_PASSWORD", "pw")

	host, secure := Endpoint()
	if host != "minio:9000" || secure {
		t.Errorf("host,secure = %q,%v; want minio:9000,false", host, secure)
	}
	ak, sk, ok := RootCredentials()
	if !ok || ak != "admin" || sk != "pw" {
		t.Errorf("RootCredentials = (%q,%q,%v); want admin/pw", ak, sk, ok)
	}
}

func TestRootCredentials_UnconfiguredNoOps(t *testing.T) {
	// No creds anywhere → ok=false so the publisher cleanly skips instead of
	// erroring (the remote-worker / unconfigured case).
	clearStorageEnv(t)
	if _, _, ok := RootCredentials(); ok {
		t.Error("ok = true with no creds set; want false")
	}
}

func TestBackend_DefaultsVersitygw(t *testing.T) {
	clearStorageEnv(t)
	if b := Backend(); b != "versitygw" {
		t.Errorf("Backend() = %q; want versitygw", b)
	}
	t.Setenv("STORAGE_BACKEND", "minio")
	if b := Backend(); b != "minio" {
		t.Errorf("Backend() = %q; want minio", b)
	}
}

func TestEndpoint_S3NameWinsUnderVersitygw(t *testing.T) {
	clearStorageEnv(t)
	t.Setenv("STORAGE_BACKEND", "versitygw")
	t.Setenv("S3_INTERNAL_URL", "http://objects.example:7070")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "svc")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "secret")
	if host, _ := Endpoint(); host != "objects.example:7070" {
		t.Errorf("host = %q; want objects.example:7070 from S3_INTERNAL_URL", host)
	}
}

func TestEndpoint_S3NameHealsStaleMinioHost(t *testing.T) {
	clearStorageEnv(t)
	t.Setenv("S3_INTERNAL_URL", "http://minio:9000")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "svc")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "secret")
	if host, _ := Endpoint(); host != "versitygw:7070" {
		t.Errorf("host = %q; want versitygw:7070 (S3 name must still honour namesRetiredMinio)", host)
	}
}

func TestRootCredentials_S3NameFallback(t *testing.T) {
	clearStorageEnv(t)
	t.Setenv("STORAGE_BACKEND", "minio")
	t.Setenv("S3_ROOT_USER", "admin")
	t.Setenv("MINIO_ROOT_PASSWORD", "pw")
	ak, sk, ok := RootCredentials()
	if !(ok && ak == "admin" && sk == "pw") {
		t.Errorf("RootCredentials = (%q,%q,%v); want S3 name + MINIO fallback mix", ak, sk, ok)
	}
}

func TestEndpoint_NeitherNameUsesHouseDefault(t *testing.T) {
	clearStorageEnv(t)
	t.Setenv("STORAGE_BACKEND", "versitygw")
	t.Setenv("VERSITYGW_ROOT_ACCESS_KEY", "svc")
	t.Setenv("VERSITYGW_ROOT_SECRET_KEY", "secret")
	host, secure := Endpoint()
	if host != "versitygw:7070" || secure {
		t.Errorf("host,secure = %q,%v; want versitygw:7070,false", host, secure)
	}
}
