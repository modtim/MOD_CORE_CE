"""S1 for 30.6.7 c7-a5 — the seed exposes MODEL_WAKE_TIMEOUT_SECONDS.

The seed (CORE/API/seed_stack_model_call_node.py) is read as SOURCE here, not
imported: this file runs from the node's own tests dir (no CORE/API conftest /
sys.path), and the load-bearing invariant is the shipped configuration_schema
literal. ast keeps the check structural instead of grep-deep.
"""
import ast
import pathlib

SEED_NAME = "seed_stack_model_call_node.py"


def _seed_path():
    """Locate the seed source in whichever tree this test runs from: the repo
    layout (CORE/API/ next to CORE/DockerContainers/) or the r9700-run.sh
    container, where CORE/API is mounted at /app."""
    here = pathlib.Path(__file__).resolve()
    candidates = [here.parents[3] / "API" / SEED_NAME,
                  pathlib.Path("/app") / SEED_NAME]
    for cand in candidates:
        if cand.is_file():
            return cand
    raise AssertionError(f"seed source {SEED_NAME} not found from any known tree")


def _variables_block():
    """The extra_variables mapping (the dict that owns MODEL + the new var)."""
    tree = ast.parse(_seed_path().read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None
                for k in node.keys]
        if "MODEL" not in keys or "REQUEST_TIMEOUT_SECONDS" not in keys:
            continue
        try:
            return ast.literal_eval(node)
        except ValueError:
            continue
    raise AssertionError("no variables block found in the seed")


def test_s1_model_wake_variable_exists_bounded_and_defaultless():
    variables = _variables_block()
    assert "MODEL_WAKE_TIMEOUT_SECONDS" in variables, (
        "the seed must ship MODEL_WAKE_TIMEOUT_SECONDS (30.6.7 c7-a5: the node "
        "owns the total wake budget)"
    )
    spec = variables["MODEL_WAKE_TIMEOUT_SECONDS"]
    assert spec["type"] == "number"
    assert spec["minimum"] == 0
    assert spec["maximum"] == 3600
    assert "default" not in spec, (
        "no default key: unset must stay unset so the platform setting "
        "llm.model_wake_timeout_seconds still wins"
    )
    assert "llm.model_wake_timeout_seconds" in spec["description"]
    assert spec.get("secret") is not True
