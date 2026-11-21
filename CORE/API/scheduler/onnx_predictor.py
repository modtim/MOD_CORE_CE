"""
ONNX Scheduler Predictor — batched inference for the overlap scheduler.

Supports cold (zero-shot) and warm (partial sequence) prediction modes.
Runs via asyncio.to_thread() to avoid blocking async handlers.
"""

import json
from product_names import themed_logger
import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import numpy as np

logger = themed_logger(__name__, 'scheduler')

PATTERN_NAMES = ["plateau", "linear_ramp", "step_function", "sawtooth", "declining", "unknown"]


@dataclass
class PredictionResult:
    processing_duration_s: float
    upload_duration_s: float
    download_duration_s: float
    pattern_type: str
    pattern_confidence: float
    peak_ram_mb: float
    peak_vram_mb: float
    peak_cpu_pct: float
    peak_net_mbps: float
    model_version: int
    inference_mode: str

    def to_dict(self):
        return asdict(self)


class ONNXSchedulerPredictor:
    """Batched ONNX inference for task scheduling predictions."""

    def __init__(self, model_path: str, normalization_path: str = None, metadata: dict = None):
        import onnxruntime as ort
        from utils.model_store import _cpu_session_options

        self.session = ort.InferenceSession(
            model_path,
            sess_options=_cpu_session_options(),
            providers=["CPUExecutionProvider"],
        )
        self.metadata = metadata or {}
        self.version = self.metadata.get("version", 0)

        self.norm = {}
        self.category_maps = {}
        if normalization_path:
            try:
                with open(normalization_path) as f:
                    norm_data = json.load(f)
                self.norm = norm_data.get("numeric", {})
                self.category_maps = norm_data.get("categories", {})
            except Exception as e:
                logger.warning("Could not load normalization: %s", e)

        config = self.metadata.get("config", {}).get("model", {})
        self.cat_features = config.get("categorical_features", [
            "node_name_id", "process_type_id", "size_tier_id",
            "hardware_fingerprint_id", "gpu_model_id",
        ])
        self.static_num_features = config.get("static_numeric_features", [
            "cpu_cores", "ram_gb", "gpu_compute_score", "cpu_multi_score",
            "cpu_single_score", "io_seq_score", "io_random_score",
            "memory_bw_score", "minio_download_mbps", "minio_upload_mbps",
        ])
        self.ts_features = config.get("time_series_features", [])
        self.known_features = config.get("known_time_features", [])
        self.max_seq_len = self.metadata.get("config", {}).get("data", {}).get("max_seq_len", 128)

        n_ts = len(self.ts_features) or 14
        n_known = len(self.known_features) or 2
        self._n_ts = n_ts
        self._n_known = n_known

        logger.info("ONNX predictor loaded: v%d, %d cat + %d num + %d ts + %d known features",
                     self.version, len(self.cat_features), len(self.static_num_features), n_ts, n_known)

    def _encode_categorical(self, name: str, value: str) -> int:
        base_name = name.replace("_id", "")
        cat_map = self.category_maps.get(base_name, {})
        return cat_map.get(value, cat_map.get(str(value), -1))

    def _normalize(self, name: str, value: float) -> float:
        params = self.norm.get(name, {})
        mean = params.get("mean", 0)
        std = params.get("std", 1)
        if std == 0:
            std = 1
        return (value - mean) / std

    def _build_inputs(self, items: List[dict], mode: str = "cold") -> dict:
        batch_size = len(items)
        n_cat = len(self.cat_features)
        n_num = len(self.static_num_features)

        static_cat = np.zeros((batch_size, n_cat), dtype=np.int64)
        static_num = np.zeros((batch_size, n_num), dtype=np.float32)
        time_series = np.zeros((batch_size, self.max_seq_len, self._n_ts), dtype=np.float32)
        known_time = np.zeros((batch_size, self.max_seq_len, self._n_known), dtype=np.float32)
        mask = np.zeros((batch_size, self.max_seq_len), dtype=np.bool_)

        for i, item in enumerate(items):
            for j, feat in enumerate(self.cat_features):
                raw_name = feat.replace("_id", "")
                val = item.get(raw_name, item.get(feat, ""))
                static_cat[i, j] = max(0, self._encode_categorical(feat, val))

            for j, feat in enumerate(self.static_num_features):
                val = float(item.get(feat, 0) or 0)
                static_num[i, j] = self._normalize(feat, val)

            samples = item.get("samples", [])
            if mode == "warm" and samples:
                seq_len = min(len(samples), self.max_seq_len)
                mask[i, :seq_len] = True
                for t, sample in enumerate(samples[:seq_len]):
                    for f, feat in enumerate(self.ts_features):
                        if f < self._n_ts:
                            time_series[i, t, f] = float(sample.get(feat, 0) or 0)
                    for f, feat in enumerate(self.known_features):
                        if f < self._n_known:
                            known_time[i, t, f] = float(sample.get(feat, 0) or 0)

        return {
            "static_cat": static_cat,
            "static_num": static_num,
            "time_series": time_series,
            "known_time": known_time,
            "mask": mask,
        }

    def predict_batch(self, items: List[dict], mode: str = "cold") -> List[PredictionResult]:
        if not items:
            return []

        inputs = self._build_inputs(items, mode)
        log_durations, pattern_logits, resource_peaks = self.session.run(None, inputs)

        results = []
        for i in range(len(items)):
            durations = np.expm1(np.clip(log_durations[i], -2, 15))
            probs = _softmax(pattern_logits[i])
            pattern_idx = int(np.argmax(probs))

            results.append(PredictionResult(
                processing_duration_s=max(float(durations[0]), 1.0),
                upload_duration_s=max(float(durations[1]) if len(durations) > 1 else 10.0, 1.0),
                download_duration_s=max(float(durations[2]) if len(durations) > 2 else 5.0, 1.0),
                pattern_type=PATTERN_NAMES[pattern_idx] if pattern_idx < len(PATTERN_NAMES) else "unknown",
                pattern_confidence=float(probs[pattern_idx]),
                peak_ram_mb=max(float(resource_peaks[i][0]), 0),
                peak_vram_mb=max(float(resource_peaks[i][1]), 0) if len(resource_peaks[i]) > 1 else 0,
                peak_cpu_pct=max(float(resource_peaks[i][2]), 0) if len(resource_peaks[i]) > 2 else 0,
                peak_net_mbps=max(float(resource_peaks[i][3]), 0) if len(resource_peaks[i]) > 3 else 0,
                model_version=self.version,
                inference_mode=mode,
            ))

        return results

    def predict_cold(self, node_name: str, process_type: str, size_tier: str,
                     hardware_profile: dict) -> PredictionResult:
        item = {
            "node_name": node_name,
            "process_type": process_type,
            "size_tier": size_tier,
            **hardware_profile,
        }
        return self.predict_batch([item], mode="cold")[0]

    def predict_warm(self, node_name: str, process_type: str, size_tier: str,
                     hardware_profile: dict, samples: List[dict]) -> PredictionResult:
        item = {
            "node_name": node_name,
            "process_type": process_type,
            "size_tier": size_tier,
            "samples": samples,
            **hardware_profile,
        }
        return self.predict_batch([item], mode="warm")[0]


def _softmax(x):
    e = np.exp(x - np.max(x))
    return e / e.sum()
