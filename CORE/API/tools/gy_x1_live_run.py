#!/usr/bin/env python3
"""GY.X1.s2 — live-install per-profile exit runner over HTTP.

Usage:
    python3 tools/gy_x1_live_run.py --base-url https://host \
        --token-file <path> [--profiles a,b] [--report out.json] \
        [--timeout 60] [--insecure-skip-verify-for-lan-selfsigned]

For every profile (default: every id in GET /v1/platform/compliance/profiles
``available``): preview -> apply (401 ``reauth_required`` -> stop, exit 2:
the operator re-logs in and re-runs; the tool never handles passwords),
conformance run (every automated result must be pass / not-applicable;
manual items are listed in the report), evidence bundle for the last 24h
saved to a temp file and verified OFFLINE with
``tools.verify_evidence_bundle.verify_bundle`` == 0, weakening a locked
setting -> require HTTP 409 ``reason: locked_by_compliance_profile`` (a
wrong 2xx is FAIL and the prior value is restored), then the prior
profile list is restored via the same preview/apply.

Exit codes: 0 every profile passed, 1 any failure, 2 reauth/usage error.
Fail closed: any HTTP/JSON/timeout error = FAIL, never skipped. The
report and logs carry profile ids, check ids, status strings and counts
only — never tokens, hosts or IPs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

JSON_CAP = 4 * 1024 * 1024
ZIP_CAP = 64 * 1024 * 1024
PASS_STATUSES = ("pass", "not-applicable")
EXPECTED_AUDIT_ACTIONS = (
    "COMPLIANCE_PROFILE_CHANGE",
    "CONFORMANCE_RUN",
    "EVIDENCE_BUNDLE_EXPORT",
    "COMPLIANCE_PROFILE_LOCK_REJECTED",
)


class _TransportError(Exception):
    """Generic transport failure; the message never carries the URL/host."""


def _verifier():
    """The HEAD offline bundle verifier (imported, never copied)."""
    try:
        from tools.verify_evidence_bundle import verify_bundle
        return verify_bundle
    except ImportError:
        pass
    path = __file__
    if path.endswith(".py"):
        sibling = path[:-len("gy_x1_live_run.py")] + "verify_evidence_bundle.py"
    else:  # pragma: no cover - defensive
        return None
    spec = importlib.util.spec_from_file_location(
        "verify_evidence_bundle", sibling)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.verify_bundle


def _build_opener(insecure: bool):
    """No-redirect opener: no HTTPRedirectHandler is registered, so any
    3xx raises HTTPError and nothing is followed. TLS verification is ON
    unless the explicit LAN flag is given (then a warning is printed)."""
    handlers = [urllib.request.ProxyHandler({}),
                urllib.request.HTTPHandler(),
                urllib.request.HTTPDefaultErrorHandler(),
                urllib.request.HTTPErrorProcessor()]
    if insecure:
        handlers.insert(2, urllib.request.HTTPSHandler(
            context=ssl._create_unverified_context()))
    else:
        handlers.insert(2, urllib.request.HTTPSHandler())
    opener = urllib.request.OpenerDirector()
    for h in handlers:
        opener.add_handler(h)
    return opener


def _base_ok(url: str) -> bool:
    if url.startswith("https://"):
        return True
    for host in ("http://localhost", "http://127.0.0.1"):
        if url == host or url.startswith(host + ":") or \
                url.startswith(host + "/"):
            return True
    return False


def _read_token(path: str):
    try:
        with open(path, "r") as fh:
            token = fh.read().strip()
    except OSError:
        print("error: token file unreadable")
        return None
    if not token:
        print("error: token file empty")
        return None
    return token


def _call(opener, base, path, *, method, token, body=None, timeout,
          cap, counts=None, key=""):
    """One request to the single --base-url origin. Returns (status,
    bytes). Non-2xx arrives as HTTPError (redirects included); the body
    is kept for 401/403/409 inspection. Transport failures raise
    _TransportError with a host-free message."""
    if counts is not None:
        counts[key] = counts.get(key, 0) + 1
    headers = {"Authorization": "Bearer %s" % token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers)
    try:
        resp = opener.open(req, None, timeout)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(cap + 1)
        except Exception:
            payload = b""
        return exc.code, payload
    except Exception:
        raise _TransportError("transport error") from None
    try:
        payload = resp.read(cap + 1)
    except Exception:
        raise _TransportError("transport error") from None
    return resp.status, payload


def _json(payload: bytes, cap: int = JSON_CAP):
    if len(payload) > cap:
        return None
    try:
        doc = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def _detail_reason(payload: bytes):
    doc = _json(payload)
    if doc is None:
        return None
    detail = doc.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("reason", detail.get("error", "")))
    return str(doc.get("reason", doc.get("error", "")))


def _weaker_value(current):
    """A value that fails the entry's own requirement (section 6 step 3):
    bool true -> false; numeric -> 0; string -> ""."""
    if isinstance(current, bool) or current is True:
        return False
    if isinstance(current, (int, float)):
        return 0
    return ""


def run_profile(opener, base, token, timeout, profile, prior_profiles,
                counts):
    """One profile's exit test. Returns (ok, record). Never raises on a
    server/transport failure: every step records a reason and fails."""
    rec = {"profile": profile, "checks": [], "manual_items": [],
           "ok": True}

    def note(name, ok, reason=""):
        rec["checks"].append({"check": name, "status": "pass" if ok else
                              "fail", "reason": reason})
        if not ok:
            rec["ok"] = False

    # 1. preview -> apply
    status, payload = _call(opener, base, "/v1/platform/compliance/profiles",
                            method="GET", token=token, timeout=timeout,
                            cap=JSON_CAP, counts=counts, key="profiles")
    if status != 200:
        note("profiles_list", False, "http_%s" % status)
        return False, rec
    doc = _json(payload)
    if doc is None:
        note("profiles_list", False, "non_json")
        return False, rec
    entries = doc.get("profiles") or []
    entry = next((e for e in entries if e.get("profile") == profile), None)
    if entry is None:
        note("profiles_list", False, "profile_absent")
        return False, rec
    status, payload = _call(
        opener, base, "/v1/platform/compliance/profiles/preview",
        method="POST", token=token, timeout=timeout, cap=JSON_CAP,
        counts=counts, key="preview", body={"profiles": [profile]})
    if status != 200:
        note("preview", False, "http_%s" % status)
        return False, rec
    prev = _json(payload)
    if prev is None or "preview_hash" not in prev:
        note("preview", False, "missing_preview_hash")
        return False, rec
    status, payload = _call(
        opener, base, "/v1/platform/compliance/profiles/apply",
        method="POST", token=token, timeout=timeout, cap=JSON_CAP,
        counts=counts, key="apply",
        body={"profiles": [profile], "preview_hash": prev["preview_hash"]})
    if status == 401:
        note("apply", False, "reauth_required")
        rec["reauth"] = True
        return False, rec
    if status != 200:
        note("apply", False, "http_%s" % status)
        return False, rec
    if _json(payload) is None:
        note("apply", False, "non_json")
        return False, rec
    note("apply", True)

    # 2. conformance run
    status, payload = _call(opener, base, "/v1/platform/conformance/run",
                            method="POST", token=token, timeout=timeout,
                            cap=JSON_CAP, counts=counts, key="run",
                            body={})
    if status != 200:
        note("conformance", False, "http_%s" % status)
        return False, rec
    run = _json(payload)
    if run is None or not isinstance(run.get("results"), list):
        note("conformance", False, "non_json")
        return False, rec
    automated = 0
    for res in run["results"]:
        check_id = str(res.get("check_id", "?"))
        kind = str(res.get("kind", "auto"))
        st = str(res.get("status", ""))
        if kind == "manual":
            rec["manual_items"].append(check_id)
            continue
        automated += 1
        if st not in PASS_STATUSES:
            note("conformance_%s" % check_id, False, st or "missing_status")
    if rec["ok"]:
        note("conformance", True, "automated=%d" % automated)

    # 3. evidence bundle (last 24h) + offline verify
    now = datetime.now(timezone.utc)
    status, payload = _call(
        opener, base, "/v1/compliance/evidence-bundle", method="POST",
        token=token, timeout=timeout, cap=ZIP_CAP, counts=counts,
        key="bundle", body={"window_start": (now - timedelta(hours=24)).
                            isoformat(), "window_end": now.isoformat()})
    if status != 200:
        note("bundle", False, "http_%s" % status)
        return False, rec
    if not payload:
        note("bundle", False, "empty_body")
        return False, rec
    fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="gyx1-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        rc = _verifier()(tmp)
    except Exception:
        rc = 1
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    if rc != 0:
        note("bundle_verify", False, "verifier_rc_%s" % rc)
        return False, rec
    note("bundle_verify", True)

    # 4. weakening a locked setting -> 409 locked_by_compliance_profile
    target = None
    for e2 in entry.get("required_settings") or []:
        if e2.get("pass_") is True or e2.get("pass") is True:
            target = e2
            break
    if target is None or target.get("setting") is None:
        note("lock_check", False, "no_passing_required_setting")
        return False, rec
    key = str(target["setting"])
    prior = target.get("current")
    status, payload = _call(
        opener, base, "/v1/platform/settings/%s" % key, method="PATCH",
        token=token, timeout=timeout, cap=JSON_CAP, counts=counts,
        key="patch", body={"value": _weaker_value(prior)})
    if status == 409:
        reason = _detail_reason(payload)
        if reason != "locked_by_compliance_profile":
            note("lock_check", False, "wrong_reason")
        else:
            note("lock_check", True)
    elif status == 200:
        note("lock_check", False, "weaken_accepted_2xx")
        # restore the prior value so the install is left as it was
        _call(opener, base, "/v1/platform/settings/%s" % key,
              method="PATCH", token=token, timeout=timeout, cap=JSON_CAP,
              counts=counts, key="restore", body={"value": prior})
    else:
        note("lock_check", False, "http_%s" % status)
        return False, rec

    # 5. restore the prior profile list
    status, payload = _call(
        opener, base, "/v1/platform/compliance/profiles/preview",
        method="POST", token=token, timeout=timeout, cap=JSON_CAP,
        counts=counts, key="restore_preview",
        body={"profiles": prior_profiles})
    if status != 200:
        note("restore_profiles", False, "http_%s" % status)
        return False, rec
    rp = _json(payload)
    if rp is None or "preview_hash" not in rp:
        note("restore_profiles", False, "missing_preview_hash")
        return False, rec
    status, payload = _call(
        opener, base, "/v1/platform/compliance/profiles/apply",
        method="POST", token=token, timeout=timeout, cap=JSON_CAP,
        counts=counts, key="restore_apply",
        body={"profiles": prior_profiles,
              "preview_hash": rp["preview_hash"]})
    if status == 401:
        note("restore_profiles", False, "reauth_required")
        rec["reauth"] = True
        return False, rec
    if status != 200:
        note("restore_profiles", False, "http_%s" % status)
        return False, rec
    note("restore_profiles", True)
    return rec["ok"], rec


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="GY.X1.s2 live per-profile exit runner")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--profiles", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--insecure-skip-verify-for-lan-selfsigned",
                        action="store_true")
    args = parser.parse_args(argv)

    if not _base_ok(args.base_url):
        print("error: base url must be https (localhost/127.0.0.1 http "
              "allowed for the stub)")
        return 2
    token = _read_token(args.token_file)
    if token is None:
        return 2
    if args.timeout < 1:
        print("error: timeout must be >= 1")
        return 2
    if args.insecure_skip_verify_for_lan_selfsigned:
        print("warning: TLS certificate verification is DISABLED for this "
              "run (LAN self-signed only)")

    opener = _build_opener(args.insecure_skip_verify_for_lan_selfsigned)
    counts: dict = {}
    base = args.base_url.rstrip("/")

    status, payload = _call(opener, base, "/v1/platform/compliance/profiles",
                            method="GET", token=token, timeout=args.timeout,
                            cap=JSON_CAP, counts=counts, key="profiles")
    doc = _json(payload) if status == 200 else None
    if doc is None:
        print("report: profiles list unavailable (http_%s or non_json)"
              % status)
        report = {"profiles": [], "expected_audit_actions":
                  EXPECTED_AUDIT_ACTIONS, "exit": 1}
        if args.report:
            with open(args.report, "w") as fh:
                json.dump(report, fh)
        return 1
    available = [str(p) for p in (doc.get("available") or [])]
    active_before = [str(p) for p in (doc.get("active") or [])]
    wanted = ([p.strip() for p in args.profiles.split(",") if p.strip()]
              if args.profiles else available)

    results = []
    exit_code = 0
    for profile in wanted:
        try:
            ok, rec = run_profile(opener, base, token, args.timeout,
                                  profile, active_before, counts)
        except _TransportError:
            ok, rec = False, {"profile": profile, "checks": [
                {"check": "transport", "status": "fail",
                 "reason": "transport_error"}], "manual_items": []}
        results.append(rec)
        if rec.get("reauth"):
            exit_code = 2
            break
        if not ok:
            exit_code = 1

    report = {"profiles": results, "expected_audit_actions":
              EXPECTED_AUDIT_ACTIONS, "exit": exit_code}
    if args.report:
        with open(args.report, "w") as fh:
            json.dump(report, fh)
    print("profiles=%d passed=%d exit=%d"
          % (len(results), sum(1 for r in results if r.get("ok")),
             exit_code))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
