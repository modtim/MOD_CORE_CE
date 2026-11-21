# spreadsheet — MOD node container for the Spreadsheet (Excel/CSV) domain primitives
# (launch-completion-megaplan sz-6.4.1). One container, three operations:
#
#   SPREADSHEET_OP=spreadsheet_read     xlsx/csv  -> <stem>.rows.json + sheets.json
#   SPREADSHEET_OP=spreadsheet_write    rows JSON -> spreadsheet.xlsx | spreadsheet.csv
#   SPREADSHEET_OP=spreadsheet_formula  sheet + FORMULA -> formula.txt
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output. Parameters
# arrive as env vars from the node's configuration_schema (SHEET, MAX_CELLS,
# FORMAT, DELIMITER, ALLOW_FORMULAS, FORMULA); a fields.json staged on the
# `data` port overrides them, and for Spreadsheet Write it may carry the rows
# themselves ({"rows": [...]}).
#
# Security posture: no network access at runtime, no eval/exec (formula
# evaluation walks a whitelisted ast node set), no shell=True, inputs capped at
# 10 MB and 1M cells, .xlsm (macro-enabled Office) refused outright, openpyxl
# always opened read_only=True, workbook XML parsed through defusedxml, and a
# CSV/Excel formula-injection guard on WRITE (cells starting with = + - @ get a
# leading apostrophe unless ALLOW_FORMULAS=true). Bad input exits non-zero with
# a JSON error in /data/output/error.json.
#
# Google Sheets is OUT of scope: it needs per-tenant OAuth and a network round
# trip, and this container has no network at runtime.
#
# The `formulas` recompute package is deliberately NOT installed: it is
# unpinned and does unbounded work on adversarial sheets, so formula cells keep
# their own cached value instead.
#
# Authored only in sz-6.4.1 — build/push is a lead deploy step (no disk space
# for a build in the agent lane).

FROM python:3.11-slim

# openpyxl is the only real dependency (csv/zipfile/ast are stdlib). et_xmlfile
# is openpyxl's own requirement; defusedxml replaces stdlib ElementTree inside
# openpyxl so workbook XML cannot be used for XXE / entity-expansion.
RUN pip install --no-cache-dir \
        openpyxl==3.1.5 \
        et_xmlfile==2.0.0 \
        defusedxml==0.7.1

RUN mkdir -p /data/input /data/output /app

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync). This block is a
# verbatim mirror of ENTRYPOINT_SOURCE in
# CORE/API/node_definitions_domains/spreadsheet.py.
RUN cat > /app/spreadsheet_process.py <<'PYEOF'
#!/usr/bin/env python3
# spreadsheet entrypoint — one operation per run, selected by SPREADSHEET_OP.
#
#   SPREADSHEET_OP=spreadsheet_read     -> <input-stem>.rows.json
#   SPREADSHEET_OP=spreadsheet_write    -> spreadsheet.xlsx | spreadsheet.csv
#   SPREADSHEET_OP=spreadsheet_formula  -> formula.txt
#
# Standard MOD node I/O contract: the worker stages the node's inputs into
# /data/input and collects /data/output. Parameters arrive as env vars from the
# node's configuration_schema (SHEET, MAX_CELLS, FORMAT, ALLOW_FORMULAS,
# DELIMITER, FORMULA); a fields.json staged on the `data` port overrides them,
# and for Spreadsheet Write it may carry the rows themselves ({"rows": [...]}).
#
# Security posture: no network at runtime, no eval/exec (formula evaluation
# walks a whitelisted ast node set), no shell=True, inputs capped at 10 MB and
# 1M cells, .xlsm rejected outright, openpyxl always read_only=True, workbook
# XML parsed through defusedxml when present, CSV formula-injection guard on
# write. Bad input exits non-zero with a JSON error in /data/output/error.json.

import ast
import csv
import json
import operator
import os
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    openpyxl = None

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_CELLS = 1_000_000
MAX_ROWS = 200_000
MAX_FORMULA_LEN = 2000
MAX_COLUMNS = 16384
FORMULA_PREFIXES = ("=", "+", "-", "@")
SUPPORTED_FUNCTIONS = ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "PRODUCT")
REF_RE = re.compile(r"\$?[A-Za-z]{1,3}\$?[0-9]{1,7}")
RANGE_RE = re.compile(r"\$?[A-Za-z]{1,3}\$?[0-9]{1,7}:\$?[A-Za-z]{1,3}\$?[0-9]{1,7}")
def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(json.dumps({"error": str(msg)}, sort_keys=True), encoding="utf-8")


def fail(msg, code="INTERNAL"):
    # vet step 5: one line of JSON on stdout, then non-zero exit. Keep writing
    # the error.json sidecar other callers (and the golden-path test) expect.
    print(json.dumps({"error": str(msg), "code": code}, sort_keys=True), flush=True)
    _write_error(msg)
    sys.exit(1)


def _all_inputs():
    out = []
    for f in sorted(IN.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            size = f.stat().st_size
            if size > MAX_INPUT_BYTES:
                raise ValueError(
                    f"{f.name} is {size} bytes — over the {MAX_INPUT_BYTES}-byte input cap"
                )
            out.append(f)
    return out


def _sheets(paths):
    # Filter staged files down to sheets, refusing macro-bearing formats.
    out = []
    for f in paths:
        suffix = f.suffix.lower()
        if suffix == ".xlsm":
            # Macro-enabled Office. openpyxl never executes VBA, but a hard
            # refusal means a macro-bearing workbook can never be read as if
            # it were a plain sheet.
            raise ValueError(f"{f.name} is .xlsm (macro-enabled Office) — refused; re-save as .xlsx")
        if suffix in (".xlsx", ".csv"):
            out.append(f)
    return out


def _verify_defused_xml():
    # openpyxl auto-switches its own XML parser to defusedxml at IMPORT time
    # (openpyxl.xml.DEFUSEDXML), provided lxml is not also installed; this
    # container's image pins no lxml, so the switch is already active by the
    # time this module loads — there is no handle left for us to patch here.
    # Fail loudly instead of silently falling back to stdlib ElementTree if
    # that ever drifts (e.g. a future image rebuild adds lxml as a transitive
    # dependency of some other package).
    if openpyxl is None:
        return
    if not getattr(getattr(openpyxl, "xml", None), "DEFUSEDXML", False):
        raise ValueError(
            "openpyxl is not using defusedxml for XML parsing (lxml present, "
            "or OPENPYXL_DEFUSEDXML=False) — refusing to parse untrusted "
            "workbook XML"
        )


def _preflight_xlsx(path):
    # Reject entity bombs / zip bombs before openpyxl ever parses the archive.
    import zipfile

    if not zipfile.is_zipfile(path):
        raise ValueError(f"{path.name} is not a valid .xlsx container (an .xlsx is a zip)")
    with zipfile.ZipFile(path) as zf:
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if total > 50 * 1024 * 1024:
                raise ValueError(f"{path.name} unpacks to over 50 MB — refused (zip bomb)")
            if info.file_size and info.compress_size and info.file_size > 200 * info.compress_size:
                raise ValueError(f"{path.name} has a {info.file_size // info.compress_size}x compression ratio — refused")
            if info.filename.lower().endswith(".xml"):
                raw = zf.read(info.filename)
                if b"<!ENTITY" in raw or b"<!DOCTYPE" in raw:
                    raise ValueError(f"{path.name} declares XML entities — refused (entity-expansion risk)")


def _params():
    # Node parameters: env vars, overridden by fields.json on the `data` port.
    out = {}
    p = IN / "fields.json"
    if p.is_file():
        raw = p.read_bytes()
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("fields.json exceeds the 10 MB input cap")
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            out = {str(k).strip().lower(): v for k, v in data.items()}
    params = {}
    for key in ("sheet", "max_cells", "format", "allow_formulas", "delimiter", "formula"):
        params[key] = out.get(key, os.getenv(key.upper(), ""))
    return params


def _read_sheet(path, sheet_name, cell_cap):
    # Return the rows of one sheet (header row first) plus the sheet names.
    if path.suffix.lower() == ".csv":
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        delim = ","
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter or ","
        except csv.Error:
            pass
        rows, cells = [], 0
        for row in csv.reader(text.splitlines(), delimiter=delim):
            cells += len(row)
            if cells > cell_cap:
                raise ValueError(f"sheet exceeds the {cell_cap}-cell cap")
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise ValueError(f"sheet exceeds the {MAX_ROWS}-row cap")
        return rows, [path.name]

    if openpyxl is None:
        raise ValueError("openpyxl is not installed in this container — .xlsx cannot be read")
    _preflight_xlsx(path)
    _verify_defused_xml()
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True, keep_links=False)
    try:
        names = list(wb.sheetnames)
        if sheet_name:
            if sheet_name not in names:
                raise ValueError(f"SHEET {sheet_name!r} not in workbook; available: {', '.join(names)}")
            target = wb[sheet_name]
        else:
            target = wb[names[0]]
        rows, cells = [], 0
        for row in target.iter_rows(values_only=True):
            cells += len(row)
            if cells > cell_cap:
                raise ValueError(f"sheet exceeds the {cell_cap}-cell cap")
            rows.append(["" if v is None else v for v in row])
            if len(rows) > MAX_ROWS:
                raise ValueError(f"sheet exceeds the {MAX_ROWS}-row cap")
        return rows, names
    finally:
        wb.close()


def _header_rows(rows):
    # Key each data row by its header cell; blank/duplicate headers become col_N.
    if not rows:
        return []
    header, seen = [], set()
    for i, raw in enumerate(rows[0]):
        key = str(raw).strip()
        if not key or key in seen:
            key = f"col_{i + 1}"
        seen.add(key)
        header.append(key)
    out = []
    for values in rows[1:]:
        out.append({header[i]: values[i] for i in range(min(len(header), len(values)))})
    return out


# ---------------------------------------------------------------------------
# Cell/formula machinery — no eval, no exec: a whitelisted ast walk.
# ---------------------------------------------------------------------------


def _col_row(ref):
    # 'B7' -> (col_index_1based, row_index_1based).
    m = REF_RE.fullmatch(ref)
    if not m:
        raise ValueError(f"not a cell reference: {ref!r}")
    letters = "".join(ch for ch in ref if ch.isalpha())
    digits = "".join(ch for ch in ref if ch.isdigit())
    col = 0
    for ch in letters.upper():
        col = col * 26 + (ord(ch) - 64)
    return col, int(digits)


def _cell_value(grid, ref):
    col, row = _col_row(ref)
    if row > len(grid) or col > MAX_COLUMNS:
        return None
    line = grid[row - 1]
    return line[col - 1] if col <= len(line) else None


def _range_values(grid, ref):
    # 'B2:D4' -> flat list of the values inside the range.
    m = RANGE_RE.fullmatch(ref)
    if not m:
        raise ValueError(f"not a cell range: {ref!r}")
    a, b = ref.split(":")
    c1, r1 = _col_row(a)
    c2, r2 = _col_row(b)
    if c2 < c1:
        c1, c2 = c2, c1
    if r2 < r1:
        r1, r2 = r2, r1
    if r2 - r1 > 100000 or (c2 - c1 + 1) * (r2 - r1 + 1) > MAX_CELLS:
        raise ValueError("range is larger than the cell cap")
    vals = []
    for r in range(r1, r2 + 1):
        line = grid[r - 1] if r <= len(grid) else []
        for c in range(c1, c2 + 1):
            vals.append(line[c - 1] if c <= len(line) else None)
    return vals


def _numbers(values):
    out = []
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
        else:
            try:
                out.append(float(str(v).strip()))
            except ValueError:
                pass
    return out


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _evaluate(node, grid, ranges):
    # Evaluate one parsed formula node. Anything off this whitelist raises.
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, grid, ranges)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node.value, str):
            return node.value
        raise ValueError("formula constants must be numbers or text")
    if isinstance(node, ast.Name):
        return _cell_value(grid, node.id)
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINOPS:
            raise ValueError("unsupported operator in formula")
        left = _evaluate(node.left, grid, ranges)
        right = _evaluate(node.right, grid, ranges)
        if isinstance(node.op, ast.Pow) and abs(right) > 64:
            raise ValueError("exponent over 64 refused")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY:
            raise ValueError("unsupported unary operator")
        return _UNARY[type(node.op)](_evaluate(node.operand, grid, ranges))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only plain function calls are allowed")
        name = node.func.id.upper()
        if name not in SUPPORTED_FUNCTIONS:
            raise ValueError(
                f"function {name} is not in the supported set ({', '.join(SUPPORTED_FUNCTIONS)})"
            )
        if len(node.args) != 1 or node.keywords:
            raise ValueError(f"{name} takes exactly one argument")
        arg = node.args[0]
        if isinstance(arg, ast.Name) and arg.id in ranges:
            values = ranges[arg.id]
        elif isinstance(arg, ast.Name) and RANGE_RE.fullmatch(arg.id):
            values = _range_values(grid, arg.id)
        elif isinstance(arg, ast.Name):
            values = [_cell_value(grid, arg.id)]
        else:
            values = [_evaluate(arg, grid, ranges)]
        nums = _numbers(values)
        if name == "COUNT":
            return float(len(nums))
        if name == "COUNTA":
            return float(len([v for v in values if v not in (None, "")]))
        if not nums:
            return 0.0 if name in ("SUM", "COUNT", "COUNTA", "PRODUCT") else None
        if name == "SUM":
            return math_sum(nums)
        if name == "AVERAGE":
            return math_sum(nums) / len(nums)
        if name == "MIN":
            return min(nums)
        if name == "MAX":
            return max(nums)
        if name == "PRODUCT":
            acc = 1.0
            for v in nums:
                acc *= v
            return acc
    raise ValueError("unsupported formula element")


def math_sum(values):
    total = 0.0
    for v in values:
        total += v
    return total


def _parse_formula(text):
    # Parse an Excel-ish formula into a whitelisted ast tree.

# Ranges are pre-extracted (ast cannot spell `A1:B9`) and handed to _evaluate
# as a pre-computed list keyed by the upper-cased reference.
    body = text.strip()
    if body.startswith("="):
        body = body[1:]
    if not body:
        raise ValueError("empty formula")
    if len(body) > MAX_FORMULA_LEN:
        raise ValueError(f"formula over {MAX_FORMULA_LEN} characters")
    def _stash(match):
        key = "RNG" + str(len(ranges))
        ranges[key] = match.group(0).upper().replace("$", "")
        return key

    ranges = {}
    body = RANGE_RE.sub(_stash, body)
    tree = ast.parse(body, mode="eval")

    return tree, ranges


def _guard_cell(value, allow_formulas):
    # CSV/Excel formula-injection guard for WRITTEN cells.
    text = "" if value is None else str(value)
    if text and text[0] in FORMULA_PREFIXES:
        if allow_formulas:
            return text
        return "'" + text
    return text


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def op_read(paths, params):
    # xlsx/csv -> <stem>.rows.json (header-keyed rows) + sheets.json.
    if not paths:
        raise ValueError("no .xlsx or .csv found on the input port")
    cap = _cap(params.get("max_cells"), MAX_CELLS)
    sheet_name = str(params.get("sheet") or "").strip()
    seen_names = []
    for path in paths:
        rows, names = _read_sheet(path, sheet_name, cap)
        for n in names:
            if n not in seen_names:
                seen_names.append(n)
        out_name = f"{path.name.rsplit('.', 1)[0]}.rows.json"
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / out_name).write_text(
            json.dumps(_header_rows(rows), default=str, indent=1), encoding="utf-8"
        )
        _log(f"read {path.name} -> {out_name} ({max(0, len(rows) - 1)} rows)")
    _write_text("sheets.json", json.dumps({"sheets": seen_names}))


def op_write(paths, params):
    # rows JSON -> spreadsheet.xlsx (default) or spreadsheet.csv. Rows are found
    # among ALL staged inputs (a .json staged by Spreadsheet Read), not just the
    # sheet files, so _all_inputs() is used here instead of the sheet filter.
    rows = _rows_from(_all_inputs())
    if not rows:
        raise ValueError("no rows to write: stage a rows JSON on the input port or in fields.json")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"{len(rows)} rows over the {MAX_ROWS}-row cap")
    fmt = str(params.get("format") or "xlsx").strip().lower()
    allow = str(params.get("allow_formulas") or "").strip().lower() in ("true", "1", "yes")
    headers = list(rows[0].keys())
    for key in headers:
        if len(str(key)) > 200:
            raise ValueError("header over 200 characters")
    if len(headers) > MAX_COLUMNS:
        raise ValueError(f"{len(headers)} columns over the {MAX_COLUMNS}-column cap")
    grid = [headers] + [[r.get(k) for k in headers] for r in rows]
    grid = [[_guard_cell(v, allow) for v in line] for line in grid]
    if fmt == "csv":
        delim = params.get("delimiter") or ","
        if len(delim) != 1:
            raise ValueError("DELIMITER must be a single character")
        OUT.mkdir(parents=True, exist_ok=True)
        with open(OUT / "spreadsheet.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh, delimiter=delim).writerows(grid)
        _log(f"wrote {len(rows)} rows -> spreadsheet.csv")
        return
    if fmt != "xlsx":
        raise ValueError(f"FORMAT {fmt!r} not supported (xlsx|csv)")
    if openpyxl is None:
        raise ValueError("openpyxl is not installed — cannot write .xlsx")
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    for line in grid:
        ws.append(line)
    wb.save(str(OUT / "spreadsheet.xlsx"))
    _log(f"wrote {len(rows)} rows -> spreadsheet.xlsx")


def _rows_from(paths):
    # Rows from fields.json (`rows`), or from a staged rows-JSON file. Rows may
    # be objects or bare lists (first list = the header row).
    candidates = []
    p = IN / "fields.json"
    if p.is_file():
        try:
            candidates.append(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, UnicodeError):
            pass
    for path in paths:
        if path.suffix.lower() != ".json" or path.name == "fields.json":
            continue
        try:
            candidates.append(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, UnicodeError):
            continue
    for data in candidates:
        if isinstance(data, dict):
            data = data.get("rows")
        if not isinstance(data, list) or not data:
            continue
        if all(isinstance(r, dict) for r in data):
            return data
        if all(isinstance(r, list) for r in data):
            header = [str(c) for c in data[0]]
            return [
                {header[i]: line[i] for i in range(min(len(header), len(line)))}
                for line in data[1:]
            ]
    return []


def op_formula(paths, params):
    # Evaluate one whitelisted formula against the sheet's cached values.
    expr = str(params.get("formula") or "").strip()
    if not expr:
        raise ValueError("FORMULA parameter is required for Spreadsheet Formula")
    if not paths:
        raise ValueError("no .xlsx or .csv found on the input port")
    cap = _cap(params.get("max_cells"), MAX_CELLS)
    rows, _ = _read_sheet(paths[0], str(params.get("sheet") or "").strip(), cap)
    grid = [[v for v in line] for line in rows]
    tree, range_names = _parse_formula(expr)
    ranges = {name: _range_values(grid, ref) for name, ref in range_names.items()}
    value = _evaluate(tree, grid, ranges)
    if value is None:
        rendered = ""
    elif isinstance(value, float) and value == int(value):
        rendered = str(int(value))
    else:
        rendered = str(value)
    _write_text("formula.txt", rendered)
    _log(f"formula {expr!r} = {rendered!r}")


def _cap(raw, default):
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError("MAX_CELLS must be an integer")
    if value < 1:
        raise ValueError("MAX_CELLS must be positive")
    return min(value, default)


OPS = {"read": op_read, "write": op_write, "formula": op_formula}


def main():
    op = os.getenv("SPREADSHEET_OP", "read").strip().lower()
    # The spec default is the node's own op key ("spreadsheet_read"); the
    # container speaks the short name. Accept either spelling.
    if op not in OPS and op.startswith("spreadsheet_"):
        op = op[len("spreadsheet_"):]
    if op not in OPS:
        _log(f"ERROR: unknown SPREADSHEET_OP {op!r}")
        fail(f"unknown SPREADSHEET_OP {op!r}", "BAD_PARAM")
    try:
        params = _params()
        paths = _sheets(_all_inputs())
        _log(f"=== spreadsheet: {op} ({len(paths)} sheet) ===")
        OPS[op](paths, params)
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        _log(f"ERROR: {exc}")
        fail(f"{type(exc).__name__}: {exc}", "INTERNAL")


if __name__ == "__main__":
    main()
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/spreadsheet_process.py"]
