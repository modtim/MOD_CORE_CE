#!/usr/bin/env python3
"""P18-F1a (part 1) — fresh-install baseline manifest generator (static, DB-free).

A CE first boot runs 371 migration steps at ~2.5 s each (20.7 min; see
/tmp/rc20-batch/diag-p18.md). Most steps only add tables / columns / indexes
that create_tables() already builds from models.py (database.py:3595
`models.Base.metadata.create_all(bind=engine)`). This tool records those steps
as applied WITHOUT running them; everything else keeps executing.

CONSERVATIVE by design: a step is baselined ONLY IF ALL hold —
  (a) every SQL statement in it is one of
        CREATE TABLE IF NOT EXISTS / ALTER TABLE .. ADD COLUMN IF NOT EXISTS /
        CREATE INDEX IF NOT EXISTS / Base.metadata.create_all(tables=[named])
  (b) it has NO INSERT, UPDATE, DELETE, ON CONFLICT, session.add/db.add,
      FUNCTION, TRIGGER, VIEW, POLICY, ROW LEVEL SECURITY, GRANT, REVOKE,
      ALTER TYPE, ALTER OWNER, SET ROLE, DO $$, PARTITION, _owner_role,
      CHECK constraint, or UNIQUE constraint/index;
  (c) every table, column and index it creates exists in models.py with the
      same name (columns by name; a multi-clause ALTER checks EVERY ADD COLUMN
      clause; an index is accepted only when its name equals an explicit
      Index("name", ..) whose column list matches, or equals the
      ix_<table>_<col> name SQLAlchemy derives from a single index=True col).
Anything else is EXECUTE. Steps listed in tools/ce_migrate_skip.txt are left
out of both lists (they are neither run nor recorded).

Stdlib only; no DB connection, no import of models.py (models.py is parsed).
Usage:  python3 CORE/API/tools/gen_fresh_baseline.py [--stdout]
            [--extra-migrations-dir DIR ...]
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
API_DIR = TOOLS_DIR.parent
MIGRATIONS_DIR = API_DIR / "migrations"
MODELS_PY = API_DIR / "models.py"
SKIP_FILE = TOOLS_DIR / "ce_migrate_skip.txt"
OUT_FILE = TOOLS_DIR / "fresh_install_baseline.txt"

HOLE = "\x00HOLE\x00"

SQL_START = re.compile(
    r"^\s*(CREATE|ALTER|DROP|SELECT|INSERT|UPDATE|DELETE|WITH|GRANT|REVOKE"
    r"|SET|DO|COMMENT|TRUNCATE|VALUES|EXPLAIN)\b",
    re.IGNORECASE,
)

# (b) banned constructs, scanned over SQL-looking string literals only.
BANNED_SQL = [
    (r"\bINSERT\b", "INSERT"),
    (r"\bUPDATE\b", "UPDATE"),
    (r"\bDELETE\b", "DELETE"),
    (r"\bON\s+CONFLICT\b", "ON CONFLICT"),
    (r"\bFUNCTION\b", "FUNCTION"),
    (r"\bTRIGGER\b", "TRIGGER"),
    (r"\bVIEW\b", "VIEW"),
    (r"\bPOLICY\b", "POLICY"),
    (r"ROW\s+LEVEL\s+SECURITY", "ROW LEVEL SECURITY"),
    (r"\bGRANT\b", "GRANT"),
    (r"\bREVOKE\b", "REVOKE"),
    (r"ALTER\s+TYPE", "ALTER TYPE"),
    (r"ALTER\s+OWNER", "ALTER OWNER"),
    (r"\bSET\s+ROLE\b", "SET ROLE"),
    (r"DO\s+\$\$", "DO $$"),
    (r"\bPARTITION\b", "PARTITION"),
    (r"\bCHECK\b", "CHECK constraint"),
    (r"\bUNIQUE\b", "UNIQUE constraint/index"),
]

RE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
RE_COL = re.compile(
    r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
# Gap (i): EVERY `ADD COLUMN IF NOT EXISTS <col>` clause of a multi-clause ALTER,
# not just the first one.
RE_ALTER_CLAUSE = re.compile(
    r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
RE_INDEX = re.compile(
    r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", re.I)
# Read-only existence probes (information_schema / pg_indexes / pg_type / pg_enum)
# change no schema and no data, so skipping them cannot lie about either; they are
# allowed alongside the four additive forms of rule (a). Anything else SELECTed
# (e.g. a value read used to drive an ALTER) is EXECUTE.
RE_GUARD_SELECT = re.compile(
    r"^\s*SELECT\b(?=.{0,600}?\b(FROM|JOIN)\s+"
    r"(information_schema\.\w+|pg_indexes|pg_type\b|pg_enum\b|pg_foreign_\w+)"
    r")",
    re.I | re.S,
)

RE_ALLOWED_STMT = re.compile(
    r"^\s*(CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS"
    r"|ALTER\s+TABLE\b(?=.{0,400}?\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS)"
    r"|CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS)\b",
    re.I | re.S,
)


def load_skip_names() -> set[str]:
    """Stems listed in tools/ce_migrate_skip.txt (same parse as mod_migrate)."""
    names: set[str] = set()
    for raw in SKIP_FILE.read_text(errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(line[:-3] if line.endswith(".py") else line)
    return names


def load_models(path: Path) -> dict[str, dict[str, set[str]]]:
    """models.py -> {tablename: {cols, idx_names, idx_cols, classes}} (AST only)."""
    tree = ast.parse(Path(path).read_text(errors="replace"), filename=str(path))
    tables: dict[str, dict[str, set[str]]] = {}
    class_to_table: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        tname = None
        for st in node.body:
            if isinstance(st, ast.Assign):
                for tgt in st.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__tablename__":
                        if isinstance(st.value, ast.Constant) and isinstance(
                                st.value.value, str):
                            tname = st.value.value
        if tname is None:
            continue
        rec = tables.setdefault(
            tname, {"cols": set(), "idx_names": set(), "idx_cols": set(),
                    "idx_map": {}})
        class_to_table[node.name] = tname
        rec.setdefault("classes", set()).add(node.name)
        for st in node.body:
            if not isinstance(st, ast.Assign):
                continue
            # explicit Index("ix_..", "col") live inside class-level tuples
            for sub in ast.walk(st):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "Index"):
                    if (sub.args and isinstance(sub.args[0], ast.Constant)
                            and isinstance(sub.args[0].value, str)):
                        inm = sub.args[0].value
                        rec["idx_names"].add(inm)
                        cols = [a.value for a in sub.args[1:]
                                if isinstance(a, ast.Constant)
                                and isinstance(a.value, str)]
                        rec["idx_map"][inm] = (
                            tuple(cols) if len(cols) == len(sub.args) - 1
                            else None)
            for tgt in st.targets:
                if not (isinstance(tgt, ast.Name) and tgt.id and not tgt.id.startswith("__")):
                    continue
                v = st.value
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                        and v.func.id.endswith("Column"):
                    rec["cols"].add(tgt.id)
                    for kw in v.keywords:
                        if kw.arg in ("index", "unique") and isinstance(
                                kw.value, ast.Constant) and kw.value.value is True:
                            rec["idx_cols"].add(tgt.id)
    return tables, class_to_table


def sql_literals(tree: ast.AST) -> list[str]:
    """String literals that LOOK like SQL (start with a SQL keyword).

    Docstrings/prose are skipped: they never start with a SQL keyword. f-strings
    keep their literal parts and blank out each expression hole."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str):
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            parts = [(v.value if isinstance(v, ast.Constant) and isinstance(
                v.value, str) else HOLE) for v in node.values]
            text = "".join(str(p) for p in parts)
        else:
            continue
        if SQL_START.match(text):
            out.append(text)
    return out


def _split_statements(text: str) -> list[str]:
    out = []
    for chunk in text.split(";"):
        s = "\n".join(ln for ln in chunk.splitlines()
                      if not ln.lstrip().startswith("--")).strip()
        if s:
            out.append(s)
    return out


def _match_paren(text: str, start: int) -> int:
    """Index just past the paren group opening at `start`, or -1."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _table_body(text: str) -> tuple[str, list[str]] | None:
    """CREATE TABLE IF NOT EXISTS <name> ( ... ) -> (name, [col names])."""
    m = RE_TABLE.match(text)
    if not m:
        return None
    name = m.group(1)
    open_paren = text.find("(", m.end())
    if open_paren < 0:
        return name, []
    end = _match_paren(text, open_paren)
    if end < 0:
        return None
    body = text[open_paren + 1:end - 1]
    cols: list[str] = []
    depth, cur, entries = 0, [], []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            entries.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    entries.append("".join(cur))
    for e in entries:
        e = e.strip()
        if not e or re.match(r"(PRIMARY|FOREIGN|CONSTRAINT|CREATE|CHECK|UNIQUE"
                             r"|REFERENCES|WITH|PARTITION)\b", e, re.I):
            continue
        tok = re.match(r"[A-Za-z_][A-Za-z0-9_]*", e)
        if tok:
            cols.append(tok.group(0))
    return name, cols


def _find_list_for_name(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node.value
    return None


def create_all_tables(tree: ast.AST, class_to_table: dict[str, str]) -> list[str] | None:
    """Named tables for every Base.metadata.create_all call, or None if any call
    is not `create_all(bind=..., tables=[Model.__table__, ...])` (rule (a))."""
    names: list[str] = []
    seen = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_all"):
            continue
        seen = True
        tbl_kw = None
        for kw in node.keywords:
            if kw.arg == "tables":
                tbl_kw = kw.value
        if tbl_kw is None:
            return None
        if isinstance(tbl_kw, ast.Name):
            tbl_kw = _find_list_for_name(tree, tbl_kw.id)
        if not isinstance(tbl_kw, ast.List) or not tbl_kw.elts:
            return None
        for elt in tbl_kw.elts:
            cls = None
            if isinstance(elt, ast.Attribute) and elt.attr == "__table__":
                inner = elt.value
                if isinstance(inner, ast.Attribute):
                    cls = inner.attr
                elif isinstance(inner, ast.Name):
                    cls = inner.id
            if cls is None or cls not in class_to_table:
                return None
            names.append(class_to_table[cls])
    return names if seen else []


def classify(path: Path, tables_map, class_to_table) -> tuple[str, str, list[str]]:
    """-> (verdict, reason, created objects)."""
    src = Path(path).read_text(errors="replace")
    created: list[tuple] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return "execute", "unparseable step file", created
    lits = sql_literals(tree)

    for tok, why in ((r"_owner_role", "_owner_role (owner-role churn)"),
                     (r"\b(?:session|db|conn)\.add\b|\.addall\b|bulk_insert",
                      "ORM add (data write)")):
        if re.search(tok, src):
            return "execute", why, created

    for text in lits:
        for stmt in _split_statements(text):
            s = re.sub(r"ON\s+DELETE\s+(CASCADE|SET NULL|RETURN|NOTHING|DEFAULT)",
                       "FKONDEL", stmt, flags=re.I)
            if RE_GUARD_SELECT.match(s):
                continue  # read-only existence probe: no schema/data effect
            if not RE_ALLOWED_STMT.match(s):
                return "execute", f"statement form not allowed: {s[:70]!r}", created
            for pat, label in BANNED_SQL:
                if re.search(pat, s, re.I):
                    return "execute", f"banned construct ({label}): {s[:70]!r}", created
            tb = _table_body(s)
            if tb:
                tname, cols = tb
                created.append(("table", tname, cols))
                continue
            m = RE_COL.match(s)
            if m:
                clause_cols = RE_ALTER_CLAUSE.findall(s)
                n_add = len(re.findall(r"\bADD\s+COLUMN\b", s, re.I))
                if (not clause_cols or len(clause_cols) != n_add
                        or clause_cols[0] != m.group(2)):
                    return "execute", (f"ALTER ADD COLUMN clauses not all "
                                       f"IF-NOT-EXISTS/parseable: {s[:70]!r}"), created
                created.append(("column", m.group(1), clause_cols))
                continue
            m = RE_INDEX.search(s)
            if m:
                cols = [c.strip() for c in m.group(3).split(",") if c.strip()]
                created.append(("index", m.group(1), m.group(2), cols))
                continue
            return "execute", f"allowed-form but unparseable: {s[:70]!r}", created

    ca_tables = create_all_tables(tree, class_to_table)
    if ca_tables is None:
        return "execute", "create_all not tables=[named]", created
    for tname in ca_tables:
        if tname not in tables_map:
            return "execute", f"create_all table {tname} not in models.py", created

    for item in created:
        kind = item[0]
        tname = item[2] if kind == "index" else item[1]
        rec = tables_map.get(tname)
        if rec is None:
            return "execute", f"{tname} not in models.py", created
        cols = item[3] if kind == "index" else item[2]
        for c in cols:
            if c not in rec["cols"]:
                return "execute", f"column {tname}.{c} not in models.py", created
        if kind == "index":
            iname = item[1]
            idx_map = rec.get("idx_map") or {}
            named_ok = iname in rec["idx_names"] and (
                iname not in idx_map or idx_map[iname] == tuple(cols))
            auto_ok = (len(cols) == 1 and cols[0] in rec["idx_cols"]
                       and iname == f"ix_{tname}_{cols[0]}")
            if not (named_ok or auto_ok):
                return "execute", (f"index {iname} on {tname}({','.join(cols)}) "
                                   "name/cols not provably modelled"), created

    def describe(item) -> str:
        if item[0] == "table":
            return f"table {item[1]} cols={','.join(item[2])}"
        if item[0] == "column":
            return f"column {item[1]}.{','.join(item[2])}"
        return f"index {item[1]} on {item[2]}({','.join(item[3])})"

    if created or ca_tables:
        parts = [f"create_all {t}" for t in ca_tables] + [describe(i) for i in created]
        return "baseline", f"additive DDL only, all modelled: {'; '.join(parts)}", created
    if lits:
        return "execute", "SQL literals present but nothing recognised", created
    return "execute", "no SQL literal and no named create_all", created


def migration_chain(extra_dirs=()) -> list[Path]:
    """Core migrations dir + each extra dir (4.13f: moved paid-addon steps),
    merged and sorted by file name. A name present in two dirs is an ERROR."""
    seen: dict[str, Path] = {}
    for d in [MIGRATIONS_DIR, *map(Path, extra_dirs)]:
        for path in Path(d).glob("*.py"):
            if path.name in seen:
                raise ValueError(
                    f"duplicate migration name {path.name!r}: found in both "
                    f"{seen[path.name].parent} and {path.parent}")
            seen[path.name] = path
    return [seen[n] for n in sorted(seen)]


def generate(extra_dirs=()) -> tuple[list[tuple[str, str]], int, int]:
    """Deterministic: same tree -> same rows. -> (rows, n_execute, n_skip).
    extra_dirs: optional additional migration dirs (default none = unchanged)."""
    tables_map, class_to_table = load_models(MODELS_PY)
    skip = load_skip_names()
    rows: list[tuple[str, str]] = []
    n_exec = 0
    for path in migration_chain(extra_dirs):
        stem = path.stem
        if stem in skip:
            continue
        verdict, reason, _ = classify(path, tables_map, class_to_table)
        if verdict == "baseline":
            rows.append((stem, reason))
        else:
            n_exec += 1
    return rows, n_exec, len(skip)


def render(rows: list[tuple[str, str]]) -> str:
    lines = [
        "# fresh_install_baseline.txt — GENERATED by tools/gen_fresh_baseline.py"
        " (P18-F1a; do not hand-edit).",
        "# One step stem per line = a step whose effects create_tables() already",
        "# builds from models.py, so mod_migrate records it WITHOUT executing.",
        "# Every other step still runs. A '# reason:' line precedes each entry.",
    ]
    for stem, reason in rows:
        lines.append(f"# reason: {reason}")
        lines.append(stem)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Generate fresh_install_baseline.txt")
    ap.add_argument("--stdout", action="store_true",
                    help="print the generated file instead of writing it")
    ap.add_argument("--extra-migrations-dir", action="append", default=[],
                    metavar="DIR", dest="extra_dirs",
                    help="additional migrations dir (repeatable), merged with "
                         "the core dir")
    args = ap.parse_args(argv)
    try:
        rows, n_exec, n_skip = generate(args.extra_dirs)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.stdout:
        sys.stdout.write(render(rows))
        return 0
    OUT_FILE.write_text(render(rows))
    print(f"baseline={len(rows)} execute={n_exec} skip={n_skip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
