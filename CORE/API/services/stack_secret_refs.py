"""
Pure secret-reference helpers: regex + name extraction for ${secret:NAME} and
{"secret": "<name>"} forms. No Vault, no DB — CE-shipped.
"""
import re
from typing import Any, Dict


# Whole-VALUE compose substitution: `${secret:NAME}`. NAME follows the same
# lettering as secret names used across the product (letters, digits, `_`,
# `.` and `-`).
_SECRET_REF_RE = re.compile(r"^\$\{secret:([A-Za-z0-9][A-Za-z0-9_.\-]*)\}$")




# ---------------------------------------------------------------------------
# ref shape helpers
# ---------------------------------------------------------------------------

def is_named_ref(ref: Any) -> bool:
    """True for the 13.5a named-ref form `{"secret": "<name>"}` (optionally
    with `"type": "file"` / `"target"`). Bare vault path strings are NOT
    named refs — those stay the existing resolver's job."""
    return (
        isinstance(ref, dict)
        and isinstance(ref.get("secret"), str)
        and ref["secret"].strip() != ""
    )


def named_secret_name(ref: Dict[str, Any]) -> str:
    return str(ref["secret"]).strip()
