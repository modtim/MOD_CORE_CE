# onnx-multi-ep-classifier (corpus synthetic, manifest entry 11)

Tiny deterministic ONNX conv classifier (224x224 RGB -> 2-class softmax).
The ONNX runtime is generated in-process and cached in `/tmp` — no model
download, no network at run time. The execution provider is selected by
`MOD_EP` (cpu | cuda | rocm | openvino) and degrades to CPU when the EP is
unavailable in the image, so a pass verdict on a given accelerator proves
that variant's runtime is installed and used.

Variant Dockerfiles must install the matching runtime package BEFORE
`requirements.txt` (`onnxruntime`, `onnxruntime-gpu`, `onnxruntime-rocm`,
`onnxruntime-openvino`) — those four are mutually exclusive and therefore
live outside the shared requirements file.

I/O: first file in `/data/input` -> `/data/output/result.json` with
`provider`, `top1`, and per-class probabilities (deterministic comparer:
json).
