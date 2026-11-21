"""Seed the Stack Model Call node (CORE/DockerContainers/stack-model-call).

Registers a TENANT-SHARED (tenant_id=1, user_id=NULL) node that wraps the
``stack-model-call`` container: 0..N image cogs + a PROMPT -> the TENANT's own
model stack, through ``POST {API_URL}/v1/llm/chat/completions``
(entrypoint.py:137 reads ``API_URL``; entrypoint.py:1-5 cite the same base URL
the reference nodes read — 3d-thumbnail-gen/generate_thumbnail.py:273).

WHY THIS EXISTS (the gap it fills)
----------------------------------
collective-be (10-26) gap 1: a workflow author needs to ask a model about the
IMAGES already in the workflow (Scout batches photos) without pointing at a BYO
endpoint. ``Switchyard LLM`` (seed_switchyard_llm_node.py) is BYO-base_url and
text-only (input_schema text, :71); ``Text LLM`` (seed_text_llm_node.py) is
text-only and hardcoded by role to your configured endpoints. This node takes
image cogs and calls the MOD route with the node's OWN run_task token
(entrypoint.py:6 — vendored mod_node_auth.get_headers()), so it needs no
credential of its own.

NO API_KEY / BASE_URL FIELD
---------------------------
The node never contacts any host but the MOD API (entrypoint.py:1-5: "No other
host is ever contacted and there is no BASE_URL override env"), authenticated by
the run_task token (n7d makes /v1/llm/chat/completions accept it). So there is
nothing to configure: ``API_URL`` is platform-injected, exactly like the other
nodes that reach the API — services/workflow_orchestrator.py:1459
``_cenv.setdefault("API_URL", os.getenv("API_BASE_URL", "http://api:80"))``
(same pattern in services/cogs_shared_service.py:504,
services/node_definitions_workflows_service.py:325).

Idempotent by (name, tenant_id); an existing row has its configuration_schema +
description reconciled to this file (same posture as
seed_switchyard_llm_node.py:203-222).
"""
from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

TENANT_ID = 1  # the bootstrap admin's tenant is tenant 1; tenant-shared (user_id NULL)

STACK_MODEL_CALL_NODE = {
    "name": "Stack Model Call",
    "description": (
        "Ask the tenant's own model stack about the images in your workflow. "
        "Feed 0..N image cogs plus a PROMPT; the node base64-encodes each image "
        "as a data: URI and POSTs the multimodal message to "
        "POST {API_URL}/v1/llm/chat/completions with the node's run_task token "
        "— no external host, no API key to configure. Set MODEL (required) and "
        "PROMPT (required); optionally SYSTEM_PROMPT and JSON_SCHEMA (a JSON "
        "Schema string — the node validates the reply and fails loudly if it "
        "does not conform). Bounds: MAX_IMAGES (default 8) and MAX_IMAGE_BYTES "
        "(default 8 MiB each) are LOUD FAILS, never truncations — if Scout "
        "batches more than MAX_IMAGES photos the run fails with a clear "
        "message telling you to lower the caps, it does not silently drop any "
        "image. Accepted image formats: png, jpg/jpeg, webp, gif (extension "
        "AND magic bytes must agree, entrypoint.py:34-45). Zero images = text-"
        "only call. Outputs output.txt (the reply) and response.json (model + "
        "usage metadata)."
    ),
    "type": "LLM",
    "executable_path": platform_image("stack-model-call:latest"),
    "process_type": "CPU",
    # Descriptive only — no orchestrator credential-injection gate keys off this
    # node_kind; the node authenticates with its own run_task token (entrypoint.py:6)
    # and needs no external egress (entrypoint.py:1-5).
    "node_kind": "stack_model_call",
    "batch_mode": "batch",
    "input_schema": {"input_type": "image"},
    "output_schema": {"output_type": "text"},
    "configuration_schema": {
        "extra_variables": {
            "MODEL": {
                "type": "string",
                "default": "",
                "required": True,
                "description": (
                    "Model id to send to the MOD LLM route. Required — the "
                    "node fails loudly if it is blank (entrypoint.py:117-121)."
                ),
            },
            "PROMPT": {
                "type": "string",
                "default": "",
                "required": True,
                "description": (
                    "The question/instruction text. Required — the node fails "
                    "loudly if it is blank (entrypoint.py:118-123)."
                ),
            },
            "SYSTEM_PROMPT": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional system instruction, sent as the first message "
                    "(entrypoint.py:125)."
                ),
            },
            "JSON_SCHEMA": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional JSON Schema as a JSON string, e.g. "
                    "{\"type\":\"object\",\"properties\":{\"answer\":"
                    "{\"type\":\"string\"}},\"required\":[\"answer\"]}. Must "
                    "parse as a JSON object or the node fails loudly "
                    "(entrypoint.py:126-135)."
                ),
            },
            "MAX_IMAGES": {
                "type": "number",
                "default": 8,
                "minimum": 1,
                "description": (
                    "How many image cogs the node will accept (entrypoint.py:142). "
                    "LOUD FAIL, never a truncation: more than this -> the node "
                    "exits non-zero naming the count and this cap "
                    "(entrypoint.py:73), it never silently drops images. Scout "
                    "batches of >8 photos need this raised (mind the request "
                    "body cap) or the batch split across runs."
                ),
            },
            "MAX_IMAGE_BYTES": {
                "type": "number",
                "default": 8388608,
                "minimum": 1,
                "description": (
                    "Per-image size cap in bytes, default 8 MiB "
                    "(entrypoint.py:143). Also a loud fail: an oversized file "
                    "fails the run naming the file and its size "
                    "(entrypoint.py:78), never a silent skip."
                ),
            },
            "MAX_TOKENS": {
                "type": "number",
                "default": None,
                "description": (
                    "Maximum tokens to generate. Unset = omit the field "
                    "entirely and let the stack decide (entrypoint.py:144)."
                ),
            },
            "TEMPERATURE": {
                "type": "number",
                "default": 0.3,
                "minimum": 0,
                "maximum": 2,
                "description": (
                    "Sampling temperature, 0.0-2.0 (entrypoint.py:145 enforces "
                    "lo=0.0 hi=2.0). Lower = more focused."
                ),
            },
            "REQUEST_TIMEOUT_SECONDS": {
                "type": "number",
                "default": 300,
                "minimum": 1,
                "description": (
                    "Completion-call timeout in seconds (entrypoint.py:146, "
                    "lo=1)."
                ),
            },
            "MODEL_WAKE_TIMEOUT_SECONDS": {
                "type": "number",
                "minimum": 0,
                "maximum": 3600,
                "description": (
                    "TOTAL seconds this node will wait for the tenant's model "
                    "stack to wake before failing MODEL_UNAVAILABLE (30.6.7 "
                    "c7-a5). UNSET = the platform setting "
                    "llm.model_wake_timeout_seconds, which the API sends back "
                    "on the 503 as X-Model-Wake-Budget-Seconds, else 600. Set "
                    "it to override that budget per run; 0 = do not wait — "
                    "fail at once on the first model_waking 503. Bounds 0..3600 "
                    "are enforced by the node itself (entrypoint.py, clamped), "
                    "and garbage is ignored, never a crash. No default key: "
                    "unset must stay unset so the platform setting still wins "
                    "(same optional-variable shape as MAX_TOKENS, which "
                    "defaults to null = omit the field)."
                ),
            },
        }
    },
    # entrypoint.py:34 EXT_MIME keys (png/jpg/jpeg/webp/gif) — models.py:2595
    # says null means "accept all", so pin the container's real set.
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "gif"],
    "cost_in_credits": 0,
}


def seed_stack_model_call_node(db: Session) -> int:
    """Register the Stack Model Call node as a tenant-shared node. Returns 1 if
    created, 0 if it already existed (idempotent). An existing row has its
    configuration_schema + description reconciled to this file so new
    parameters reach the live node."""
    existing = db.query(NodeDefinition).filter(
        NodeDefinition.name == STACK_MODEL_CALL_NODE["name"],
        NodeDefinition.tenant_id == TENANT_ID,
    ).first()
    if existing:
        changed = False
        if existing.configuration_schema != STACK_MODEL_CALL_NODE["configuration_schema"]:
            existing.configuration_schema = STACK_MODEL_CALL_NODE["configuration_schema"]
            changed = True
        if existing.description != STACK_MODEL_CALL_NODE["description"]:
            existing.description = STACK_MODEL_CALL_NODE["description"]
            changed = True
        if changed:
            db.commit()
        return 0
    nd = NodeDefinition(
        name=STACK_MODEL_CALL_NODE["name"],
        tenant_id=TENANT_ID,
        user_id=None,  # tenant-shared — everyone in the tenant can use it
        description=STACK_MODEL_CALL_NODE["description"],
        type=STACK_MODEL_CALL_NODE["type"],
        executable_path=STACK_MODEL_CALL_NODE["executable_path"],
        process_type=STACK_MODEL_CALL_NODE["process_type"],
        node_kind=STACK_MODEL_CALL_NODE["node_kind"],
        batch_mode=STACK_MODEL_CALL_NODE["batch_mode"],
        input_schema=STACK_MODEL_CALL_NODE["input_schema"],
        output_schema=STACK_MODEL_CALL_NODE["output_schema"],
        configuration_schema=STACK_MODEL_CALL_NODE["configuration_schema"],
        accepted_formats=STACK_MODEL_CALL_NODE["accepted_formats"],
        cost_in_credits=STACK_MODEL_CALL_NODE["cost_in_credits"],
        is_in_marketplace=True,  # surface it in Browse for the tenant
    )
    db.add(nd)
    db.commit()
    return 1
