# tensorrt-resnet18-engine (corpus synthetic, manifest entry 20)

ResNet-18 TensorRT FP16 engine build + inference via torch2trt.
NVIDIA-ONLY BY NATURE: tensorrt requires an NVIDIA GPU, so this node has no
non-NVIDIA variants at all — the manifest expects `incompat` (recorded, not
built) on rocm/intel/npu/cpu and `pass` on cuda. `node_config.json`
declares the incompatible variants for the record.

`main.py` fetches the pinned resnet18 weights once (cached in
`/tmp/corpus-tensorrt` — the only network access, cold cache only), builds
or loads the FP16 engine, and infers the first image in `/data/input`,
writing `/data/output/result.json` (top-1 index + confidence + timing; json
comparer).
