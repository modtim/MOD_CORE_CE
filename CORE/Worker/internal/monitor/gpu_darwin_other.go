//go:build !darwin

package monitor

// MAC.1.s14 — non-darwin stub for the Metal sampler. gpu.go (platform-neutral,
// it feeds the CUDA/ROCm/Intel paths everywhere else) references the symbol,
// so every other platform resolves it to "no Metal here": an empty snapshot
// has no Type, which SampleGPU turns into its pre-existing "none" fall-through
// — a linux box whose profile somehow carries a Metal tag stays byte-for-byte
// identical to what it reported before this change.
func sampleDarwinMetal() GPUSnapshot {
 return GPUSnapshot{}
}
