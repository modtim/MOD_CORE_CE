#!/usr/bin/env python3
"""Acceptance gate: scan a produced public tree for secrets and leaked paths.

Exits non-zero (blocking publication) if any denylisted path survived the
extraction or any known secret/identifier pattern is present. Run this against
a built public tree (the CI workflows run it on the repo root) before pushing to
the public remote.

Usage:
    python3 tools/secret_scan.py --tree <dir>
        [--fixture-allowlist tools/ce_preflight_fixture_allowlist.yaml]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse

# Paths that must never appear in the public tree (substring match on relpath).
FORBIDDEN_PATHS = [
    "CORE/LicenseServer/",
    "StripeWebhookForwarder/",
    "registry-certs/",
    ".env.secrets",
    ".env.public.example",
    "/.license/",
    "license_key",
    "installation_id",
    "Dockerfile.protected",
    "CORE/API/routers/billing.py",
    "CORE/API/utils/stripe_api.py",
    "vault-init.sh",
    "render-env-from-vault.sh",
    # Internal-only docs / planning / QA / agent config (Gate 2b). These are
    # denied in manifest.yaml; this is the belt-and-suspenders acceptance gate.
    "CLAUDE.md",
    "/.claude/",
    "/.continue/",
    "/.idea/",
    "/.planning/",
    "api-audit/",
    "openspec/",
    "GO_TO_MARKET_PLAN",
    "MASTER_TEST_PLAN",
    "DEMO_QA_TRACKER",
    "BILLING_SYSTEM.md",
    "AEC_PROJECT.md",
    "COPY_REWRITE.md",
    "IMPLEMENTATION_COMPLETE",
    "IMPLEMENTATION_STATUS",
    "SECURITY_AUDIT.md",
    "SECURITY_ROADMAP.md",
    "SAAS_LAUNCH_READINESS",
    "STAGE_VERIFICATION_QUEUE",
    "SESSION_FIX_REPORT",
    "QA_RESULTS_",
    "QA_MANUAL_TESTING",
    "QA_MASTER_SCORECARD",
    "QA_RETEST",
    "QA_REMAINING",
    "QA_TEST_PLAN",
    "FUTURE_",
    "HANDOFF",
    "_PLAN.md",
]

# The public tree runs THIS file as its own gate, so the scanner and the
# fixture allowlist it loads are part of the tree they police. Their source
# necessarily contains the patterns they detect (the org-domain regexes, the
# RFC 1918 CIDR constants, the personal-identifier regex, and the allowlist's
# `reason:` prose quoting scanned addresses), which would otherwise be reported
# as findings in the gate's own files. Self-exclusion is by EXACT relpath of the
# shipped overlay targets only (never a glob, never a directory): content checks
# are skipped on the gate's own two files, every other path in the tree stays
# fully scanned, and these two files are still covered by FORBIDDEN_PATHS.
SELF_SCAN_EXCLUDES = frozenset({
    "tools/secret_scan.py",
    "tools/ce_preflight_fixture_allowlist.yaml",
})

# --- modtechlabs.com: two different signals share this substring -----------
#
# 1. An INTERNAL SUBDOMAIN — <label>.modtechlabs.com, e.g. dev-api.,
#    dev-s3., dev-dashboard., dev-workflows., dev-keycloak., minio., s3.,
#    airflow., license., harbor., mod-headscale. — names a real internal
#    host/service and must NEVER ship, in ANY file. A subdomain is
#    recognized by a label (`[a-z0-9-]+`) immediately followed by a literal
#    `.` immediately before `modtechlabs.com`.
INTERNAL_SUBDOMAIN_RX = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.modtechlabs\.com", re.IGNORECASE)
#
# 2. The BARE org domain (modtechlabs.com with no subdomain label) and org
#    contact addresses (<local-part>@modtechlabs.com — security@,
#    trademark@, conduct@, etc.) are the project's PUBLIC identity.
#    They are REQUIRED in the governance/overlay doc set (LICENSE, NOTICE,
#    README, SECURITY.md, TRADEMARK.md, ...) and must NOT be flagged there —
#    that would block every CE build on the project's own public contact
#    info. Everywhere else (code/config), the same string has no business
#    being hardcoded, so it's still flagged. This is checked separately,
#    per-file, in main() — see GOVERNANCE_DOC_ALLOWLIST below — rather than
#    as an unconditional entry in SECRET_PATTERNS.
BARE_ORG_DOMAIN_RX = re.compile(
    r"(?<![a-z0-9.-])modtechlabs\.com", re.IGNORECASE)

# Relative paths (forward-slash, as produced by os.path.relpath under the
# scanned tree) that ARE the project's public-facing governance identity —
# the bare org domain / org contact email is expected and allowed only here.
GOVERNANCE_DOC_ALLOWLIST = {
    "LICENSE",
    "NOTICE",
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "GOVERNANCE.md",
    "MAINTAINERS.md",
    "TRADEMARK.md",
    "SYNC.md",
    "docs/sbom.md",
    "docs/assets/presentation.config.yaml",
}

# admin@ is the single public contact
# The four tokens CE must show to the public. This is an EXACT-match allow only
# — not a domain-wide allowance and not path-based: any other @modtechlabs.com
# address or *.modtechlabs.com host (including an allowlisted name with a
# suffix, e.g. docs.modtechlabs.com.evil.io) still fails the gate.
PUBLIC_CONTACT_ALLOWLIST = frozenset({
    "admin@modtechlabs.com",
    "security@modtechlabs.com",
    "legal@modtechlabs.com",
    "docs.modtechlabs.com",
})

# A host/contact token is longer than the regex match itself: for
# `docs.modtechlabs.com` the lookbehind in BARE_ORG_DOMAIN_RX starts the match
# at `modtechlabs.com`, so the exact-match check has to compare the WHOLE token.
# Trailing sentence punctuation is not part of the host.
_TOKEN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.@_-")


def _contact_token(data: str, mo) -> str:
    """The full host/address token containing a BARE_ORG_DOMAIN_RX match."""
    lo, hi = mo.start(), mo.end()
    while lo > 0 and data[lo - 1] in _TOKEN_CHARS:
        lo -= 1
    while hi < len(data) and data[hi] in _TOKEN_CHARS:
        hi += 1
    return data[lo:hi].rstrip(".-")


def _is_public_contact(token: str) -> bool:
    """Public contacts shown in CE — exact match only."""
    return token.lower() in PUBLIC_CONTACT_ALLOWLIST

# --- RFC 1918, loopback and link-local CIDR constants (range names, not hosts)
#
# Trusted-proxy / private-net / egress classifiers (CORE/Worker/internal/*,
# CORE/API/{services,utils}/) name the private ranges as CIDR CONSTANTS —
# netip.MustParsePrefix("192.168.0.0/16"), ipaddress.ip_network("10.0.0.0/8").
# A network base whose host bits are all zero is the NAME of a range: no client
# is ever served from it and it identifies nothing, so it carries no leak —
# unlike a concrete address inside the range (192.168.1.5, 192.168.1.5:8080),
# which is still flagged. EXACT-MATCH ONLY: the dotted quad plus its `/bits`
# suffix must equal one of these five strings. Fail-closed — a range with
# non-zero host bits, wrong prefix bits, a short form, trailing characters
# after the suffix, or a dotted/path context (a hostname label glued in front
# of the range) is still a finding.
RFC1918_NETWORK_CIDRS = frozenset({
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
})

# Context that would make the match part of a longer token (a host in a URL or
# path, an address with a port, a hostname suffix) rather than a bare constant.
_IP_CTX_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/")


def _is_rfc1918_network(data: str, mo) -> bool:
    """True only for an exact RFC1918/loopback/link-local CIDR constant."""
    tok = data[mo.start():mo.end()]
    hi = mo.end()
    if hi < len(data) and data[hi] == "/":
        j = hi + 1
        while j < len(data) and data[j].isdigit():
            j += 1
        if j == hi + 1:
            return False  # '/' with no prefix length: not a CIDR constant
        tok = data[mo.start():j]
        hi = j
    if tok not in RFC1918_NETWORK_CIDRS:
        return False
    lo = mo.start()
    if lo > 0 and data[lo - 1] in _IP_CTX_CHARS:
        return False
    if hi < len(data) and data[hi] in _IP_CTX_CHARS:
        return False
    return True


# Secret / identifier content patterns. Any match FAILS the build (blocks push).
SECRET_PATTERNS = [
    # Require actual key material after the header, not just the bare marker —
    # the Dashboard's own settings UI uses the header text as a <textarea>
    # placeholder (the bare PEM header line) to show users the expected
    # format; that must not trip this gate.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[ \t]*(?:\\n|\r?\n)+"
                r"[A-Za-z0-9+/=]{16,}"), "private key header"),
    (re.compile(r"\bsk_live_[0-9A-Za-z]+"), "stripe live key"),
    (re.compile(r"\bsk_test_[0-9A-Za-z]{10,}"), "stripe test key"),
    (re.compile(r"\bwhsec_[0-9A-Za-z]+"), "stripe webhook secret"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key"),
    # Require a real, long token body. A documentation placeholder such as
    # "TRIAL-eyJwYXlsb2FkIjoi..." (truncated with a literal "...") is well
    # short of a real embedded blob's continuous length, so it is not flagged.
    (re.compile(r"\bTRIAL-eyJ[A-Za-z0-9+/_.=-]{40,}"), "embedded token blob"),
    # PII / internal host identifiers (Gate 2b — must be scrubbed at extraction).
    # Internal subdomains are denied everywhere, unconditionally. The bare
    # org domain / org email is handled conditionally in main() (see
    # BARE_ORG_DOMAIN_RX / GOVERNANCE_DOC_ALLOWLIST above), not here.
    (INTERNAL_SUBDOMAIN_RX, "internal infra domain (subdomain)"),
    # A personal address leak (a personal mailbox) is still flagged
    # everywhere, including governance docs. The personal-identifier rule
    # is the project's public maintainer contact (MAINTAINERS.md) — that's an
    # org-email case, covered by the governance-doc-conditional check above,
    # not this unconditional one.
    (re.compile(r"\btim@(?!modtechlabs\.com\b)"), "personal identifier"),
    # Private / LAN IPv4 ranges (should be scrubbed to RFC 5737 doc ranges).
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "private IP (192.168/16)"),
    (re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
     "private IP (172.16/12)"),
    (re.compile(r"\b38\.101\.44\.85\b"), "internal public/LAN IP"),
    # DERP / Headscale / mesh internal endpoints (host part scrubbed above; this
    # catches an internal mesh host that slipped through with a raw domain).
    (re.compile(r"\b(?:derp|headscale|mesh)[0-9]*\.(?:internal|lan|mod)\b"),
     "internal mesh endpoint"),
    # Reused / hardcoded credential pattern: a bcrypt hash is a real credential
    # artifact (not a placeholder) and must never ship.
    (re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"), "bcrypt password hash"),
]

TEXT_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
            ".md", ".txt", ".sh", ".env", ".html", ".css", ".toml", ".cfg",
            ".ini", ".conf", ".go", ".mod", ""}

# --- fixture allowlist (SECRET_PATTERNS only) ---

# Compiled allowlist entries: list of {path, pattern_only?, reason}.
# `reason` is optional documentation only: the gate never reads or enforces it.
# `path` (non-empty str) and `pattern_only` (str) ARE enforced at load time.
_fixture_allowlist: list[dict] | None = None


class KnownSecretsUnavailable(Exception):
    """The reused-credential sidecar exists but cannot be read (rc 2).

    The message names the path only, never any content of the file."""


class FixtureAllowlistUnavailable(Exception):
    """Raised when an allowlist was REQUESTED but could not be loaded.

    The old code swallowed this into `[]`, so a tree scanned
    without PyYAML (python:3.12-slim) silently produced findings instead of
    the allowlisted clean pass.  Failing safe is right but confusing, so an
    explicitly-requested allowlist must now fail loud (rc 2, see main()).
    """


def load_fixture_allowlist(path: str | None = None) -> list[dict]:
    """Load the YAML fixture allowlist.  Returns [] if none was requested.

    Raises FixtureAllowlistUnavailable if an allowlist WAS requested
    (--fixture-allowlist / CE_PREFLIGHT_FIXTURE_ALLOWLIST) but the path is
    missing or PyYAML is not installed.
    """
    global _fixture_allowlist
    if _fixture_allowlist is not None:
        return _fixture_allowlist
    allowlist_path = path or os.environ.get(
        "CE_PREFLIGHT_FIXTURE_ALLOWLIST", "")
    if not allowlist_path:
        # Nothing requested: behaviour unchanged.
        _fixture_allowlist = []
        return _fixture_allowlist
    if not os.path.isfile(allowlist_path):
        raise FixtureAllowlistUnavailable(
            f"fixture allowlist requested but path not found: {allowlist_path}")
    try:
        import yaml
    except ImportError:
        raise FixtureAllowlistUnavailable(
            "fixture allowlist requested but PyYAML not installed: "
            "pip install pyyaml")
    try:
        with open(allowlist_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        # A malformed allowlist means "cannot evaluate" (rc 2),
        # exactly like a missing path or missing PyYAML.  It must never fall
        # through as an empty allowlist (which would turn known fixtures into
        # findings, rc 1, indistinguishable from a real hit).
        raise FixtureAllowlistUnavailable(
            f"fixture allowlist requested but YAML is malformed in "
            f"{allowlist_path}: {exc}"
        )
    # A requested allowlist that is not a YAML list (dict, scalar, empty file)
    # is "cannot evaluate" (rc 2), never a silent empty allowlist.
    if not isinstance(data, list):
        raise FixtureAllowlistUnavailable(
            f"fixture allowlist requested but {allowlist_path} must be a "
            f"YAML list of entries"
        )
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise FixtureAllowlistUnavailable(
                f"fixture allowlist entry {idx} in {allowlist_path} must be "
                f"a mapping"
            )
        entry_path = entry.get("path")
        if not isinstance(entry_path, str) or not entry_path:
            raise FixtureAllowlistUnavailable(
                f"fixture allowlist entry {idx} in {allowlist_path} needs a "
                f"non-empty string `path`"
            )
        only = entry.get("pattern_only")
        if only is not None and not isinstance(only, str):
            raise FixtureAllowlistUnavailable(
                f"fixture allowlist entry {idx} in {allowlist_path}: "
                f"`pattern_only` must be a string"
            )
    _fixture_allowlist = data
    return _fixture_allowlist


def _glob_to_regex(pattern: str) -> str:
    """Convert a glob pattern (*, **, ?) to a regex string."""
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                else:
                    out.append(".*")
                    i += 2
                    continue
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _is_fixture_allowed(rel: str, secret_label: str,
                        allowlist: list[dict]) -> bool:
    """Return True if the (file, pattern) pair is allow-listed."""
    for entry in allowlist:
        # Entries are validated by load_fixture_allowlist(); fail closed here
        # too so a hand-built list can never allowlist everything.
        if not isinstance(entry, dict):
            continue
        file_path = entry.get("path")
        if not isinstance(file_path, str) or not file_path:
            continue
        # Glob paths (* or ?) must match the whole relpath; plain paths match
        # the exact file or a directory prefix ("tools/a" never matches
        # "tools/abc.py").
        if "*" in file_path or "?" in file_path:
            if not re.fullmatch(_glob_to_regex(file_path), rel):
                continue
        elif rel != file_path and not rel.startswith(
                file_path.rstrip("/") + "/"):
            continue
        # If pattern_only is specified, the secret label must also match.
        pattern_only = entry.get("pattern_only")
        if pattern_only is not None:
            if pattern_only not in secret_label:
                continue
        return True
    return False


def load_reused_secrets() -> list[str]:
    """Literal secret strings that must never ship (the 'reused password' gate).

    Live credentials must not be embedded in this committed scanner, so the
    audited reused password(s) are read from an uncommitted sidecar file:
    ``open-core/known_secrets.local`` (or $OPEN_CORE_KNOWN_SECRETS), one literal
    per line, blanks and ``#`` comments ignored. When the file is absent the
    gate is a no-op and the build still relies on the pattern rules above.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("OPEN_CORE_KNOWN_SECRETS",
                          os.path.join(here, "known_secrets.local"))
    out: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
    except FileNotFoundError:
        return []  # documented: an absent sidecar is a no-op gate
    except (OSError, UnicodeDecodeError) as exc:
        # An existing but unreadable (or non-UTF-8) sidecar must NOT silently
        # disable the reused-credential gate. Report the path and the error
        # class only (never content) and let main() fail with rc 2.
        raise KnownSecretsUnavailable(
            f"known secrets file exists but cannot be read: {path} "
            f"({type(exc).__name__})") from None
    return out


def _url_decode_bounded(raw: str, max_rounds: int = 3) -> str:
    """Iteratively percent-decode up to ``max_rounds`` times (bounded).

    A single ``urllib.parse.unquote()`` pass only undoes one level of
    percent-encoding; a double-encoded byte (``%252E`` -> ``%2E`` -> ``.``)
    needs a second pass before the literal character appears, which would
    otherwise let a double-encoded internal hostname slip past the gate.
    Bounded at ``max_rounds`` so a pathological ``%2525...`` chain can't spin
    unboundedly; stops early once a pass is a no-op (fixed point).
    """
    dec = raw
    for _ in range(max_rounds):
        nxt = urllib.parse.unquote(dec)
        if nxt == dec:
            break
        dec = nxt
    return dec


def _line_of(data: str, offset: int) -> int:
    """1-based line number of ``offset`` in ``data`` (findings report this,
    never the matched text: the CI log is public)."""
    return data.count("\n", 0, offset) + 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--fixture-allowlist", default=None,
                    help="Path to YAML fixture allowlist (SECRET_PATTERNS gate only)")
    args = ap.parse_args()

    try:
        fixture_allowlist = load_fixture_allowlist(args.fixture_allowlist)
    except FixtureAllowlistUnavailable as exc:
        # An explicitly-requested allowlist that cannot be loaded
        # must fail loud, not silently scan with 0 entries.  rc 2 is distinct
        # from the findings rc (1).
        print(f"secret_scan: {exc}")
        return 2
    if fixture_allowlist:
        print(f"secret_scan: fixture allowlist loaded: {len(fixture_allowlist)} "
              f"entry(ies)")

    # The scan itself lives in _run_with_allowlist() so the CLI and the direct
    # API cannot drift apart. main() only parses args and loads the allowlist.
    try:
        return _run_with_allowlist(args.tree, fixture_allowlist)
    except KnownSecretsUnavailable as exc:
        print(f"secret_scan: {exc}")
        return 2


def _run_with_allowlist(tree: str, allowlist: list[dict] | None = None) -> int:
    """Run the full scan with a pre-loaded allowlist (CLI and direct API).

    Returns 0 on pass, 1 on fail, and raises KnownSecretsUnavailable (main()
    maps it to rc 2) when the reused-credential sidecar cannot be read.
    Output is identical for both callers.
    """
    hits: list[str] = []
    reused_secrets = load_reused_secrets()
    al = allowlist if allowlist is not None else []

    for root, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, tree)
            for bad in FORBIDDEN_PATHS:
                if bad in rel:
                    hits.append(f"FORBIDDEN PATH: {rel} (matched '{bad}')")
            if rel in SELF_SCAN_EXCLUDES:
                continue  # the gate's own files (see note above)
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT:
                continue
            try:
                with open(full, encoding="utf-8") as f:
                    raw = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            dec = _url_decode_bounded(raw)
            data = raw if dec == raw else raw + "\n" + dec
            for rx, label in SECRET_PATTERNS:
                mo0 = None
                for mo in rx.finditer(data):
                    if _is_fixture_allowed(rel, label, al):
                        continue
                    if label.startswith("private IP") and _is_rfc1918_network(
                            data, mo):
                        continue  # RFC1918 range NAME, not a host
                    if _is_public_contact(_contact_token(data, mo)):
                        continue  # public contacts shown in CE
                    if mo0 is None:
                        mo0 = mo  # flag the first non-allowed match
                if mo0:
                    # label + path + line only: NEVER any part of the match
                    hits.append(f"SECRET [{label}] in {rel}:"
                                f"{_line_of(data, mo0.start())} (value redacted)")
            # Bare org domain / org contact email: allowed ONLY in the
            # governance/overlay doc set, or when the matched token is one of
            # the public contacts (PUBLIC_CONTACT_ALLOWLIST).
            if rel.replace(os.sep, "/") not in GOVERNANCE_DOC_ALLOWLIST:
                bad = [mo for mo in BARE_ORG_DOMAIN_RX.finditer(data)
                       if not _is_public_contact(_contact_token(data, mo))]
                if bad:
                    hits.append(f"SECRET [org domain/email outside governance "
                                f"docs] in {rel}:"
                                f"{_line_of(data, bad[0].start())} (value redacted)")
            for secret in reused_secrets:
                if secret in data:
                    hits.append(f"SECRET [reused credential] in {rel}:"
                                f"{_line_of(data, data.find(secret))} "
                                f"(value redacted)")

    if hits:
        print(f"ACCEPTANCE FAILED — {len(hits)} finding(s), publication blocked:")
        for h in hits[:100]:
            print(f"  ✗ {h}")
        if len(hits) > 100:
            print(f"  ... and {len(hits) - 100} more")
        return 1

    print("ACCEPTANCE PASSED — no forbidden paths or secret patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
