package executor

// UNDER1S: real-S3 micro-benchmark of the in-process stage/upload/cleanup legs
// against a throwaway gateway the runner created (U1S_S3_ENDPOINT/AK/SK, test
// only). Skipped otherwise. Measures the existing code path unchanged.

import (
	"io"
	"context"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"

	"github.com/modtim/MOD_CORE_CE/CORE/Worker/internal/transport"
)

func TestU1SBenchS3Legs(t *testing.T) {
	ep := os.Getenv("U1S_S3_ENDPOINT")
	if ep == "" {
		t.Skip("set U1S_S3_ENDPOINT")
	}
	mc := transport.MinIOCredentials{AccessKey: os.Getenv("U1S_S3_AK"), SecretKey: os.Getenv("U1S_S3_SK"),
		Endpoint: ep, ExpiresAt: time.Now().Add(time.Hour)}
	creds := &transport.Credentials{MinIO: mc}
	ctx := context.Background()
	cl, err := s3ClientFor(mc)
	if err != nil {
		t.Fatal(err)
	}
	bucket := "u1s-bench"
	if err := cl.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
		t.Fatalf("make bucket: %v", err)
	}
	t.Cleanup(func() {
		for o := range cl.ListObjects(ctx, bucket, minio.ListObjectsOptions{Recursive: true}) {
			cl.RemoveObject(ctx, bucket, o.Key, minio.RemoveObjectOptions{})
		}
		cl.RemoveBucket(ctx, bucket)
	})
	for _, sz := range []int{1024, 100 * 1024, 10 << 20, 100 << 20} {
		var stage, upl, clean, warmPut, coldPut []float64
		n := 20
		if sz >= 100<<20 {
			n = 20
		}
		for i := 0; i < n; i++ {
			dir := t.TempDir()
			b := make([]byte, sz)
			for j := range b {
				b[j] = byte((j*31 + i*7 + sz) % 251)
			}
			os.WriteFile(filepath.Join(dir, "out.bin"), b, 0o600)
			task := &transport.Task{ID: 9000 + i, TenantID: 1, Payload: transport.TaskPayload{NodeKind: "x", MinioBucket: bucket}}
			s := time.Now()
			if _, err := stageNativeOutputsForScan(ctx, task, creds, dir); err != nil {
				t.Fatalf("stage: %v", err)
			}
			stage = append(stage, float64(time.Since(s).Microseconds())/1000)
			s = time.Now()
			if _, err := UploadNativeOutputs(ctx, task, creds, dir, nil); err != nil {
				t.Fatalf("upload: %v", err)
			}
			upl = append(upl, float64(time.Since(s).Microseconds())/1000)
			s = time.Now()
			cleanupNativeStagedOutputs(ctx, task, creds)
			clean = append(clean, float64(time.Since(s).Microseconds())/1000)
			if sz == 1024 {
				c1, _ := s3ClientFor(mc)
				s = time.Now()
				c1.PutObject(ctx, bucket, "cold"+strconv.Itoa(i), bytesReader(b), int64(len(b)), minio.PutObjectOptions{})
				coldPut = append(coldPut, float64(time.Since(s).Microseconds())/1000)
				s = time.Now()
				c1.PutObject(ctx, bucket, "warm"+strconv.Itoa(i), bytesReader(b), int64(len(b)), minio.PutObjectOptions{})
				warmPut = append(warmPut, float64(time.Since(s).Microseconds())/1000)
			}
		}
		t.Logf("size=%d stage_puts %s", sz, u1sStats(stage))
		t.Logf("size=%d durable_upload(put+verifyGET) %s", sz, u1sStats(upl))
		t.Logf("size=%d cleanup %s", sz, u1sStats(clean))
		if sz == 1024 {
			t.Logf("1KB put on FRESH client (conn+bucket-location) %s", u1sStats(coldPut))
			t.Logf("1KB put on WARM client (same client 2nd call)   %s", u1sStats(warmPut))
		}
	}
}

func bytesReader(b []byte) *bytesRd { return &bytesRd{b: b} }

type bytesRd struct {
	b []byte
	o int
}

func (r *bytesRd) Read(p []byte) (int, error) {
	if r.o >= len(r.b) {
		return 0, ioEOF
	}
	n := copy(p, r.b[r.o:])
	r.o += n
	return n, nil
}
var ioEOF = io.EOF
