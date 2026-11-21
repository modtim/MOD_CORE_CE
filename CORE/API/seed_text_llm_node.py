"""Seed the general-purpose Text LLM node.

Registers a TENANT-SHARED (tenant_id=1, user_id=NULL) node that wraps the
`textllm` container: it takes a text PROMPT (+ optional CONTEXT, e.g. the
context.txt a RAG Query node emits) and calls your configured text LLM (an
OpenAI-compatible endpoint), returning the generated text. It is the
prompt-composer for the image pipeline (compose an SDXL prompt from a request +
retrieved character context) and a general text step.

WHY tenant-shared (tenant_id=1, user_id=NULL) and NOT tenant_id=NULL-with-injection
-----------------------------------------------------------------------------------
Unlike the RAG/vector nodes, this node needs NO platform-injected credential.
The RAG nodes are tenant_id=NULL + node_kind in {vector_upsert, vector_query}
solely so the orchestrator's should_inject_vector_token gate mints a vector API
token — the ONLY credential that authenticates to /v1/cyclone/vector/*. This node calls the
managed LLM's served endpoint directly (http://ollama:11434 generator /
http://vllm-big:8000 reasoner — the same aliases routers/ai_config.py resolves),
which is served on the platform network with NO auth. Every GENERAL processing
node is already granted platform-network reachability by the orchestrator (it
sets MOD_NEEDS_API_TOKEN → the worker vends MOD_TASK_TOKEN_URL → netpol places
the container on the main network), so the alias resolves for the node exactly as
it does for the API. Nothing to inject → a normal tenant-shared node, mirroring
Video Combine (id 1498: tenant_id=1, user_id=NULL, is_in_marketplace=true).

Idempotent by (name, tenant_id).
"""
from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

TENANT_ID = 1  # the bootstrap admin's tenant is tenant 1; tenant-shared (user_id NULL)

TEXT_LLM_NODE = {
    "name": "Text LLM",
    "description": (
        "Generate text with your configured LLM. Takes a PROMPT and, optionally, "
        "CONTEXT (e.g. the context.txt a RAG Query node emits) and returns the "
        "completion. Use it to compose an SDXL prompt from a request plus "
        "retrieved character/brand context, or as a general text step "
        "(summarise, rewrite, classify, extract). Outputs output.txt (the text) "
        "and response.json (model + usage metadata)."
    ),
    "type": "LLM",
    "executable_path": platform_image("textllm:latest"),
    "process_type": "CPU",
    # Descriptive only — this node needs no credential injection, so this kind is
    # NOT in any orchestrator inject-gate allowlist (unlike vector_query).
    "node_kind": "text_llm",
    "batch_mode": "batch",
    "input_schema": {"input_type": "text"},
    "output_schema": {"output_type": "text"},
    "configuration_schema": {
        "extra_variables": {
            "ROLE": {
                "type": "string",
                "default": "generator",
                "description": (
                    "Which model to use: 'generator' (fast, default) or "
                    "'reasoner' (larger, for harder reasoning). Picks the endpoint."
                ),
            },
            "SYSTEM_PROMPT": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional system instruction (e.g. 'You are an expert SDXL "
                    "prompt engineer. Output one comma-separated prompt only')."
                ),
            },
            "PROMPT": {
                "type": "string",
                "default": "",
                "description": (
                    "The request text. Optional here — usually the prompt is "
                    "staged as the node's text input (/data/input/prompt.txt or "
                    "the first .txt/.md). Set it to hardcode a prompt."
                ),
            },
            "TEMPERATURE": {
                "type": "number",
                "default": 0.3,
                "description": "Sampling temperature (0.0-2.0). Lower = more focused.",
            },
            "MAX_TOKENS": {
                "type": "number",
                "default": 1024,
                "description": "Maximum tokens to generate.",
            },
            "MODEL": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional explicit served model id. Leave blank to use the "
                    "model the selected role's endpoint currently serves "
                    "(auto-discovered)."
                ),
            },
        }
    },
    "accepted_formats": None,
    "cost_in_credits": 0,
}


def seed_text_llm_node(db: Session) -> int:
    """Register the Text LLM node as a tenant-shared node. Returns 1 if created,
    0 if it already existed (idempotent)."""
    existing = db.query(NodeDefinition).filter(
        NodeDefinition.name == TEXT_LLM_NODE["name"],
        NodeDefinition.tenant_id == TENANT_ID,
    ).first()
    if existing:
        return 0
    nd = NodeDefinition(
        name=TEXT_LLM_NODE["name"],
        tenant_id=TENANT_ID,
        user_id=None,  # tenant-shared — everyone in the tenant can use it
        description=TEXT_LLM_NODE["description"],
        type=TEXT_LLM_NODE["type"],
        executable_path=TEXT_LLM_NODE["executable_path"],
        process_type=TEXT_LLM_NODE["process_type"],
        node_kind=TEXT_LLM_NODE["node_kind"],
        batch_mode=TEXT_LLM_NODE["batch_mode"],
        input_schema=TEXT_LLM_NODE["input_schema"],
        output_schema=TEXT_LLM_NODE["output_schema"],
        configuration_schema=TEXT_LLM_NODE["configuration_schema"],
        accepted_formats=TEXT_LLM_NODE["accepted_formats"],
        cost_in_credits=TEXT_LLM_NODE["cost_in_credits"],
        is_in_marketplace=True,  # surface it in Browse for the tenant
    )
    db.add(nd)
    db.commit()
    return 1


if __name__ == "__main__":
    import database
    from database import SessionLocal

    database.initialize()
    _db = SessionLocal()
    try:
        n = seed_text_llm_node(_db)
        row = _db.query(NodeDefinition).filter(
            NodeDefinition.name == TEXT_LLM_NODE["name"],
            NodeDefinition.tenant_id == TENANT_ID,
        ).first()
        print(f"created={n} id={row.id} name={row.name!r} tenant_id={row.tenant_id} "
              f"user_id={row.user_id} node_kind={row.node_kind} "
              f"is_in_marketplace={row.is_in_marketplace}")
    finally:
        _db.close()
