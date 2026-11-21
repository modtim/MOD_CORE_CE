#!/usr/bin/env python3
"""tensorrt-resnet18-engine — corpus synthetic node (manifest entry 20).

Builds a ResNet-18 TensorRT FP16 engine (torch2trt) and runs one inference
pass on the first image in /data/input. NVIDIA-ONLY by nature — no other
accelerator variant exists (manifest: cuda pass, rest incompat). Weights
are fetched ONCE (pinned URL) and cached in /tmp/corpus-tensorrt. Output:
/data/output/result.json (top-1 index + confidence + timing).
"""
import glob
import json
import os
import sys
import time
import numpy as np
import torch

IN_DIR, OUT_DIR = "/data/input", "/data/output"
MODEL_DIR = "/tmp/corpus-tensorrt"
WEIGHTS_URL = "https://download.pytorch.org/models/resnet18-f37072fd.pth"
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


def load_torch_model():
    import torchvision
    w = torch.hub.load_state_dict_from_url(WEIGHTS_URL, map_location="cpu",
                                           cache_dir=os.path.join(MODEL_DIR, "weights"))
    m = torchvision.models.resnet18(weights=None)
    m.load_state_dict(w)
    return m.eval()


def build_engine(path):
    import tensorrt  # noqa: F401 — proves the NVIDIA-only import
    import torch2trt
    model = load_torch_model().cuda().eval()
    sample = torch.zeros(1, 3, 224, 224, device="cuda", dtype=torch.float32)
    trt_model = torch2trt.torch2trt(model, [sample], fp16_mode=True,
                                    max_workspace_size=1 << 30)
    trt_model(sample)  # force engine materialization
    with open(path, "wb") as fh:
        fh.write(trt_model.engine.serialize())
    del trt_model, model


def main():
    os.makedirs(MODEL_DIR, exist_ok=True)
    engine_path = os.path.join(MODEL_DIR, "resnet18.engine")
    if not os.path.exists(engine_path):
        build_engine(engine_path)
    import tensorrt as trt
    with open(engine_path, "rb") as fh:
        engine = trt.Runtime(trt.Logger(trt.Logger.WARNING)).deserialize_cuda_engine(fh.read())
    ctx = engine.create_execution_context()
    srcs = sorted(glob.glob(os.path.join(IN_DIR, "*")))
    if not srcs:
        print("no input file in /data/input", file=sys.stderr)
        return 1
    from PIL import Image
    img = np.asarray(Image.open(srcs[0]).convert("RGB").resize((224, 224)), np.float32) / 255.0
    x = np.ascontiguousarray(((img - MEAN) / STD).transpose(2, 0, 1)[None], np.float32)
    logits = np.empty(list(ctx.get_binding_shape(1)), np.float32)
    t0 = time.time()
    ctx.execute_v2([int(x.ctypes.data), int(logits.ctypes.data)])
    dt_ms = (time.time() - t0) * 1000.0
    e = np.exp(logits[0])
    top = int(e.argmax())
    result = {"top1": top, "top1_confidence": float(e[top] / e.sum()),
              "inference_ms": dt_ms, "engine": "resnet18-fp16"}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(f"inferred top-1={top} in {dt_ms:.2f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
