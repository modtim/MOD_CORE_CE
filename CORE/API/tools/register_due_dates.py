#!/usr/bin/env python3
"""remediation-SLA due dates for the GSEC register.

Parses docs/security-audit/REGISTER.md (severity from the section header,
found/confirmed date from the first ISO date in the Status cell) and computes
a remediation due date per OPEN item per docs/security-audit/REMEDIATION_SLA_POLICY.md:

    Critical 7 days (CISA KEV: 7) · High 30 · Medium 90 · Low none (fixed in the
    next release) · Info none

counted from the found/confirmed date. A risk-acceptance exception is a
Notes-column sign-off `ACCEPTED YYYY-MM-DD` (expiry date); an acceptance whose
expiry has passed is NOT an exception. FIXED rows are remediated; anything
else (VERIFIED / PARTIAL / OPEN / as-reported / TRIAGED) is open.

Usage:
    python -m tools.register_due_dates                     # report only
    python -m tools.register_due_dates --write             # fill/update a Due column
    python -m tools.register_due_dates --register PATH --today YYYY-MM-DD
    python -m tools.register_due_dates --kev-ids R-001,R-002

`--write` rewrites the register in place (idempotent: the Due column is
recomputed every run). The report prints open items sorted by days-overdue
and a summary; exit code is always 0 (this is a report tool — the
conformance check vuln.remediation_overdue is the enforcement surface).
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

API_ROOT = Path(__file__).resolve().parent.parent            # CORE/API


def _repo_root() -> Path:
    """Repo root (the ancestor whose CORE/API == API_ROOT); in the flat
    container layout the API dir itself is the basis (crypto_inventory.py
    pattern)."""
    for ancestor in API_ROOT.parents:
        if (ancestor / "CORE" / "API").resolve() == API_ROOT:
            return ancestor
    return API_ROOT


REPO_ROOT = _repo_root()
REGISTER_DEFAULT = REPO_ROOT / "docs" / "security-audit" / "REGISTER.md"

#: SLA windows in days, keyed by severity (REMEDIATION_SLA_POLICY.md §2).
SLA_DAYS: Dict[str, int] = {"critical": 7, "high": 30, "medium": 90, "low": 0}
KEV_CRITICAL_DAYS = 7

_SEVERITY_HEADER = re.compile(r"^##\s+(Critical|High|Medium|Low|Info)\b", re.I)
_ROW = re.compile(r"^\|\s*R-\d+\s*\|")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_ACCEPTED = re.compile(r"\bACCEPTED\s+(\d{4}-\d{2}-\d{2})\b", re.I)
_PIPE_SPLIT = re.compile(r"(?<!\\)\|")  # split on unescaped pipes only


def _now() -> datetime:
    """The clock, named so tests can pin a fixed today (the check and the
    weekly report call `parse_register` without a `today`, so this is the
    single date source they go through)."""
    return datetime.now(timezone.utc)


def _parse_date(raw: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _cells(line: str) -> List[str]:
    r"""Split a markdown table row on UNESCAPED pipes only: register titles
    carry literal `\|` (e.g. `pg_dump \| gzip`), which a plain split() would
    turn into a phantom column and shift every later cell."""
    return [c.strip() for c in _PIPE_SPLIT.split(line.strip().strip("|"))]


def _is_open(status_cell: str) -> bool:
    """FIXED (any variant: FIXED <sha>, FIXED-INERT, FIXED in code) is
    remediated; everything else is open."""
    return not status_cell.strip().upper().startswith("FIXED")


def parse_register(text: str, *, kev_ids: Optional[set] = None,
                   today: Optional[datetime] = None,
                   sla_days: Optional[Dict[str, int]] = None,
                   kev_critical_days: Optional[int] = None) -> List[Dict]:
    """One dict per register row: id, severity, status, open, found,
    accepted_until, sla_days, due (datetime|None), overdue (bool),
    days_overdue. Rows without a parseable found date get due=None
    (reported, never assumed). ``sla_days``/``kev_critical_days`` override
    the policy defaults with the platform settings of the same name
    (vuln.sla_days_*), so an owner can retune a window without a code
    change; 0 for a severity means no due date for it."""
    kev_ids = set(kev_ids or ())
    today = today or _now()
    sla_map = dict(SLA_DAYS)
    for sev, key in (("critical", "vuln.sla_days_critical"),
                     ("high", "vuln.sla_days_high"),
                     ("medium", "vuln.sla_days_medium"),
                     ("low", "vuln.sla_days_low")):
        if sla_days is not None and sev in sla_days:
            sla_map[sev] = sla_days[sev]
    kev_days = (kev_critical_days if kev_critical_days is not None
                else KEV_CRITICAL_DAYS)
    rows: List[Dict] = []
    severity: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("## "):  # level-2 header closes the previous section
            sev = _SEVERITY_HEADER.match(line)
            severity = sev.group(1).lower() if sev else None
            continue
        if not _ROW.match(line):
            continue
        cells = _cells(line)
        if len(cells) < 7:
            continue
        rid, status, notes = cells[0], cells[5], cells[6]
        found_m = _DATE.search(status)
        found = _parse_date(found_m.group(1)) if found_m else None
        acc_m = _ACCEPTED.search(notes)
        accepted_until = _parse_date(acc_m.group(1)) if acc_m else None
        sla = sla_map.get(severity or "")
        if severity == "critical" and (rid in kev_ids or "KEV" in notes):
            sla = kev_days
        due = found + timedelta(days=sla) if (found and sla) else None
        open_ = _is_open(status)
        unexpired = accepted_until is not None and accepted_until >= today
        overdue = bool(open_ and due and today > due and not unexpired)
        rows.append({
            "id": rid,
            "severity": severity,
            "status": status,
            "open": open_,
            "found": found,
            "accepted_until": accepted_until,
            "sla_days": sla,
            "due": due,
            "overdue": overdue,
            "days_overdue": int((today - due).days) if overdue else 0,
        })
    return rows


def _due_cell(row: Dict) -> str:
    if not row["open"]:
        return "—"
    if row["due"] is None:
        return "no-found-date"
    return row["due"].strftime("%Y-%m-%d")


def write_register(text: str, rows_by_id: Dict[str, Dict]) -> str:
    """Fill/update a `Due` column (the LAST column of every register table).
    Idempotent: a table whose header already ends in `Due` gets its rows'
    last cell rewritten; otherwise the column is appended."""
    def _append_cell(line: str, cell: str) -> str:
        """Insert ``cell`` as a new last column before the trailing pipe."""
        return line.rstrip()[:-1].rstrip() + f" | {cell} |"

    out: List[str] = []
    has_due: Optional[bool] = None
    for line in text.splitlines():
        if line.startswith("| Reg-ID"):
            has_due = _cells(line)[-1] == "Due"
            out.append(line if has_due else _append_cell(line, "Due"))
            continue
        if has_due is not None and line.startswith("|---"):
            out.append(line if has_due else _append_cell(line, "---"))
            continue
        if has_due is not None and _ROW.match(line):
            rid = _cells(line)[0]
            due = _due_cell(rows_by_id[rid]) if rid in rows_by_id else "—"
            if has_due:
                cells = _cells(line)
                cells[-1] = due
                out.append("| " + " | ".join(cells) + " |")
            else:
                out.append(_append_cell(line, due))
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--register", default=str(REGISTER_DEFAULT))
    p.add_argument("--today", default=None, help="YYYY-MM-DD clock override")
    p.add_argument("--kev-ids", default="", help="comma-separated KEV row ids")
    p.add_argument("--write", action="store_true",
                   help="fill/update the Due column in the register file")
    args = p.parse_args(argv)
    path = Path(args.register)
    text = path.read_text(encoding="utf-8")
    today = _parse_date(args.today) if args.today else None
    kev = {s.strip() for s in args.kev_ids.split(",") if s.strip()}
    rows = parse_register(text, kev_ids=kev, today=today)
    by_id = {r["id"]: r for r in rows}

    overdue = sorted((r for r in rows if r["overdue"]),
                     key=lambda r: -r["days_overdue"])
    print(f"register: {path} · rows={len(rows)} · open="
          f"{sum(1 for r in rows if r['open'])} · overdue={len(overdue)}")
    for r in overdue:
        print(f"  OVERDUE {r['id']} [{r['severity']}] found "
              f"{r['found'].strftime('%Y-%m-%d')} due {r['due'].strftime('%Y-%m-%d')} "
              f"+{r['days_overdue']}d")
    if args.write:
        path.write_text(write_register(text, by_id), encoding="utf-8")
        print(f"written: {path} (Due column)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
