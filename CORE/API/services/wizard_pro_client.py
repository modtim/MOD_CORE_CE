"""GT.P2.1 — CE-side client for the optional `mod-wizard-pro` sidecar (Option A).

The optional wizard ML side (`services/wizard_ml/**` + training) runs as a standalone
sidecar container; the CE API calls it over loopback HTTP. This module is the
CE-side client only and imports nothing from the non-CE side.

Contract (openspec launch-completion-megaplan decomp/gt-p1-wizard-boundary.md,
P2.1 row + section 3 Option A):
- Base URL: loopback constant. No `get_setting`/`get_platform_setting` helper
  exists in utils/services, so the constant is the documented default; no new
  behavioural env vars are introduced.
- Auth: internal service token from Vault path `platform/services/wizard-pro/token`
  (the sidecar reads the same path, so rotation is symmetric). No token -> the
  sidecar is treated as unavailable.
- 250 ms timeout so a wedged sidecar cannot stall the agentic loop.
- `pro_available()` is memoized with a 30 s TTL so a down sidecar does not add
  250 ms to every call.
- Per-symbol fallback contract when absent/unreachable (mirrors the existing
  `except ImportError` guards): embed -> None, rerank -> None, ab-buckets ->
  {} (callers read `.get(kind, DEFAULT_AB_BUCKET)`), config -> {}, model_version
  -> "ce". Every public function catches all errors and returns its fallback;
  nothing here raises into callers.
- Metrics (E10) are emitted in-process in CE: `MetricRegistry` below is the CE
  stub registry, no round trip to the sidecar.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from utils.vault_client import approle_login, read_kv_v2

PRO_BASE_URL = "http://127.0.0.1:8090"
VAULT_TOKEN_PATH = "platform/services/wizard-pro/token"
TIMEOUT_SECONDS = 0.25
AVAILABILITY_TTL_SECONDS = 30.0

DEFAULT_AB_BUCKET = "control"
CE_MODEL_VERSION = "ce"

# Tests inject an httpx.MockTransport here; production stays None (default transport).
_TEST_TRANSPORT: Optional[httpx.BaseTransport] = None

_avail_cache: Dict[str, Any] = {"value": None, "expires_at": 0.0}
_token_cache: Dict[str, Any] = {"value": "", "expires_at": 0.0}


def reset_caches() -> None:
    """Drop memoized availability/token state (tests, and after token rotation)."""
    _avail_cache["value"] = None
    _avail_cache["expires_at"] = 0.0
    _token_cache["value"] = ""
    _token_cache["expires_at"] = 0.0


def _read_token() -> str:
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not (role_id and secret_id):
        return ""
    try:
        token = approle_login(role_id=role_id, secret_id=secret_id)
        data = read_kv_v2(VAULT_TOKEN_PATH, token=token) or {}
        return str(data.get("value") or "").strip()
    except Exception:  # noqa: BLE001 — Vault outage must degrade, not raise
        return ""


def _token() -> str:
    now = time.monotonic()
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]
    value = _read_token()
    if value:
        _token_cache["value"] = value
    _token_cache["expires_at"] = now + AVAILABILITY_TTL_SECONDS
    return value


def _request(method: str, path: str, json: Optional[dict] = None,
            params: Optional[dict] = None) -> Optional[httpx.Response]:
    token = _token()
    if not token:
        return None
    try:
        with httpx.Client(base_url=PRO_BASE_URL, timeout=TIMEOUT_SECONDS,
                          transport=_TEST_TRANSPORT) as client:
            return client.request(method, path, json=json, params=params,
                                  headers={"X-Wizard-Pro-Token": token})
    except Exception:  # noqa: BLE001 — sidecar down must degrade, not raise
        return None


def pro_available() -> bool:
    """Memoized (30 s TTL) availability probe: one probe per TTL window, not
    250 ms per call. No token -> never available."""
    now = time.monotonic()
    if _avail_cache["value"] is not None and now < _avail_cache["expires_at"]:
        return bool(_avail_cache["value"])
    value = False
    try:
        if _token():
            resp = _request("GET", "/pro/v1/health")
            value = resp is not None and resp.status_code == 200
    except Exception:  # noqa: BLE001
        value = False
    _avail_cache["value"] = value
    _avail_cache["expires_at"] = now + AVAILABILITY_TTL_SECONDS
    return value


def embed(text: str, tenant_id: Optional[int] = None,
         tier: Optional[str] = None) -> Optional[List[float]]:
    """POST /pro/v1/embed — plain / per-tenant / cross-tenant variants. Fallback None."""
    if not pro_available():
        return None
    resp = _request("POST", "/pro/v1/embed",
                    json={"text": text, "tenant_id": tenant_id, "tier": tier})
    if resp is None or resp.status_code != 200:
        return None
    try:
        vector = (resp.json() or {}).get("vector")
        return list(vector) if vector else None
    except Exception:  # noqa: BLE001
        return None


def embed_batch(texts: List[str], tenant_id: Optional[int] = None) -> Optional[List[List[float]]]:
    """POST /pro/v1/embed-batch. Fallback None."""
    if not pro_available():
        return None
    resp = _request("POST", "/pro/v1/embed-batch", json={"texts": texts, "tenant_id": tenant_id})
    if resp is None or resp.status_code != 200:
        return None
    try:
        vectors = (resp.json() or {}).get("vectors")
        return [list(v) for v in vectors] if vectors else None
    except Exception:  # noqa: BLE001
        return None


def reembed_all(texts: List[str], batch: int = 128) -> Optional[Tuple[List[List[float]], str]]:
    """E2 — re-embed the wizard vector store's source texts: the sidecar does
    the embed + model version, the CE caller owns the pgvector DDL. Fallback
    None when the sidecar is absent (CE: skip the re-embed) or an embedded
    batch fails mid-run (the old table stays intact)."""
    if not pro_available():
        return None
    version = model_version()
    vecs: List[List[float]] = []
    for i in range(0, len(texts), batch):
        chunk = embed_batch(texts[i:i + batch])
        if chunk is None or len(chunk) != len(texts[i:i + batch]):
            return None
        vecs.extend(chunk)
    return vecs, version


def rerank(description: str, docs: List[str]) -> Optional[List[float]]:
    """POST /pro/v1/rerank — description + docs -> scores. Fallback None (no boost)."""
    if not pro_available():
        return None
    resp = _request("POST", "/pro/v1/rerank", json={"description": description, "docs": docs})
    if resp is None or resp.status_code != 200:
        return None
    try:
        scores = (resp.json() or {}).get("scores")
        return [float(s) for s in scores] if scores else None
    except Exception:  # noqa: BLE001
        return None


def rag_retrieve(canvas: Any, prompt: str, proposal: Any,
                 tenant_id: int) -> Optional[str]:
    """POST /pro/v1/rag/retrieve — few-shot block for a (canvas, prompt,
    proposal) on behalf of `tenant_id` (selects the per-tenant pool). The
    A/B bucket is resolved SERVER-SIDE by the sidecar from the tenant id —
    the client never sends or learns a bucket. Fallback None."""
    if not pro_available():
        return None
    resp = _request("POST", "/pro/v1/rag/retrieve",
                    json={"canvas": canvas, "prompt": prompt, "proposal": proposal,
                          "tenant_id": tenant_id})
    if resp is None or resp.status_code != 200:
        return None
    try:
        block = (resp.json() or {}).get("block")
        return str(block) if block else None
    except Exception:  # noqa: BLE001
        return None


def ab_buckets(tenant_id: Optional[int] = None) -> Dict[str, Any]:
    """GET /pro/v1/ab-buckets?tenant_id=… — per-model-kind bucket map
    (`{"rag": …, "reranker": …, …}`, callers pick the kind they need) + LLM
    model pin. Fallback: every kind on the default bucket, no pin."""
    if not pro_available():
        return {"buckets": {}, "model_pin": None}
    resp = _request("GET", "/pro/v1/ab-buckets", params={"tenant_id": tenant_id})
    if resp is None or resp.status_code != 200:
        return {"buckets": {}, "model_pin": None}
    try:
        data = resp.json() or {}
        return {"buckets": {str(k): str(v) for k, v in
                            dict(data.get("buckets") or {}).items()},
                "model_pin": data.get("model_pin")}
    except Exception:  # noqa: BLE001
        return {"buckets": {}, "model_pin": None}


def wizard_config() -> Dict[str, Any]:
    """GET /pro/v1/config — the sidecar's full wizard_ml config (toggles,
    ab_cutoffs, llm_model_pin; kill-switch reads, E13). Fallback {}."""
    if not pro_available():
        return {}
    resp = _request("GET", "/pro/v1/config")
    if resp is None or resp.status_code != 200:
        return {}
    try:
        return dict(resp.json() or {})
    except Exception:  # noqa: BLE001
        return {}


def config() -> Dict[str, Any]:
    """GET /pro/v1/config — the wizard_ml config's Toggles (kill-switch
    reads, E13). Fallback {}."""
    cfg = wizard_config()
    if not cfg:
        return {}
    return dict(cfg.get("toggles") or cfg)


def feedback_embed(record: Dict[str, Any], tenant_id: Optional[int] = None) -> Optional[List[float]]:
    """POST /pro/v1/feedback-embed — per-tenant record embed (E12/E14 path). Fallback None."""
    if not pro_available():
        return None
    resp = _request("POST", "/pro/v1/feedback-embed", json={"record": record, "tenant_id": tenant_id})
    if resp is None or resp.status_code != 200:
        return None
    try:
        vector = (resp.json() or {}).get("vector")
        return list(vector) if vector else None
    except Exception:  # noqa: BLE001
        return None


def embed_per_tenant(canvas: Any, prompt: Optional[str], proposal: Any,
                     tenant_id: Optional[int] = None) -> Optional[List[float]]:
    """E12 — per-tenant record embed (full-fidelity serialisation happens
    sidecar-side). Fallback None (caller skips the write)."""
    return feedback_embed(
        {"canvas": canvas, "prompt": prompt, "proposal": proposal},
        tenant_id=tenant_id)


def embed_cross_tenant(canvas: Any, prompt: Optional[str], proposal: Any,
                       intent: str = "from_scratch") -> Optional[List[float]]:
    """E14 — cross-tenant record embed: the sidecar strips (E15 privacy
    contract) then embeds; raw record never leaves CE unstripped. Fallback
    None (caller skips the write)."""
    return feedback_embed(
        {"canvas": canvas, "prompt": prompt, "proposal": proposal,
         "intent": intent},
        tenant_id=None)


def registry_refresh() -> bool:
    """E6 — ask the sidecar to re-read its model registry (the sidecar
    refreshes in-process; CE confirms it is alive and reporting). Fallback
    False (the scheduler job no-ops)."""
    if not pro_available():
        return False
    return bool(registry_status())


def refresh_interval() -> int:
    """E7 — the sidecar registry's refresh interval in seconds. Fallback 3600."""
    status = registry_status()
    try:
        return int(status.get("refresh_interval_seconds") or 3600)
    except (TypeError, ValueError):
        return 3600


def trigger_training(kind: str, scope: Optional[str] = None) -> Dict[str, Any]:
    """E8 — POST /pro/v1/training/{kind} (thread_started ack from the
    sidecar). Fallback {} (the scheduler job no-ops and logs)."""
    if not pro_available():
        return {}
    resp = _request("POST", f"/pro/v1/training/{kind}", json={"scope": scope})
    if resp is None or resp.status_code != 200:
        return {}
    try:
        return dict(resp.json() or {})
    except Exception:  # noqa: BLE001
        return {}


def registry_status() -> Dict[str, Any]:
    """GET /pro/v1/registry/status. Fallback {}."""
    if not pro_available():
        return {}
    resp = _request("GET", "/pro/v1/registry/status")
    if resp is None or resp.status_code != 200:
        return {}
    try:
        return dict(resp.json() or {})
    except Exception:  # noqa: BLE001
        return {}


def model_version() -> str:
    """Sidecar model pin when present; CE fallback 'ce'."""
    status = registry_status()
    return str(status.get("model_version") or CE_MODEL_VERSION)


def embedding_dim() -> int:
    """E1 — the width of the wizard embedding model in effect, from the
    sidecar registry. Fallback: the CE default 1024 (Arctic) — the value
    `database_wizard_vector._EMBEDDING_DIM` has always fallen back to."""
    try:
        dim = registry_status().get("embedding_dim")
        return int(dim) if dim else 1024
    except (TypeError, ValueError):
        return 1024


# --- E11: few-shot block rendering (CE-side mirror) --------------------------
#
# The paid `serialise_canvas` / `serialise_proposal` helpers moved to the
# sidecar; the few-shot block is what the Builder prompt consumes, and when
# the sidecar is absent we must still render the plain block client-side
# (the sidecar's own serializer logic lives here as the default render).


def _serialise_canvas(canvas: Optional[Dict[str, Any]]) -> str:
    """Stable full-fidelity render of a canvas (mirrors the paid
    serialise_canvas shape: NODES[name:cfg] EDGES[src->tgt])."""
    if not canvas:
        return ""
    nodes = canvas.get("nodes") or []
    edges = canvas.get("edges") or []
    nodes_sorted = sorted(nodes, key=lambda n: str(n.get("id", "")))
    edges_sorted = sorted(
        edges,
        key=lambda e: (str(e.get("source", "")), str(e.get("target", ""))),
    )
    node_strs: List[str] = []
    for n in nodes_sorted:
        nd_name = (n.get("node_definition_name")
                   or (n.get("data") or {}).get("nodeDefinitionName")
                   or "?")
        cfg = ((n.get("data") or {}).get("configurations")
               or n.get("configurations") or {})
        cfg_repr = json.dumps(cfg, sort_keys=True, default=str)[:200] if cfg else ""
        node_strs.append(f"{nd_name}:{cfg_repr}")
    edge_strs = [f"{e.get('source', '?')}->{e.get('target', '?')}" for e in edges_sorted]
    return f"NODES[{','.join(node_strs)}] EDGES[{','.join(edge_strs)}]"


def _serialise_proposal(proposal: Optional[Dict[str, Any]]) -> str:
    """Stable full-fidelity render of a proposal (mirrors the paid
    serialise_proposal: NAME[name] + the canvas shape of its body)."""
    if not proposal:
        return ""
    name = (proposal.get("name") or "").strip()
    body = _serialise_canvas(proposal)
    return f"NAME[{name}] {body}" if name else body


def few_shot_block(canvas: Any, prompt: Optional[str], proposal: Any) -> str:
    """E11 — the few-shot block for a (canvas, prompt, proposal). Sidecar
    present: POST /pro/v1/rag/retrieve (retrieval pool + A/B bucket resolved
    server-side). Sidecar absent: the CE-side default render of this record
    itself (the sidecar's serializer logic, moved client-side) — always a
    plain block, never None."""
    if pro_available():
        block = rag_retrieve(canvas, prompt or "", proposal, tenant_id=None)
        if block is not None:
            return block
    return (
        f"PROMPT: {(prompt or '').strip()}\n"
        f"CANVAS: {_serialise_canvas(canvas)}\n"
        f"PROPOSAL: {_serialise_proposal(proposal)}"
    )


def health() -> Dict[str, Any]:
    """GET /pro/v1/health — license + model readiness. Fallback {}."""
    if not pro_available():
        return {}
    resp = _request("GET", "/pro/v1/health")
    if resp is None or resp.status_code != 200:
        return {}
    try:
        return dict(resp.json() or {})
    except Exception:  # noqa: BLE001
        return {}


class MetricRegistry:
    """In-process CE metric registry stub (E10): metrics are emitted in CE,
    no round trip to the sidecar. Names follow the `wizard_` prefix convention
    of services/wizard_ml/metrics.py."""

    def __init__(self) -> None:
        self._metrics: Dict[str, Dict[Any, float]] = {}

    def get_metric(self, name: str) -> Dict[Any, float]:
        return self._metrics.setdefault(name, {})

    def record(self, name: str, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
        metric = self._metrics.setdefault(name, {})
        key = tuple(sorted((labels or {}).items()))
        metric[key] = metric.get(key, 0.0) + value

    def snapshot(self) -> Dict[str, Dict[Any, float]]:
        return {name: dict(values) for name, values in self._metrics.items()}


REGISTRY = MetricRegistry()
