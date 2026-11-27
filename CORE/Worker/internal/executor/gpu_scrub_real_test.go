package executor

// GPU-SCRUB-REAL-TEST: end-to-end proof of the cross-tenant VRAM scrub on a
// REAL NVIDIA card with the REAL executor scrub path (scrubForTask ->
// scrubCard -> VramScrubber.Scrub -> launchScrubHelper -> docker + nvidia
// runtime). Skipped unless SCRUBTEST_REAL=1 (it needs docker, a GPU and the
// scrubtest-helper-* images). Tenant workloads are plain containers started
// by the test; the executor decides and runs the scrub between them exactly
// as Run does at its "Step 3.9".

import (
	"os"
	"testing"
)

// Pattern word 0xA55AC0DE written little-endian: de c0 5a a5.
const realPatternHex = "dec05aa5"

// Tenant A: fill 12 x 256 MiB device allocations with the pattern, read one
// back to prove the probe can see it, free everything, exit.
const realWriterScript = `import ctypes,sys
L=ctypes.CDLL("libcuda.so.1")
def ck(r,w):
    if r!=0:
        sys.stderr.write("%s rc=%d\n"%(w,r)); sys.exit(1)
ck(L.cuInit(0),"cuInit")
d=ctypes.c_int(0); ck(L.cuDeviceGet(ctypes.byref(d),0),"dev")
ctx=ctypes.c_void_p(); ck(L.cuCtxCreate_v2(ctypes.byref(ctx),0,d),"ctx")
PAT=0xA55AC0DE
SZ=256<<20
ps=[]
for i in range(12):
    p=ctypes.c_uint64(0); ck(L.cuMemAlloc_v2(ctypes.byref(p),ctypes.c_size_t(SZ)),"alloc")
    ck(L.cuMemsetD32_v2(p,ctypes.c_uint(PAT),ctypes.c_size_t(SZ//4)),"memset")
    ps.append(p)
ck(L.cuCtxSynchronize(),"sync")
buf=(ctypes.c_ubyte*(1<<20))()
ck(L.cuMemcpyDtoH_v2(buf,ps[0],ctypes.c_size_t(1<<20)),"dtoh")
hits=bytes(buf).count(bytes.fromhex("dec05aa5"))
print("WRITER wrote_bytes=%d readback_pattern_words_in_1MiB=%d"%(SZ*12,hits))
for p in ps: ck(L.cuMemFree_v2(p),"free")
ck(L.cuCtxDestroy_v2(ctx),"ctxd")
`

// Tenant B: uninitialised-memory probe. For several allocation sizes: cuMemAlloc
// (no memset), cuMemcpyDtoH in 64 MiB chunks, count pattern words. Prints only
// counts (never memory contents).
const realProbeScript = `import ctypes,sys
L=ctypes.CDLL("libcuda.so.1")
def ck(r,w):
    if r!=0:
        sys.stderr.write("%s rc=%d\n"%(w,r)); sys.exit(1)
ck(L.cuInit(0),"cuInit")
d=ctypes.c_int(0); ck(L.cuDeviceGet(ctypes.byref(d),0),"dev")
ctx=ctypes.c_void_p(); ck(L.cuCtxCreate_v2(ctypes.byref(ctx),0,d),"ctx")
pat=bytes.fromhex("dec05aa5")
CH=64<<20
buf=(ctypes.c_ubyte*CH)()
tot=0
for sz in (4096,1<<20,16<<20,256<<20,1<<30,3<<30):
    p=ctypes.c_uint64(0)
    r=L.cuMemAlloc_v2(ctypes.byref(p),ctypes.c_size_t(sz))
    if r!=0:
        print("PROBE size=%d alloc_rc=%d"%(sz,r)); continue
    hits=0; nz=0; off=0
    while off<sz:
        n=min(CH,sz-off)
        ck(L.cuMemcpyDtoH_v2(buf,ctypes.c_uint64(p.value+off),ctypes.c_size_t(n)),"dtoh")
        b=bytes(buf)[:n]
        hits+=b.count(pat); nz+=n-b.count(0)
        off+=n
    print("PROBE size=%d pattern_words=%d nonzero_bytes=%d"%(sz,hits,nz))
    tot+=hits
    ck(L.cuMemFree_v2(p),"free")
print("PROBE_TOTAL pattern_words=%d"%tot)
`

func realEnabled(t *testing.T) {
	if os.Getenv("SCRUBTEST_REAL") != "1" {
		t.Skip("SCRUBTEST_REAL!=1: real-GPU scrub proof needs docker + an NVIDIA card")
	}
}
