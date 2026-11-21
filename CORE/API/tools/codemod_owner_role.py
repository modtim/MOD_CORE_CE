#!/usr/bin/env python3
"""CE-MIGROLE-3a — codemod for the migrations that still hardcode the modTech owner role.

CORE/API/migrations/_owner_role.py (CE-MIGROLE-1) is the single resolver every
migration should use: `owner_role(cur)` gives the QUOTED owner token
(`"modTech"` on the hosted product, the connection's current user on a CE install) and
`maybe_set_role(cur)` switches into the role only when it exists.  182
migrations still spell the role out (`_OWNER_ROLE = "modTech"` plus
`ALTER ... OWNER TO "{_OWNER_ROLE}"` / `SET ROLE "{_OWNER_ROLE}"` / a
`psycopg2.connect(..., user=os.environ.get("POSTGRES_USER", _OWNER_ROLE))`
fallback).  Hand-editing 182 files is not viable; this codemod does it.

Modes
    --report                 classify every file, print counts + the ODD list
    --apply --files <list>   rewrite the named files (path or bare name)

Classification (per file, from the file's own text)
    a  standard ALTER OWNER   `cur.execute(f'ALTER TABLE <tgt> OWNER TO "{_OWNER_ROLE}"')`
                              (also the literal `OWNER TO "modTech"` form)
    b  SET ROLE block         `set_role_ok = False` + try/`SET ROLE`/except-print
    c  connect-default        `user=os.environ.get("POSTGRES_USER", _OWNER_ROLE)`
    d  ODD                    anything not cleanly matched

Rewrites mirror the already-converted references (20260508*, 20260907_add_wizard_runs,
20260908_add_conversational_project_builder, 20261007n_fx152_key_reminders):

    _OWNER_ROLE = "modTech"                     -> deleted
    cur.execute(f'ALTER ... OWNER TO "{_OWNER_ROLE}"')
                                                -> cur.execute(f'ALTER ... OWNER TO {owner}')
    print(f"(ALTER ... OWNER TO {_OWNER_ROLE} failed: {e})")
                                                -> print(f"(ALTER ... OWNER TO {owner} failed: {e})")
    set_role_ok = False
    try:  cur.execute(f'SET ROLE "{_OWNER_ROLE}"')  set_role_ok = True
    except Exception as e: print(...)            -> set_role_ok = _owner_role.maybe_set_role(cur)
    owner token                                 -> owner = _owner_role.owner_role(cur)
    psycopg2.connect(user=env(..., _OWNER_ROLE), dbname=env(..., "modTech"),
                     password=env(..., ""))     -> env-only, no fallback (RuntimeError if unset)

Every rewritten file must ast-parse AND be free of leftover `_OWNER_ROLE` /
`modTech` in CODE lines (comments/docstrings may still mention the role by name);
otherwise the codemod refuses to write it and reports it ODD.  Nothing is executed
against any DB — this tool only rewrites source.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # CORE/API
MIG_DIR = os.path.join(API_DIR, "migrations")

CONST_RE = re.compile(r'^_OWNER_ROLE = "modTech"[ \t]*$', re.M)
CONNECT_CALL_RE = re.compile(r"^\s*\w+ = psycopg2\.connect\($")
CONNECT_BLOCK = (
    "{indent}# CE-MIGROLE-1: no hardcoded owner/user default — connect as whatever\n"
    "{indent}# POSTGRES_USER this install uses (SaaS: the modTech superuser; CE fresh\n"
    "{indent}# boot: `core`).  Nothing here re-owns objects, so no ALTER OWNER role\n"
    "{indent}# token is needed; the statements are all IF-NOT-EXISTS column/index DDL.\n"
    "{indent}user = os.environ.get(\"POSTGRES_USER\")\n"
    "{indent}password = os.environ.get(\"POSTGRES_PASSWORD\")\n"
    "{indent}db = os.environ.get(\"POSTGRES_DB\")\n"
    "{indent}if not (user and password and db):\n"
    "{indent}    raise RuntimeError(\n"
    "{indent}        \"POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB env vars \"\n"
    "{indent}        \"missing — run this migration inside my_fastapi_app where \"\n"
    "{indent}        \"they're populated by docker-compose.\"\n"
    "{indent}    )\n"
    "{indent}\n"
)
ALTER_RE = re.compile(r"cur\.execute\(f?['\"]ALTER TABLE .*? OWNER TO \"(?:\{_OWNER_ROLE\}|modTech)\"")
ALTER_FAIL_RE = re.compile(r"OWNER TO (?:\{_OWNER_ROLE\}|modTech) failed: \{e\}")
CONNECT_USER_RE = re.compile(r"user=os\.environ\.get\(\"POSTGRES_USER\",\s*_OWNER_ROLE\)")
CONNECT_DB_RE = re.compile(r'dbname=os\.environ\.get\("POSTGRES_DB",\s*"modTech"\)')
CONNECT_PW_RE = re.compile(r'password=os\.environ\.get\("POSTGRES_PASSWORD",\s*""\)')
SETROLE_HEAD_RE = re.compile(r"^\s*set_role_ok = False\s*$")
SETROLE_TRY_RE = re.compile(r"^\s*try:\s*$")
SETROLE_EXEC_RE = re.compile(r"^\s*cur\.execute\(f?['\"]SET ROLE .*['\"]\)\s*$")
SETROLE_TRUE_RE = re.compile(r"^\s*set_role_ok = True\s*$")
SETROLE_EXCEPT_RE = re.compile(r"^\s*except Exception(?: as e)?:\s*(#.*)?$")
SETROLE_PRINT_RE = re.compile(r"^\s*(print\(.*|pass(?:\s+#.*)?)$")


def _paren_balance(text: str) -> int:
    return text.count("(") + text.count("[") - text.count(")") - text.count("]")


def _match_setrole(lines: list[str], i: int) -> tuple[int, int] | None:
    """(start, end exclusive) if a hardcoded SET ROLE block starts at i."""
    n = len(lines)
    head = i < n and bool(SETROLE_HEAD_RE.match(lines[i]))
    j = i + 1 if head else i
    if not (j < n and SETROLE_TRY_RE.match(lines[j])):
        return None
    j += 1
    if j >= n or not SETROLE_EXEC_RE.match(lines[j]):
        return None
    j += 1
    if head and j < n and SETROLE_TRUE_RE.match(lines[j]):
        j += 1
    if not (j < n and SETROLE_EXCEPT_RE.match(lines[j])):
        return None
    j += 1
    if not (j < n and SETROLE_PRINT_RE.match(lines[j])):
        return None
    bal = 0
    while j < n:
        bal += _paren_balance(lines[j])
        j += 1
        if bal <= 0:
            break
    return i, j


def _setrole_spans(lines: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        hit = _match_setrole(lines, i)
        if hit is None:
            i += 1
            continue
        spans.append(hit)
        i = hit[1]
    return spans


def _docstring_lines(src: str) -> set[int]:
    """1-based line numbers covered by docstrings (module/function/class bodies)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return doc_lines


def _code_lines(src: str) -> list[str]:
    """Lines that are code (not comments/docstrings) — used for the safety check."""
    doc_lines = _docstring_lines(src)
    out = []
    for i, line in enumerate(src.splitlines(), start=1):
        if i in doc_lines:
            continue
        code = line.split("#")[0]
        out.append(code)
    return out


STRAY_LINE_RES = (ALTER_RE, ALTER_FAIL_RE, CONNECT_USER_RE, CONNECT_DB_RE)
CONST_LINE_RE = re.compile(r"^\s*_OWNER_ROLE = \"modTech\"[ \t]*$")


def _classify(src: str) -> tuple[str, list[str]]:
    """Return (kind, reasons).  kind in {'a','b','c','d'}."""
    reasons: list[str] = []
    if CONNECT_USER_RE.search(src):
        kind = "c"
    elif ALTER_RE.search(src):
        kind = "a"
    elif _setrole_spans(src.splitlines()):
        kind = "b"
    elif "_OWNER_ROLE" in src or "modTech" in src:
        kind = "d"
        reasons.append("no recognised ALTER/SET-ROLE/connect pattern")
    else:
        kind = "d"
        reasons.append("nothing to convert")
    return kind, reasons


def _stray_modtech_lines(src: str) -> list[int]:
    """1-based lines holding a `modTech` literal that a-d do not already count.

    Comments and docstrings are ignored; so is the `_OWNER_ROLE` constant and
    any line already matched by the ALTER / SET-ROLE / connect patterns.
    """
    doc_lines = _docstring_lines(src)
    setrole_lines: set[int] = set()
    for a, b in _setrole_spans(src.splitlines()):
        setrole_lines.update(range(a + 1, b + 1))
    hits: list[int] = []
    for i, line in enumerate(src.splitlines(), start=1):
        if i in setrole_lines:
            continue
        if i in doc_lines:
            continue
        code = line.split("#")[0]
        if "modTech" not in code:
            continue
        if CONST_LINE_RE.match(line):
            continue
        if any(rx.search(code) for rx in STRAY_LINE_RES):
            continue
        hits.append(i)
    return hits


def _import_lines(indent: str) -> str:
    return (
        f"{indent}sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
        f"{indent}import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime\n"
    )


def _ensure_fstring(line: str) -> str:
    """Add the `f` prefix to the string literal that just gained a {owner} field."""
    if "{owner}" not in line:
        return line
    if re.search(r"f(['\"])[^'\"]*\{owner\}", line):
        return line
    new = re.sub(
        r"(cur\.execute\()(['\"])([^'\"]*\{owner\}[^'\"]*)",
        lambda m: f"{m.group(1)}f{m.group(2)}{m.group(3)}",
        line,
    )
    if new != line:
        return new
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    if stripped[:1] in ("'", '"'):
        return indent + "f" + stripped
    return line


def _rewrite(src: str, kind: str) -> str | None:
    """Return the rewritten source, or None if the result is refused (unsafe)."""
    lines = src.splitlines(keepends=True)
    bare = [l.rstrip("\n") for l in lines]
    drop: set[int] = set()
    repl: dict[int, str] = {}
    ins: dict[int, str] = {}

    for i, l in enumerate(bare):
        if CONST_RE.match(l):
            drop.add(i)
            if i + 1 < len(bare) and bare[i + 1].strip() == "":
                drop.add(i + 1)

    setrole_starts: list[int] = []
    for start, end in _setrole_spans(bare):
        indent = re.match(r"\s*", bare[start]).group(0)
        repl[start] = f"{indent}set_role_ok = _owner_role.maybe_set_role(cur)"
        for j in range(start + 1, end):
            drop.add(j)
        setrole_starts.append(start)

    first_owner: int | None = None
    for i, l in enumerate(bare):
        if i in drop or i in repl:
            continue
        if "OWNER TO" not in l:
            continue
        stripped = l.lstrip()
        if not (stripped.startswith("cur.execute(") or stripped.startswith('print(f"')
                or stripped.startswith("print(f'")):
            continue  # SQL constants / docstrings are not touched
        newl = l
        newl = newl.replace(' OWNER TO "{_OWNER_ROLE}"', " OWNER TO {owner}")
        newl = newl.replace(' OWNER TO "modTech"', " OWNER TO {owner}")
        newl = newl.replace("OWNER TO {_OWNER_ROLE}", "OWNER TO {owner}")
        newl = newl.replace("OWNER TO modTech", "OWNER TO {owner}")
        if newl != l:
            newl = _ensure_fstring(newl)
            repl[i] = newl
            if first_owner is None:
                first_owner = i

    # CE-MIGROLE-3: SET-ROLE-only files (no OWNER TO rewrite) still need the import
    if first_owner is not None or setrole_starts:
        anchor = first_owner
        if setrole_starts:
            anchor = min(setrole_starts) if first_owner is None else min(min(setrole_starts), first_owner)
        else:
            ind = len(re.match(r"\s*", bare[first_owner]).group(0))
            j = first_owner - 1
            while j >= 0 and (bare[j].strip() == ""
                               or len(re.match(r"\s*", bare[j]).group(0)) >= ind):
                j -= 1
            if j >= 0 and bare[j].rstrip().endswith(":"):
                anchor = j
        indent = re.match(r"\s*", bare[anchor]).group(0)
        block = _import_lines(indent)
        if first_owner is not None:
            block += f"{indent}owner = _owner_role.owner_role(cur)\n"
        ins[anchor] = block + ins.get(anchor, "")

    if kind == "c":
        for i, l in enumerate(bare):
            if CONNECT_CALL_RE.match(l):
                indent = re.match(r"\s*", l).group(0)
                ins[i] = CONNECT_BLOCK.format(indent=indent)
                break
        for i, l in enumerate(bare):
            if CONNECT_USER_RE.search(l):
                repl[i] = re.sub(r"user=os\.environ\.get\(\"POSTGRES_USER\",\s*_OWNER_ROLE\)",
                                 "user=user,", l)
            elif CONNECT_DB_RE.search(l):
                repl[i] = re.sub(r'dbname=os\.environ\.get\("POSTGRES_DB",\s*"modTech"\)',
                                 "dbname=db,", l)
            elif CONNECT_PW_RE.search(l):
                repl[i] = re.sub(r'password=os\.environ\.get\("POSTGRES_PASSWORD",\s*""\)',
                                 "password=password,", l)

    out: list[str] = []
    for i, l in enumerate(bare):
        if i in drop:
            continue
        if i in ins:
            out.extend(x for x in ins[i].split("\n") if x != "")
        out.append(repl.get(i, l))
    new_src = "\n".join(out)
    if src.endswith("\n"):
        new_src += "\n"

    try:
        ast.parse(new_src)
    except SyntaxError:
        return None
    for cl in _code_lines(new_src):
        if "_OWNER_ROLE" in cl or "modTech" in cl:
            return None
    return new_src


def _resolve(rel: str) -> str:
    p = rel.strip()
    if os.path.sep not in p:
        return os.path.join(MIG_DIR, p)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="classify and count; print the ODD list")
    ap.add_argument("--apply", action="store_true", help="rewrite the named files")
    ap.add_argument("--files", help="space/comma-separated file list (path or bare migration name)")
    args = ap.parse_args()

    if not args.report and not args.apply:
        print("pass --report and/or --apply --files <list>")
        return 2

    if args.report:
        files = sorted(
            os.path.join(MIG_DIR, f) for f in os.listdir(MIG_DIR)
            if f.endswith(".py") and f != "_owner_role.py"
        )
        counts = {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0}
        odd: list[str] = []
        stray: list[str] = []
        for p in files:
            src = open(p).read()
            if CONST_RE.search(src):
                kind, reasons = _classify(src)
                counts[kind] += 1
                if kind == "d":
                    odd.append(f"CORE/API/{os.path.relpath(p, API_DIR)}: {reasons[0]}")
            for ln in _stray_modtech_lines(src):
                stray.append(f"CORE/API/{os.path.relpath(p, API_DIR)}:{ln}")
        counts["e"] = len(stray)
        print(f"a (ALTER OWNER)  : {counts['a']}")
        print(f"b (SET ROLE)     : {counts['b']}")
        print(f"c (connect)      : {counts['c']}")
        print(f"d (ODD)          : {counts['d']}")
        print(f"e (stray modTech): {counts['e']}")
        for line in odd:
            print(f"ODD {line}")
        for line in stray:
            print(f"STRAY {line}")
        return 1 if counts["e"] else 0

    targets = [t for t in re.split(r"[\s,]+", args.files or "") if t]
    if not targets:
        print("no files given")
        return 2
    written, refused = [], []
    for t in targets:
        p = _resolve(t)
        src = open(p).read()
        kind, _ = _classify(src)
        if kind == "d":
            if not CONST_RE.search(src):
                written.append((p, "no change (already converted)"))
            else:
                refused.append((p, "ODD — not rewritten"))
            continue
        new_src = _rewrite(src, kind)
        if new_src is None:
            refused.append((p, "rewrite refused: result failed ast-parse or left modTech/_OWNER_ROLE in code"))
            continue
        if new_src == src:
            written.append((p, "no change (already converted)"))
            continue
        open(p, "w").write(new_src)
        written.append((p, f"rewritten as kind {kind}"))
    for p, why in written:
        print(f"WROTE {p}: {why}")
    for p, why in refused:
        print(f"REFUSED {p}: {why}")
    return 0 if not refused else 1


if __name__ == "__main__":
    raise SystemExit(main())
