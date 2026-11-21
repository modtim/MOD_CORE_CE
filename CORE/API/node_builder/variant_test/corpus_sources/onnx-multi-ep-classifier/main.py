#!/usr/bin/env python3
"""onnx-multi-ep-classifier — corpus synthetic node (manifest entry 11).

Tiny 2-class image classifier run through onnxruntime; the execution
provider is chosen from env MOD_EP (cpu|cuda|rocm|openvino, unset = cpu)
and degrades to CPU when the EP is unavailable in the image. The model is a
deterministic conv classifier generated in-process and cached in /tmp — no
download, no network. Output: /data/output/result.json (provider, top-1,
per-class probs).
"""
import glob
import json
import os
import sys
import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper
from PIL import Image

IN_DIR, OUT_DIR = "/data/input", "/data/output"
MODEL_PATH = "/tmp/corpus_classifier.onnx"
CLASSES = ("background", "foreground")
_EPS = {"cuda": "CUDAExecutionProvider", "rocm": "ROCmExecutionProvider", "openvino": "OpenVINOExecutionProvider"}


def pick_provider():
    avail = ort.get_available_providers()
    ep = _EPS.get((os.environ.get("MOD_EP") or "cpu").strip().lower(),
                  "CPUExecutionProvider")
    return ep if ep in avail else "CPUExecutionProvider"


def make_model():
    """Deterministic 2-class conv classifier (Conv->Relu->GAP->MatMul->Softmax)."""
    rng = np.random.default_rng(11)
    w = (rng.standard_normal((2, 3, 3, 3)) * 0.05).astype(np.float32)
    fc = (rng.standard_normal((2, 2)) * 0.1).astype(np.float32)
    inits = [helper.make_tensor("W", TensorProto.FLOAT, list(w.shape), w.tobytes()),
             helper.make_tensor("B", TensorProto.FLOAT, [2], np.zeros(2, np.float32).tobytes()),
             helper.make_tensor("FC", TensorProto.FLOAT, list(fc.shape), fc.tobytes()),
             helper.make_tensor("shape", TensorProto.INT64, [1], np.array([2], np.int64).tobytes())]
    nodes = [helper.make_node("Conv", ["x", "W", "B"], ["y"], kernel_shape=[3, 3], pads=[1, 1, 1, 1]),
             helper.make_node("Relu", ["y"], ["z"]),
             helper.make_node("GlobalAveragePool", ["z"], ["g"]),
             helper.make_node("Reshape", ["g", "shape"], ["v"]),
             helper.make_node("MatMul", ["v", "FC"], ["p"]),
             helper.make_node("Softmax", ["p"], ["out"], axis=1)]
    graph = helper.make_graph(
        nodes, "corpus-classifier",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, 224, 224])],
        inits, outputs=[helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, 2])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    onnx.checker.check_model(model)
    return model


def main():
    if not os.path.exists(MODEL_PATH):
        onnx.save(make_model(), MODEL_PATH)
    srcs = sorted(glob.glob(os.path.join(IN_DIR, "*")))
    if not srcs:
        print("no input file in /data/input", file=sys.stderr)
        return 1
    sess = ort.InferenceSession(MODEL_PATH, providers=[pick_provider()])
    used = sess.get_providers()[0]
    img = np.asarray(Image.open(srcs[0]).convert("RGB").resize((224, 224)), np.float32) / 255.0
    probs = sess.run(["out"], {"x": img.transpose(2, 0, 1)[None]})[0][0]
    result = {"provider": used, "top1": CLASSES[int(probs.argmax())],
              "classes": {c: float(p) for c, p in zip(CLASSES, probs)}}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(f"classified with {used}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
