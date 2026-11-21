"""service-stack-builder Group 0 — shared contracts.

The single source of truth for the request/response shapes every other workstream
builds against (see the internal design notes). Field names here MUST
match components/service-builder/types.ts exactly.

A stack is composed from N services; each service comes from one SOURCE (image /
node / git / script / site), carries per-service config, and may be exposed. The
assemble endpoint turns this into a validated `compose_spec.services`; the estimate
endpoint prices the reservation vector.
"""
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, ConfigDict, Field


# --- service source (discriminated by `kind`) --------------------------------
# Kept as one model with a `kind` tag + optional fields (rather than a strict
# union) so the frontend and the per-source builders can evolve independently.
ServiceSourceKind = Literal["image", "node", "git", "script", "site"]


class ServiceSource(BaseModel):
    kind: ServiceSourceKind
    # image
    image: Optional[str] = None
    # node (de-specialized into a long-lived service — no smoke-guard / data-io)
    node_definition_id: Optional[int] = None
    # git (build-from-source, stays live)
    repo_url: Optional[str] = None
    ref: Optional[str] = None
    build_context: Optional[str] = None
    # script (packaged into a long-lived service image)
    script: Optional[str] = None
    base_image: Optional[str] = None
    filename: Optional[str] = None
    # site (static/live website)
    site_source: Optional[Literal["upload", "repo"]] = None
    site_ref: Optional[str] = None  # object key or repo url depending on site_source


class GpuReservation(BaseModel):
    vram_gb: int = 0


class ServiceSpec(BaseModel):
    """One service in the composed stack."""
    name: str
    source: ServiceSource
    env: Dict[str, str] = Field(default_factory=dict)
    # CONTAINER ports the service listens on (NOT host ports — exposed services
    # route over the mesh and never publish a host port; see stack-public-exposure).
    ports: List[int] = Field(default_factory=list)
    volumes: List[str] = Field(default_factory=list)
    gpu: Optional[GpuReservation] = None
    replicas: int = 1
    # reserved compute for pricing (cpu cores, memory GB); GPU comes from `gpu`.
    cpu: float = 0.0
    mem_gb: float = 0.0
    # expose publicly via the magic-URL system (x-mod.reachable + Caddy).
    expose: bool = False
    # optional explicit long-lived command (required if a node/image declares none).
    command: Optional[str] = None


class AssembleRequest(BaseModel):
    name: str
    services: List[ServiceSpec]


class AssembleResponse(BaseModel):
    compose_spec: Dict[str, Any]
    # per-service resolved notes (e.g. de-specialization applied, build queued).
    notes: Dict[str, str] = Field(default_factory=dict)


class WizardPrefillResponse(BaseModel):
    """`/persistent-stacks/from-wizard` pre-fill payload.

    The wizard on-ramp turns a plain-language prompt into a validated compose
    the builder pre-fills its form from. It deliberately does NOT persist a
    PersistentStack: deploy runs through /assemble → /persistent-stacks, so a
    stack row created here would only ever be an orphan `status=created`
    record. This response carries exactly what the builder pre-fills."""
    name: str
    compose_spec: Dict[str, Any]
    storage_class: str = "local_fs"
    accelerator_requirements: Optional[Dict[str, Any]] = None
    egress_policy: Optional[Dict[str, Any]] = None
    secret_refs: Optional[Dict[str, Any]] = None
    tls_config: Optional[Dict[str, Any]] = None


# --- compose import (stack-wizard-compose-import) ----------------------------
class ComposeImportRequest(BaseModel):
    """Preview an imported docker-compose: parse + normalize + infer x-mod."""
    name: str
    compose: str  # raw docker-compose YAML (paste or uploaded file content)


# --- 27.4 slice 2 — LLM serving-profile render preview -----------------------
class RenderPreviewRequest(BaseModel):
    """POST /stacks/render-preview — expand a top-level `x-mod.serving` LLM
    serving profile into concrete compose services (per-GPU engines + the
    SGLang gateway `lb`) and return the rendered YAML without creating anything."""
    compose_yaml: str  # raw docker-compose YAML, optionally with x-mod.serving


class RenderPreviewResponse(BaseModel):
    """The rendered compose as YAML plus its service names. `rendered` is True
    when a serving profile was present and expanded; a profile-less compose
    still parses+returns (rendered=False)."""
    compose_yaml: str
    services: List[str]
    rendered: bool


# --- x-mod correctness smoke (stack-training-and-prewarm §2.2) ----------------
# A compose-imported stack has no bundled family fixture/request client, so it may
# declare its OWN correctness smoke in `x-mod.smoke`: how to make one real request
# and what a valid response looks like. Consumed by
# services/stack_calibration.parse_xmod_smoke during characterization. Documentary
# models — the wire type stays Dict[str, Any] (confirmations is freeform), these
# pin the accepted shape so the contract is legible.
class XModSmokeRequest(BaseModel):
    # populate_by_name so the `json` wire key maps to json_body without shadowing
    # pydantic's BaseModel.json().
    model_config = ConfigDict(populate_by_name=True)
    path: str                                  # required — the request path
    method: str = "GET"
    protocol: Literal["http", "grpc"] = "http"
    port: Optional[int] = None                 # defaults to x-mod.readiness.port
    json_body: Optional[Dict[str, Any]] = Field(default=None, alias="json")  # JSON request body
    body: Optional[str] = None                 # raw request body (alt to json)
    headers: Optional[Dict[str, str]] = None
    timeout_s: Optional[float] = None


class XModSmokeExpect(BaseModel):
    status: Optional[int] = None               # expected HTTP status (default: <400)
    contains: Optional[str] = None             # substring the body must contain
    json_contains: Optional[Dict[str, Any]] = None  # key==value the JSON body must have


class XModSmoke(BaseModel):
    request: XModSmokeRequest
    expect: Optional[XModSmokeExpect] = None


class ComposeImportPreviewResponse(BaseModel):
    """The normalized candidate compose_spec plus the structured list of MOD
    metadata the user must confirm before it can be stored."""
    name: str
    compose_spec: Dict[str, Any]
    # each item: {scope: service|stack, field, suggested, reason, required,
    #             service?, options?} — see services/stack_compose_import.infer_xmod
    needs_confirmation: List[Dict[str, Any]] = Field(default_factory=list)
    storage_class: str = "local_fs"
    # 13.5c — inline secret-looking env values (KEY matches the product's
    # _SECRET_ENV_RE) found in the pasted compose: each item is
    # {service, env, secret} (secret = suggested tenant-secret name). The
    # compose_spec's values for these keys are REDACTED to the ***redacted***
    # sentinel in this response; the client passes the chosen items back in
    # finalize as `move_secrets` so the values become tenant secrets.
    inline_secrets: List[Dict[str, str]] = Field(default_factory=list)
    # D76 — plain-language warnings, e.g. a requested fixed host port that is
    # already in use on every worker (names the holder). Advisory: never blocks.
    warnings: List[str] = Field(default_factory=list)


class ComposeImportFinalizeRequest(BaseModel):
    """Finalize an import: (re)normalize, apply confirmations, validate, create.

    Prefer sending the raw `compose` YAML (re-normalized server-side so a client
    cannot smuggle a spec past normalization); `compose_spec` is accepted as a
    fallback for callers that already hold a normalized spec."""
    name: str
    compose: Optional[str] = None
    compose_spec: Optional[Dict[str, Any]] = None
    # {services: {<name>: {accelerator?, egress?, readiness?, smoke?}},
    #  ha_class?, storage_class?}  — `smoke` matches XModSmoke (correctness probe).
    confirmations: Dict[str, Any] = Field(default_factory=dict)
    home_zone: Optional[str] = None
    quota_bytes: Optional[int] = None
    # GA.ASW.6 — see schemas.PersistentStackCreate.acknowledge_no_backup.
    acknowledge_no_backup: bool = False
    # 13.5c — optional "Move to Secrets" selections: items from the preview's
    # `inline_secrets` list ({service, env, secret}) the caller chose to move.
    # Each becomes a tenant secret (created/updated through the 13.5a storage
    # path) and the spec's value becomes ${secret:NAME} — the plaintext never
    # lands in compose_spec. Omitted/empty = today's behaviour (values stay
    # inline and redact on GET).
    move_secrets: Optional[List[Dict[str, str]]] = None
    # GX.S10a — optional variables to set/declare on the imported stack.
    # {NAME: {kind, value?, existing_path?, file?, mode?, services?,
    #          cog_id?, from_cog_id?, pin_version?}}. Secret `value`s are
    # written to Vault and the stored spec keeps refs only (never the value);
    # a detected placeholder left undeclared is a 422 naming it.
    variables: Optional[Dict[str, Any]] = None


# --- cost estimate -----------------------------------------------------------
class EstimateServiceUnits(BaseModel):
    name: str
    cpu: float = 0.0
    mem_gb: float = 0.0
    gpu: List[int] = Field(default_factory=list)  # reserved vram_gb per GPU


class EstimateRequest(BaseModel):
    services: List[EstimateServiceUnits]


class EstimateServiceCost(BaseModel):
    name: str
    credits_per_hour: float


class EstimateResponse(BaseModel):
    per_service: List[EstimateServiceCost]
    total_credits_per_hour: float
    projected_credits_per_month: float
    rates: Dict[str, float]  # {cpu, mem, gpu}
    metering_enabled: bool


# --- placement check ----------------------------------------------------------
class PlacementCheckAccelerator(BaseModel):
    """GPU demand for one service. kind 'any' (or blank) is answered against
    every fleet accelerator kind and the best verdict wins."""
    kind: str = "any"
    count: int = Field(default=1, ge=1)
    min_vram_gb: float = Field(gt=0)


class PlacementCheckService(BaseModel):
    name: str
    accelerator: PlacementCheckAccelerator


class PlacementCheckRequest(BaseModel):
    """Pre-deploy 'does this fit the fleet?' demand, one entry per GPU service
    (POST /v1/stacks/placement-check). Read-only.

    `stack_id` (optional) answers the re-check case: when the demand belongs to a
    stack that is ALREADY running, its own reserved devices/VRAM are treated as
    free for the verdict (otherwise a stack re-checked on its own cards reads as
    "unplaceable" because it is competing with itself). The stack must belong to
    the caller's tenant, else the request 404s."""
    services: List[PlacementCheckService] = Field(default_factory=list)
    stack_id: Optional[int] = None


# --- node -> service fragment (Group B output contract) ----------------------
class ServiceFragment(BaseModel):
    """A compose-service fragment produced by a source resolver (node/git/script/
    site/image). Long-lived by construction: no smoke-guard, no /data/input or
    /data/output wiring, no auto-shutdown."""
    image: Optional[str] = None
    build: Optional[Dict[str, Any]] = None  # {context, dockerfile} for build sources
    command: Optional[str] = None
    environment: Dict[str, str] = Field(default_factory=dict)
    ports: List[int] = Field(default_factory=list)
    deploy: Dict[str, Any] = Field(default_factory=dict)  # resources.reservations
    restart: str = "unless-stopped"
