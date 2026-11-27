package releases

import (
	"context"
	"os"
	"strings"
	"testing"
)

// ALL-ONE-VERSION-1-1-0: the whole product moves to 1.1.0 and CORE/Worker/VERSION
// ships "v1.1.0". The installed fleet runs "v1.0.84"; the rolling publisher
// must see v1.1.0 as strictly NEWER (a numeric compare, never a string one:
// "v1.1.0" < "v1.0.84" as text would be wrong the other way round for 1.10).
func TestPublishIfNewer_V110IsNewerThanV1084(t *testing.T) {
	cases := []struct {
		existing, next string
		wantPublish    bool
	}{
		{"v1.0.84", "v1.1.0", true},
		{"1.0.84", "v1.1.0", true},
		{"v1.0.84", "1.1.0", true},
		{"v1.0.100", "v1.1.0", true},  // 3-digit patch still below 1.1.0
		{"v1.1.0", "v1.1.0", false},   // equal: skip
		{"v1.1.0", "v1.0.84", false},  // downgrade refused
	}
	for _, tc := range cases {
		t.Run(tc.existing+"->"+tc.next, func(t *testing.T) {
			store, host := startFakeStore(t, map[string][]byte{
				manifestObjectName: manifestBytes(tc.existing),
			})
			pointAtFakeStore(t, host)
			_, published, _ := PublishIfNewer(context.Background(), oneBinary(t), tc.next, newTestLogger())
			if published != tc.wantPublish {
				t.Errorf("%s -> %s: published=%v want %v", tc.existing, tc.next, published, tc.wantPublish)
			}
			if !tc.wantPublish && store.putCount() != 0 {
				t.Errorf("skip/refuse wrote %d objects", store.putCount())
			}
		})
	}
}

func TestCompareSemver_V110(t *testing.T) {
	a, ok1 := parseSemver("v1.1.0")
	b, ok2 := parseSemver("v1.0.84")
	if !ok1 || !ok2 {
		t.Fatalf("parse failed: %v %v", ok1, ok2)
	}
	if compareSemver(a, b) != 1 || compareSemver(b, a) != -1 {
		t.Errorf("v1.1.0 must compare newer than v1.0.84")
	}
}

// The committed VERSION file itself must parse (the release pipeline fails
// closed on anything else) and must be at least 1.1.0.
func TestCommittedWorkerVersionParsesAndIsAtLeast110(t *testing.T) {
	raw, err := os.ReadFile("../../VERSION")
	if err != nil {
		t.Skipf("VERSION not reachable from this tree: %v", err)
	}
	got, ok := parseSemver(strings.TrimSpace(string(raw)))
	if !ok {
		t.Fatalf("CORE/Worker/VERSION %q does not parse", raw)
	}
	floor, _ := parseSemver("v1.1.0")
	if compareSemver(got, floor) < 0 {
		t.Errorf("CORE/Worker/VERSION %q is below v1.1.0", raw)
	}
}
