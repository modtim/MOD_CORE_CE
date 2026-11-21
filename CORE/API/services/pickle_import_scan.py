"""FX.148 p3a (owner decision Q135=C) — static pickle-import scan.

Model artefacts arrive in formats that are pickle underneath: `.pt`/`.pth`
(torch.save), `.bin` (legacy torch), `.ckpt`, `.pkl`/`.pickle`, `.joblib`,
`.npy`/`.npz` (numpy object arrays) and the modern torch container, which is
a ZIP whose `data/<name>/data.pkl` member is a pickle. Unpickling any of them
EXECUTES whatever `GLOBAL`/`STACK_GLOBAL` + `REDUCE` pair the file names, so
a downloaded model can carry `os.system` / `subprocess.Popen` /
`builtins.eval` and run it the moment the file is loaded.

This module never unpickles. It disassembles the byte stream with stdlib
`pickletools.genops` and reports the opcodes that would call a callable
(`GLOBAL`, `STACK_GLOBAL`, `INST`, `REDUCE`, `NEWOBJ`, `NEWOBJ_EX`,
`PERSID`, `BINPERSID`) whose module half is not one a weights-only tensor
pickle would ever emit.

Verdicts: clean / unsafe / error (unreadable, over cap, unparseable).

Settings are registered HERE — `services/platform_settings.py` is off-limits
to this lane and `register()` is a no-op on re-registration, so a later
registry-side entry cannot clobber this spec. Defaults are the SAFE posture:
the scan runs (a clean model is byte-identical in behaviour, an unsafe one is
refused), and the owner exception map ("pickle blocked unless owner
exception w/ reason", parent FX.148) is empty.
"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from services import platform_settings as _psr

logger = logging.getLogger(__name__)

SETTING_ENABLED = "models.pickle_scan_enabled"
SETTING_MAX_BYTES = "models.pickle_scan_max_bytes"
SETTING_EXCEPTIONS = "models.pickle_scan_owner_exceptions"
SETTING_FAIL_CLOSED = "models.pickle_scan_fail_closed"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per file

for _spec in (
    (SETTING_ENABLED, bool, True, "Scan downloaded/extracted model artefacts "
     "for dangerous pickle opcodes before they are registered or loaded. "
     "FX.148 p3a; read by services/pickle_import_scan."),
    (SETTING_MAX_BYTES, int, DEFAULT_MAX_BYTES,
     "Per-file cap for the pickle scan; a file over the cap is an ERROR "
     "verdict (the caller's fail mode decides), never a silent pass."),
    (SETTING_EXCEPTIONS, dict, {},
     "Owner exceptions to the pickle block, keyed by artefact name: "
     "{name: {'reason': str, 'approver': str}}. A non-empty reason is "
     "mandatory — an exception without one does not lift the block."),
    ("models.pickle_scan_fail_closed", bool, True,
     "What to do when the pickle scan ERRORS (unreadable / over cap): True "
     "(default) refuses the artefact, False accepts it with an audit row. "
     "Same posture as FX.90's malware_scan.fail_closed, kept separate so "
     "one detector's outage never changes the other's."),
):
    _psr.register(_spec[0], _spec[1], _spec[2], group="models",
                  description=_spec[3])


# Opcodes that would invoke a callable when the pickle is loaded.
CALL_OPCODES = frozenset({
    "GLOBAL", "STACK_GLOBAL", "INST", "REDUCE", "NEWOBJ", "NEWOBJ_EX",
    "PERSID", "BINPERSID",
})

# Modules a pickle must never name: importing/using any of them at load time
# runs attacker-chosen code.
DANGEROUS_MODULES = frozenset({
    "os", "posix", "nt", "subprocess", "multiprocessing", "ctypes", "sys",
    "shutil", "glob", "fcntl", "pty", "resource", "signal", "thread",
    "dummy_thread", "marshal", "import", "importlib", "imp", "io", "file",
    "posixpath", "genericops", "commands", "pipes", "sqlite3", "smtplib",
    "socket", "ssl", "urllib", "urllib2", "requests", "http", "xmlrpc",
    "xml", "base64", "pickle", "dill", "shelve", "tempfile", "traceback",
    "code", "codeop", "ptyopen", "runas", "builtins", "__builtin__",
})

# builtins is legitimate for a few helpers (bytearray, slice, type); these
# members are the code-execution surface.
DANGEROUS_BUILTIN_MEMBERS = frozenset({
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "open", "input", "breakpoint",
    "memoryview", "exit", "quit", "help", "iter",
})

# What a weights-only loader DOES name — kept as documentation, not as an
# allowlist. A callable can only be invoked through a GLOBAL/STACK_GLOBAL/
# INST that named a module, so the denylist above is the whole rule; an
# allowlist here would refuse real torch pickles (`torch._utils`,
# `_torch_package_0_.utils`, ...) on a default-on gate, which is exactly the
# false-positive hazard this part must avoid.
SAFE_LOADER_MODULE_EXAMPLES = (
    "torch._utils._rebuild_tensor_v2", "torch.HalfStorageType",
    "_torch_package_0_.utils._rebuild_tensor_v2",
    "numpy._numpy_pickle_utils._numpy_unpickle",
    "collections.OrderedDict",
)

# Pickle protocol markers (`\x80` + protocol number) — the first bytes of
# every protocol >= 2 stream, which is what torch/numpy/sklearn emit.
# Protocol >=2 streams start with \x80 + the protocol number; protocol 0/1
# streams start with MARK (`(`) or an immediate STOP (`.`).
PICKLE_PROTO_MARKERS = (b"\x80\x00", b"\x80\x01", b"\x80\x02", b"\x80\x03",
                        b"\x80\x04", b"\x80\x05", b"(", b".")

# File suffixes whose content is (or can be) a pickle.
PICKLE_SUFFIXES = (".pt", ".pth", ".pt2", ".bin", ".ckpt", ".checkpoints",
                   ".pkl", ".pickle", ".joblib", ".npy", ".npz", ".t7",
                   ".obj", ".dat")


# ---------------------------------------------------------------------------
# Verdict shape
# ---------------------------------------------------------------------------

@dataclass
class ScanVerdict:
    """One scan result. `status` is clean | unsafe | error.

    When unsafe, the finding is carried verbatim (`opcode` + `module` +
    `member` + `path`) so the audit row and the 422 name WHAT the file
    would have run — never file content.
    """
    status: str
    detail: Optional[str] = None
    opcode: Optional[str] = None
    module: Optional[str] = None
    member: Optional[str] = None
    path: Optional[str] = None
    scanned_files: int = 0


@dataclass
class ScanConfig:
    enabled: bool
    max_bytes: int
    exceptions: dict = field(default_factory=dict)
    fail_closed: bool = True


def get_config(db=None) -> ScanConfig:
    """Read the four settings, falling back to the registered defaults."""
    try:
        enabled = _psr.get_setting(SETTING_ENABLED, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.148 p3a: read of %s failed (%s); using safe "
                       "default True", SETTING_ENABLED, e)
        enabled = True
    try:
        cap = _psr.get_setting(SETTING_MAX_BYTES, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.148 p3a: read of %s failed (%s); using default",
                       SETTING_MAX_BYTES, e)
        cap = DEFAULT_MAX_BYTES
    try:
        exc = _psr.get_setting(SETTING_EXCEPTIONS, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.148 p3a: read of %s failed (%s); no exceptions",
                       SETTING_EXCEPTIONS, e)
        exc = {}
    try:
        fail_closed = _psr.get_setting(SETTING_FAIL_CLOSED, db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.148 p3a: read of %s failed (%s); using safe "
                       "default True", SETTING_FAIL_CLOSED, e)
        fail_closed = True
    return ScanConfig(enabled=bool(enabled) if enabled is not None else True,
                      max_bytes=int(cap or DEFAULT_MAX_BYTES),
                      exceptions=exc if isinstance(exc, dict) else {},
                      fail_closed=bool(fail_closed)
                      if fail_closed is not None else True)


def owner_exception_ok(artefact: Optional[str], cfg: ScanConfig) -> Optional[str]:
    """The owner-exception reason for `artefact`, or None when blocked.

    The parent spec allows the pickle block to be lifted only as an owner
    exception WITH a reason, so an entry without a non-empty reason is not
    an exception at all.
    """
    if not artefact or not isinstance(cfg.exceptions, dict):
        return None
    entry = cfg.exceptions.get(artefact)
    if not isinstance(entry, dict):
        return None
    reason = (entry.get("reason") or "").strip()
    if not reason:
        return None
    return reason


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------

def _globals_in(data: bytes) -> tuple[list, list]:
    """Return (globals, call_opcodes) found in one pickle byte stream.

    `globals` is a list of (module, member, opcode) triples; `call_opcodes`
    the names of the callable-invoking opcodes seen. STACK_GLOBAL takes its
    module/name from the two preceding string opcodes, so those are tracked
    in a small rolling window.
    """
    import pickletools

    strings: list[str] = []
    globals_: list[tuple[str, str, str]] = []
    calls: list[str] = []
    for info, arg, _pos in pickletools.genops(data):
        name = info.name
        if name in ("SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8",
                    "UNICODE", "SHORT_BINSTRING", "BINSTRING", "BINBYTES",
                    "SHORT_BINBYTES"):
            if isinstance(arg, bytes):
                arg = arg.decode("utf-8", "replace")
            if isinstance(arg, str):
                strings.append(arg)
                if len(strings) > 8:
                    strings.pop(0)
        elif name in ("GLOBAL", "INST"):
            # GLOBAL/INST carry the pair in the opcode argument itself,
            # space-separated ("posix system").
            mod, _, member = (arg or "").partition(" ")
            # protocol <=2 spells the builtin module "__builtin__"; the
            # denylist + audit speak one canonical name.
            if mod in ("__builtin__", "__builtins__"):
                mod = "builtins"
            globals_.append((mod, member, name))
        elif name == "STACK_GLOBAL":
            # STACK_GLOBAL takes them off the stack: the two preceding
            # string opcodes.
            if len(strings) >= 2:
                globals_.append((strings[-2], strings[-1], name))
        elif name in CALL_OPCODES:
            calls.append(name)
    return globals_, calls


def _classify(data: bytes, path: Optional[str] = None) -> ScanVerdict:
    """Disassemble one pickle stream and classify it (never loads it).

    The rule is denylist-first, exactly the parent spec's wording
    ("GLOBAL/STACK_GLOBAL referencing os, subprocess, builtins.eval/exec,
    posix"): a global whose module half is a known code-execution module, or
    whose builtins member is one of the eval/exec/import/open family, is
    UNSAFE. A stream that names no such global is clean — real torch/numpy
    weight pickles name `torch._utils._rebuild_tensor_v2`-style globals and
    must not be caught by a bare "unknown module" rule.
    """
    try:
        globals_, calls = _globals_in(data)
    except Exception as e:  # noqa: BLE001 — truncated/garbage stream
        return ScanVerdict(status="error",
                           detail=f"not parseable as a pickle stream: {e}",
                           path=path)
    for mod, member, opcode in globals_:
        if mod in DANGEROUS_MODULES:
            return ScanVerdict(status="unsafe", path=path, opcode=opcode,
                               module=mod, member=member,
                               detail=f"{opcode} resolves to {mod}.{member}, "
                                      f"a module the pickle block refuses")
        if mod in ("builtins", "__builtins__") and (
                member in DANGEROUS_BUILTIN_MEMBERS or member == ""):
            return ScanVerdict(status="unsafe", path=path, opcode=opcode,
                               module=mod, member=member,
                               detail=f"{opcode} resolves to builtins."
                                      f"{member or '<bare>'}, a code-execution "
                                      f"builtin")
    return ScanVerdict(status="clean", path=path,
                       detail=(f"{len(calls)} callable opcode(s) "
                               f"outside the denylist" if calls else None))


def _pickle_streams(path: str, data: bytes, force: bool = False) -> list[tuple[str, bytes]]:
    """Every pickle stream carried by one file.

    A modern torch artefact (`.pt`/`.pth`/`.bin` saved by torch 1.6+) is a
    ZIP whose `data/<name>/data.pkl` member is the pickle; scanning only the
    outer container would miss it entirely, so each pickle-carrying member is
    scanned on its own. A `.npy` object array carries a pickle after its
    magic header. Anything else is the file itself.
    """
    if zipfile.is_zipfile(path) or data[:4] in (b"PK\x03\x04", b"PK\x05\x06",
                                               b"PK\x03\x00"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                streams = []
                for info in zf.infolist():
                    nm = info.filename
                    if nm.endswith((".pkl", ".pickle")) or nm.endswith(
                            PICKLE_SUFFIXES):
                        streams.append((nm, zf.read(nm)))
                return streams
        except (OSError, zipfile.BadZipFile, EOFError):
            # Not a readable zip: fall back to scanning the raw bytes, which
            # still catches a bare pickle glued at the front of the file.
            return [(os.path.basename(path), data)]
    if path.lower().endswith(".npy"):
        for marker in (b"\x80\x05", b"\x80\x04", b"\x80\x03", b"\x80\x02"):
            at = data.find(marker)
            if at > 0:
                return [(f"{os.path.basename(path)}@{at}", data[at:])]
    # A file that does not even start with a pickle protocol marker is not a
    # pickle: raw weight blobs (`.bin` in particular) are common in model
    # packages and must not become ERROR verdicts (which, fail-closed, would
    # refuse a perfectly good upload). `.pkl`/`.pickle` are taken on faith —
    # a file claiming to be a pickle that does not parse is itself suspicious.
    if any(data.startswith(m) for m in PICKLE_PROTO_MARKERS):
        return [(os.path.basename(path), data)]
    if path.lower().endswith((".pkl", ".pickle", ".joblib")):
        return [(os.path.basename(path), data)]
    if force:
        # The caller asserted this blob is a pickle candidate (scan_bytes on
        # an in-memory stream: a hand-built / older-protocol stream carries no
        # PROTO marker at all), so inspect it rather than skip it.
        return [(os.path.basename(path), data)]
    return []


def scan_bytes(data: bytes, artefact: Optional[str] = None) -> ScanVerdict:
    """Scan one in-memory blob (a torch zip container or a bare pickle)."""
    streams = _pickle_streams(artefact or "artefact", data, force=True)
    for label, blob in streams:
        verdict = _classify(blob, path=label)
        verdict.scanned_files = len(streams)
        if verdict.status != "clean":
            return verdict
    return ScanVerdict(status="clean", scanned_files=len(streams))


def scan_file(path: str, *, max_bytes: Optional[int] = None) -> ScanVerdict:
    """Scan one file on disk. Over-cap -> ERROR (never a silent pass)."""
    cap = max_bytes if max_bytes is not None else DEFAULT_MAX_BYTES
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return ScanVerdict(status="error", detail=f"unreadable: {e}", path=path)
    if size > cap:
        return ScanVerdict(status="error", path=path,
                           detail=f"exceeds scan cap ({size} > {cap} bytes)")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return ScanVerdict(status="error", detail=f"unreadable: {e}", path=path)
    # force=False: a file that is neither a zip container nor a pickle stream
    # (a raw `.bin` weight blob, a `.npy` of plain floats) is NOT a pickle and
    # must be clean, not an ERROR — fail-closed ERROR would refuse a
    # perfectly good upload.
    for label, blob in _pickle_streams(path, data, force=False):
        verdict = _classify(blob, path=os.path.basename(path))
        if verdict.status != "clean":
            verdict.path = path
            return verdict
    return ScanVerdict(status="clean", path=path)


def scan_artefact_dir(root: str, *, max_bytes: Optional[int] = None,
                      only_pickle_carrying: bool = True) -> ScanVerdict:
    """Scan every pickle-carrying file under a staged artefact dir.

    `.safetensors`/`.gguf`/`.onnx`/config/json members are skipped by design
    (`only_pickle_carrying`): they are not unpickled at load time, so a
    dangerous-pickle verdict cannot apply to them and scanning them only
    wastes the cap budget. A file over the cap is an ERROR verdict naming it.
    """
    cap = max_bytes if max_bytes is not None else DEFAULT_MAX_BYTES
    scanned = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if only_pickle_carrying and not name.lower().endswith(PICKLE_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            verdict = scan_file(full, max_bytes=cap)
            verdict.scanned_files = scanned + 1
            if verdict.status != "clean":
                return verdict
            scanned += 1
    return ScanVerdict(status="clean", scanned_files=scanned,
                       detail=f"{scanned} pickle-carrying file(s) clean")


# ---------------------------------------------------------------------------
# The gate (audit + fail mode), mirroring FX.90's artefact gate
# ---------------------------------------------------------------------------

class PickleUnsafeError(Exception):
    """The artefact carries a dangerous pickle; the caller must refuse it."""
    message = "artefact carries an unsafe pickle"

    def __init__(self, message: Optional[str] = None):
        if message:
            self.message = message


class PickleScanUnavailableError(Exception):
    """The scan errored and the fail mode is closed -> refuse the artefact."""
    message = "pickle scan unavailable"

    def __init__(self, message: Optional[str] = None):
        if message:
            self.message = message


def gate_artefact_pickle_scan(
    db,
    *,
    path: Optional[str] = None,
    dir_root: Optional[str] = None,
    source: str,
    artefact: Optional[str],
    user=None,
    tenant_id: Optional[int] = None,
) -> None:
    """FX.148 p3a — scan a DOWNLOADED/EXTRACTED model artefact for dangerous
    pickles BEFORE it is registered or made usable (the companion to FX.90's
    ClamAV artefact gate: ClamAV matches byte signatures, this matches the
    semantic case it cannot — a well-formed pickle naming `os.system`).

    No-op while `models.pickle_scan_enabled` is False. When enabled:
      clean    -> return (the caller may register the artefact)
      unsafe   -> `MALWARE_UPLOAD_BLOCKED` audit (the shared artefact-block
                  action; metadata carries detector="pickle_scan" + the
                  opcode/module/member so the trail names WHAT would have
                  run, never file content) + PickleUnsafeError (-> 422).
                  An owner exception (`models.pickle_scan_owner_exceptions`,
                  reason mandatory) downgrades this to a WARNING + audit
                  instead of a block.
      error    -> `models.pickle_scan_fail_closed` True (default) ->
                  PickleScanUnavailableError (-> 503); False -> accept with
                  a WARNING + MALWARE_SCAN_ERROR audit.

    Audit actions reuse the FX.90 pair: models.py is off-limits to this lane,
    so no new enum member (and no new migration) can be added here. The
    `detector` key in metadata is what separates the two verdicts.
    """
    from audit import log_security_event
    from models import AuditAction

    cfg = get_config(db=db)
    if not cfg.enabled:
        return
    if not (path or dir_root):
        raise PickleUnsafeError("pickle scan needs a path or dir_root")

    if dir_root:
        verdict = scan_artefact_dir(dir_root, max_bytes=cfg.max_bytes)
    else:
        verdict = scan_file(path, max_bytes=cfg.max_bytes)

    metadata = {
        "source": source,
        "artefact": artefact,
        "detector": "pickle_scan",
        "opcode": verdict.opcode,
        "module": verdict.module,
        "member": verdict.member,
        "path": verdict.path,
        "scanned_files": verdict.scanned_files,
    }

    if verdict.status == "unsafe":
        reason = owner_exception_ok(artefact, cfg)
        if reason is not None:
            log_security_event(
                db, AuditAction.MALWARE_SCAN_ERROR,
                user_id=getattr(user, "id", None),
                tenant_id=tenant_id if tenant_id is not None
                        else getattr(user, "tenant_id", None),
                metadata={**metadata, "owner_exception_reason": reason,
                          "applied": "accepted-under-owner-exception"},
            )
            logger.warning("FX.148 p3a: %s accepted under an owner exception "
                           "(reason: %s)", artefact, reason)
            return
        log_security_event(
            db, AuditAction.MALWARE_UPLOAD_BLOCKED,
            user_id=getattr(user, "id", None),
            tenant_id=tenant_id if tenant_id is not None
                    else getattr(user, "tenant_id", None),
            metadata={**metadata, "detail": verdict.detail},
        )
        raise PickleUnsafeError(
            f"artefact '{artefact}' carries an unsafe pickle "
            f"({verdict.detail or 'dangerous pickle opcode'})")

    if verdict.status == "error":
        if cfg.fail_closed:
            raise PickleScanUnavailableError(
                f"pickle scan failed for '{artefact}' "
                f"({verdict.detail or 'unreadable'})")
        log_security_event(
            db, AuditAction.MALWARE_SCAN_ERROR,
            user_id=getattr(user, "id", None),
            tenant_id=tenant_id if tenant_id is not None
                    else getattr(user, "tenant_id", None),
            metadata={**metadata, "detail": verdict.detail,
                      "applied": "fail-open"},
        )
