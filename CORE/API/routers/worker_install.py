"""
Worker install & binary download API (single-tenant, ships in Community Edition)

The generic "add another machine" surface of /workers:

  GET  /workers/setup-script                      interactive Linux installer (curl | sudo bash)
  GET  /workers/install/macos.sh                  macOS (launchd) installer
  POST /workers/agent/download-token              short-lived single-use token (platform admin)
  GET  /workers/agent/install-script              Linux / macOS / Windows installer, token-gated
  GET  /workers/agent/download                    worker binary, token-gated
  GET  /workers/download/{os}/{arch}              worker binary (public, no secrets)
  GET  /workers/download/tailscale/{os}/{arch}    pinned Tailscale client mirror
  GET  /workers/releases/worker/{version}/{os_arch}

Nothing here is tenant-aware and nothing here is metered or licence-gated.
Split out of worker_provisioning.py so the open-core manifest can ship these
routes while the paid tenant-assignment / SSH-provisioning code in that file
stays denied. Paths, responses and the OpenAPI tag are unchanged; main.py
mounts both routers at the same position in the route order.

Binary source: the ``worker-releases`` bucket. The embedded worker publishes
every platform binary into it on each start (CORE/Worker/internal/releases),
so a fresh CE install needs no extra publishing step.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any
from pydantic import BaseModel, ConfigDict
import glob
import os
import re
import secrets as py_secrets
import logging

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

from dependencies import get_db, get_current_user, get_optional_current_user, require_active_and_onboarded_user
from models import User

# Same tag as worker_provisioning.router so the OpenAPI document is unchanged.
router = APIRouter(prefix="/workers", tags=["worker_provisioning"])


def require_platform_admin(current_user: User = Depends(require_active_and_onboarded_user)):
    """Dependency to ensure user is a platform admin"""
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="This endpoint requires platform admin privileges"
        )
    return current_user


class AgentDownloadTokenResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    token: Optional[str] = None
    expires_in_seconds: Optional[int] = None
    os: Optional[str] = None
    arch: Optional[str] = None
    install_command: Optional[str] = None


# ==========================================
# Worker Agent Binary Download
# ==========================================
# Two-step flow:
#   1. Platform admin calls POST /workers/agent/download-token to get a
#      short-lived, single-use token (valid 15 minutes).
#   2. Install script calls GET /workers/agent/download?token=TOKEN
#      (no auth header needed — the token IS the auth).
# This means the admin can generate a command in the Dashboard and paste
# it on a Raspberry Pi without needing a browser or OAuth flow on the device.

import hashlib
import time as _time

WORKER_RELEASES_BUCKET = "worker-releases"

# In-memory token store: {token_hash: {"os": ..., "arch": ..., "expires": ..., "used": bool}}
# Tokens are short-lived (15 min) and single-use. Server restart clears them (fine — they're ephemeral).
_download_tokens: Dict[str, dict] = {}
_DOWNLOAD_TOKEN_TTL = 15 * 60  # 15 minutes


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cleanup_expired_tokens():
    """Remove expired tokens to prevent memory growth."""
    now = _time.time()
    expired = [k for k, v in _download_tokens.items() if now > v["expires"]]
    for k in expired:
        del _download_tokens[k]


def _approle_mode(db: Session) -> str:
    """GSEC Q21.4 — per-worker AppRole enforcement mode (off|report|enforce).

    Resolves to 'off' on ANY failure (unknown key, registry error, bad
    value): the lifecycle must stay inert unless an admin explicitly
    flipped the platform setting to report/enforce."""
    try:
        from services import platform_settings as _ps
        mode = _ps.get_setting("worker.approle_per_worker_enforced", db=db)
        return mode if mode in ("off", "report", "enforce") else "off"
    except Exception:
        return "off"


def resolve_approle_worker_id(db: Session, worker_name: str):
    """GSEC Q21.5 — THE one AppRole key for a worker (FIX: one role key).

    Returns the WorkerLicense numeric id for the given worker name, or
    ``None`` when no license row exists yet. Both call sites must go through
    this helper so the per-worker AppRole is named identically everywhere:

      - this module's download-token render (``ensure_approle``), and
      - ``worker_management``'s enrollment hook
        (``_ensure_worker_approle_on_enrollment``) and revoke path, which key
        on ``worker.id`` of the same WorkerLicense row.

    A worker with no license row has no stable id at render time — callers
    skip the render (serving an unrendered script) rather than minting a
    slug-keyed role that enrollment would then never reuse."""
    from crud.worker_management import get_worker_by_name
    existing = get_worker_by_name(db, (worker_name or "").strip())
    return existing.id if existing is not None else None


_BINARY_CHUNK = 1024 * 1024


class _BinaryStream:
    """Fixed-size chunks of an S3 object with a known size; releases the connection when done."""

    def __init__(self, response, size: Optional[int]):
        self._response = response
        self.size = size

    def __iter__(self):
        try:
            yield from self._response.stream(_BINARY_CHUNK)
        finally:
            self._response.close()
            self._response.release_conn()


def _get_minio_binary(object_name: str):
    """Stream a binary from the worker-releases MinIO bucket."""
    try:
        from utils.storage import get_service_client
        client = get_service_client()
        size = client.stat_object(WORKER_RELEASES_BUCKET, object_name).size
        response = client.get_object(WORKER_RELEASES_BUCKET, object_name)
        return _BinaryStream(response, size)
    except Exception as e:
        logger.error(f"Failed to fetch worker binary {object_name}: {e}")
        return None


def _worker_object_name(os_name: str, arch: str) -> str:
    """
    Map (os, arch) to the worker-releases bucket object name.

    linux/darwin binaries carry no extension (`mod-worker-linux-amd64`);
    windows binaries are PE executables and must keep `.exe` so Windows'
    own loader/AV/SmartScreen treat them as one (`mod-worker-windows-amd64.exe`).
    Matches the naming CORE/Worker/Makefile's cross-build target produces and
    CORE/startup/start-main-server.sh's cross-compile block would need to
    mirror once windows publishing lands.
    """
    name = f"mod-worker-{os_name}-{arch}"
    if os_name == "windows":
        name += ".exe"
    return name


def _manifest_sha256_for(os_name: str, arch: str) -> Optional[str]:
    """
    Published SHA-256 for a worker binary from the release manifest
    (worker-releases/latest.json), or None when no manifest / no digest exists.

    GSEC R-035: the Go worker's self-update refuses to replace its binary
    unless the download matches this digest, so the download response must
    carry it. `amd64_sha256`/`arm64_sha256` are the original manifest fields
    and always describe the linux binaries (see CORE/Worker/internal/releases/
    publish.go's Manifest); other platforms use `{os}_{arch}_sha256`.
    """
    try:
        from utils.worker_releases import get_manifest
        m = get_manifest() or {}
        key = "amd64_sha256" if arch == "amd64" else "arm64_sha256"
        if os_name != "linux":
            key = f"{os_name}_{arch}_sha256"
        value = m.get(key)
        return (str(value).strip() or None) if value else None
    except Exception as e:
        logger.warning("worker-releases manifest lookup failed for %s/%s: %s", os_name, arch, e)
        return None


def _binary_missing_detail(os_name: str, arch: str) -> str:
    """404 text for a worker binary that is not in the worker-releases bucket.

    Paid text is unchanged. In CE the bucket is filled by the built-in
    ``mod-worker`` container on each start (there is no server-deploy script),
    so the hint points there instead.
    """
    from utils.paid_import import is_ce_edition
    if is_ce_edition():
        return (
            f"Worker binary not found for {os_name}/{arch}. The mod-worker container "
            "publishes it to the worker-releases bucket when it starts; wait for it "
            "to be healthy, or restart it (docker compose restart mod-worker)."
        )
    return f"Worker binary not found for {os_name}/{arch}. Run a server deploy to build binaries."


def _binary_headers(stream, filename: str, os_name: str = "", arch: str = "") -> dict:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    size = getattr(stream, "size", None)
    if size is not None:
        headers["Content-Length"] = str(size)
    if os_name and arch:
        sha256_hex = _manifest_sha256_for(os_name, arch)
        if sha256_hex:
            headers["X-Content-SHA256"] = sha256_hex
    return headers


def _install_api_base(request: Request) -> str:
    """Base URL the download-token install commands point at (F10).

    A CE default install never sets API_EXTERNAL_URL (the compose overlay has
    no such row), and the old `https://api.localhost` fallback is unreachable
    on the LAN, so the commands the Dashboard hands out never ran. An
    explicitly set API_EXTERNAL_URL/API_URL still wins verbatim (paid posture
    unchanged); otherwise derive the base from the request itself, exactly as
    the setup-script and macOS installer routes already do.
    """
    explicit = (os.getenv("API_EXTERNAL_URL") or os.getenv("API_URL") or "").strip()
    if explicit and explicit != "https://api.localhost":
        return explicit.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.post("/agent/download-token", response_model=AgentDownloadTokenResponse, response_model_exclude_unset=True, dependencies=[Depends(require_platform_admin)])
def create_download_token(
    request: Request,
    os_name: str = "linux",
    arch: str = "amd64",
    worker_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_platform_admin),
):
    """
    Generate a short-lived, single-use download token for the worker binary.

    **Platform admin only.**

    Returns a token and a ready-to-paste install command for the target device.
    Token is valid for 15 minutes and can only be used once.

    Supported combinations: linux/amd64, linux/arm64, windows/amd64,
    darwin/amd64, darwin/arm64. Windows has no arm64 build (see
    CORE/Worker/Makefile's cross-build target — only `windows-amd64`
    exists), so windows/arm64 is rejected like any other unsupported combo.

    ``worker_name`` (optional, GSEC Q21.5): when given AND the platform
    setting ``worker.approle_per_worker_enforced`` is not 'off' AND the
    worker already has a WorkerLicense row, the API ensures the worker's
    per-worker Vault AppRole (``worker_approle.ensure_approle``, keyed by the
    license's numeric id — the same key the Q21.4 enrollment hook uses) and
    stores the role/secret pair on the token. /agent/install-script then
    renders those values INTO the served install script so the operator never
    pastes a shared (or wrong) AppRole pair by hand. Mode 'off' (default) or
    a missing license row yields an unrendered script with no Vault calls; a
    Vault failure is non-fatal too — the token is still issued and the
    operator can fall back to explicit ``--vault-role-id`` /
    ``--vault-secret-id`` flags on the install command.
    """
    os_name = os_name.lower().strip()
    arch = arch.lower().strip()

    if os_name not in ("linux", "windows", "darwin"):
        raise HTTPException(status_code=400, detail=f"Unsupported OS: {os_name}. Supported: linux, windows, darwin")
    if os_name == "windows":
        if arch != "amd64":
            raise HTTPException(status_code=400, detail=f"Unsupported arch for windows: {arch}. Supported: amd64")
    elif arch not in ("amd64", "arm64"):
        raise HTTPException(status_code=400, detail=f"Unsupported arch: {arch}. Supported: amd64, arm64")

    _cleanup_expired_tokens()

    token = py_secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    token_data: Dict[str, Any] = {
        "os": os_name,
        "arch": arch,
        "expires": _time.time() + _DOWNLOAD_TOKEN_TTL,
        "used": False,
    }

    # Q21.5: per-worker AppRole creds rendered into the served install script
    # (linux only — the bash installer carries the __MOD_VAULT_*__ placeholders;
    # the .ps1 reads its values from the registry instead). Best-effort: a
    # Vault problem must never block a binary download.
    #
    # INERT BY DEFAULT: the whole step is gated on the platform setting
    # worker.approle_per_worker_enforced (off|report|enforce, resolved by the
    # same _approle_mode helper the Q21.4 lifecycle hooks in
    # worker_management use). Mode 'off' (the default) means ZERO Vault calls
    # and an unrendered script — placeholders normalize to empty inside the
    # installer, which is today's behaviour.
    #
    # Identity: ONE role key per worker. The render reuses the worker's
    # WorkerLicense numeric id — exactly the key the Q21.4 enrollment hook
    # (_ensure_worker_approle_on_enrollment) and revoke path use — via
    # resolve_approle_worker_id, so render and lifecycle can never drift
    # (no slug-vs-id second naming scheme).
    slug = (worker_name or "").strip()
    if slug and os_name == "linux" and _approle_mode(db) != "off":
        try:
            # services/worker_approle.py is paid (manifest deny): in CE the
            # module is absent, so this step is skipped and the script is
            # served unrendered (same as mode 'off').
            from utils.paid_import import optional_paid
            _approle = optional_paid("services.worker_approle")
            worker_id = (
                resolve_approle_worker_id(db, slug) if _approle is not None else None
            )
            if worker_id is not None:
                payload = _approle.ensure_approle(
                    worker_id, db=db, user_id=current_user.id,
                )
                if payload.get("role_id") and payload.get("secret_id"):
                    token_data["vault"] = {
                        "role": payload.get("role"),
                        "role_id": payload.get("role_id"),
                        "secret_id": payload.get("secret_id"),
                    }
        except Exception:
            logger.warning(
                "download-token: could not render per-worker AppRole creds "
                "for worker_name=%r — serving an unrendered install script "
                "(operator can pass --vault-role-id/--vault-secret-id)",
                slug,
            )

    _download_tokens[token_hash] = token_data

    api_url = _install_api_base(request)
    install_script_url = f"{api_url}/workers/agent/install-script?token={token}"

    if os_name == "windows":
        # PowerShell has no `curl | bash -s --` equivalent for passing args
        # through a pipe, so config is passed via env vars the served .ps1
        # reads as a fallback when its own -ApiUrl/-BootstrapToken params are
        # unset (see worker_install.ps1). `iex (irm ...)` is the PowerShell
        # analogue of `curl | bash`.
        install_command = (
            f"$env:MOD_API_URL='{api_url}'; $env:MOD_BOOTSTRAP_TOKEN='<YOUR_ENROLLMENT_TOKEN>'; "
            f"iex (irm '{install_script_url}')"
        )
    else:
        # linux and darwin share the same `curl | bash -s --` install-script
        # shape (worker_install.sh / worker_install_macos.sh both parse the
        # same --api-url/--token/--bootstrap-token flags); which script gets
        # served is decided by the token's os (see get_install_script).
        install_command = (
            f"curl -fsSL '{install_script_url}' | bash -s -- "
            f"--api-url {api_url} --token {token} --bootstrap-token <YOUR_ENROLLMENT_TOKEN>"
        )

    return {
        "token": token,
        "expires_in_seconds": _DOWNLOAD_TOKEN_TTL,
        "os": os_name,
        "arch": arch,
        "install_command": install_command,
    }


# api-lint: public-by-design: in-body token check validates token query param against _download_tokens dict, checking single-use and expiry
@router.get("/agent/download", response_model=None)
def download_worker_agent(
    token: str,
    os_name: Optional[str] = None,
    arch: Optional[str] = None,
):
    """
    Download the worker agent binary using a short-lived download token.

    No auth header needed — the token (from /agent/download-token) IS the auth.
    Token is single-use and expires after 15 minutes.
    """
    _cleanup_expired_tokens()

    token_hash = _hash_token(token)
    token_data = _download_tokens.get(token_hash)

    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired download token - generate a new command in Dashboard > Workers > Add Worker")

    if token_data["used"]:
        raise HTTPException(status_code=401, detail="Download token has already been used - generate a new command in Dashboard > Workers > Add Worker")

    if _time.time() > token_data["expires"]:
        del _download_tokens[token_hash]
        raise HTTPException(status_code=401, detail="Download token has expired - generate a new command in Dashboard > Workers > Add Worker")

    # Use OS/arch from token, allow override from query params
    dl_os = (os_name or token_data["os"]).lower().strip()
    dl_arch = (arch or token_data["arch"]).lower().strip()

    object_name = _worker_object_name(dl_os, dl_arch)
    stream = _get_minio_binary(object_name)

    if stream is None:
        raise HTTPException(
            status_code=404,
            detail=_binary_missing_detail(dl_os, dl_arch)
        )

    # Mark token as used
    token_data["used"] = True

    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers=_binary_headers(stream, object_name, dl_os, dl_arch),
    )


def _macos_plist_template() -> str:
    """MAC.1.s15: the macOS launchd service model lives in a template FILE
    (templates/macos/com.example.modworker.plist.tmpl) so the plist shape
    is reviewable on its own. The served installer runs piped from curl on a
    machine with no repo checkout, so the API inlines the template into the
    script at the ``__MOD_PLIST_TEMPLATE__`` sentinel (the script prefers the
    file when it is run from the repo). The template carries no secrets — the
    worker's env is sourced from the 0600 worker.env at exec time.
    """
    # Looked up by suffix, not by full name: the CE extract rewrites the
    # vendor prefix inside source text (com.example. -> com.example.) but
    # cannot rename the file, so a literal full name would miss it in CE.
    matches = sorted(glob.glob(os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "templates", "macos", "*modworker.plist.tmpl",
    )))
    if not matches:
        return ""
    with open(matches[0], "r") as f:
        return f.read().rstrip("\n")


# The sentinel LINE the installer carries inside its quoted here-doc. Matched
# as a whole line only: the installer's comment and its own guard grep mention
# the same token but never as a bare line, so a line-anchored replace cannot
# touch them.
_DETECT_SENTINEL = "__MOD_DETECT_OLD_MOD_SH__"


def _detect_old_mod_script() -> str:
    """F17-WORKER-INSTALL-PRINTS-OLD-MOD-REPORT: the old-MOD detector lives in
    a script FILE (CORE/Worker/scripts/detect-old-mod.sh) so it is reviewable
    and testable on its own. The served Linux installer runs piped from curl
    on a machine with no repo checkout, so the API inlines the detector text
    into the script at the ``__MOD_DETECT_OLD_MOD_SH__`` sentinel (the same
    mechanism _macos_plist_template uses for the launchd plist). A missing
    file returns "" and the served script keeps the sentinel, which the
    installer's own guard turns into "old MOD check skipped" (the step is
    informational and never breaks an install).

    Looked up by suffix (not full path) like the plist template: the CE extract
    rewrites vendor prefixes inside source text but cannot rename files, so a
    suffix match keeps working in both trees.

    F19: the API image build context is CORE/API only, so CORE/Worker is OUTSIDE
    it and the old ``../Worker/scripts`` lookup found nothing inside the image
    (the served installer kept the sentinel and printed "old MOD check skipped").
    The single source of truth that ships is therefore CORE/API/worker_scripts/
    detect-old-mod.sh (same directory-relative pattern _macos_plist_template
    uses, which resolves under the image WORKDIR /app); the repo-root Worker
    copy is tried second so a checkout without the API copy still renders.
    A pytest asserts the two copies stay byte-identical.
    """
    base = os.path.dirname(os.path.dirname(__file__))
    matches = sorted(glob.glob(os.path.join(
        base, "worker_scripts", "*detect-old-mod.sh",
    )))
    if not matches:
        matches = sorted(glob.glob(os.path.join(
            base, "..", "Worker", "scripts", "*detect-old-mod.sh",
        )))
    if not matches:
        return ""
    with open(matches[0], "r") as f:
        return f.read().rstrip("\n")


def _render_install_script(script_name: str, content: str, token_data: Dict[str, Any]) -> str:
    """GSEC Q21.5: substitute per-worker Vault AppRole creds into the served
    install script.

    The Linux bash installer carries the credential placeholders
    (``__MOD_VAULT_ROLE_ID__`` / ``__MOD_VAULT_SECRET_ID__``); a token minted
    with a worker_name has the pair on its record (see
    create_download_token). Unrendered scripts (no worker_name, or a Vault
    failure) are served verbatim — the placeholders normalize to empty inside
    the script and the operator can pass --vault-role-id/--vault-secret-id.
    Secrets land only in the script body (served over the same token-gated
    channel as the binary itself), never in logs or the token response.

    The macOS installer gets a different substitution: its launchd plist
    template is inlined at ``__MOD_PLIST_TEMPLATE__`` (no secrets involved).
    """
    if script_name == "worker_install_macos.sh":
        tmpl = _macos_plist_template()
        if tmpl and "__MOD_PLIST_TEMPLATE__" in content:
            content = content.replace("__MOD_PLIST_TEMPLATE__", tmpl)
        return content
    if script_name != "worker_install.sh":
        return content
    # F17: inline the old-MOD detector into the served Linux installer (one
    # wiring point, at render time: the static file keeps only the sentinel).
    # An empty read leaves the sentinel in place; the installer's own guard
    # prints "old MOD check skipped" for that case.
    detector = _detect_old_mod_script()
    if detector:
        # Replace the sentinel LINE only (not the comment mention or the
        # installer's own grep guard, which both carry the same token).
        content = content.replace("\n" + _DETECT_SENTINEL + "\n",
                                  "\n" + detector + "\n")
    vault = token_data.get("vault") or {}
    role_id = vault.get("role_id")
    secret_id = vault.get("secret_id")
    if not role_id and not secret_id:
        # Unrendered (no worker_name on the token, or a Vault failure): serve
        # verbatim — the script normalizes the placeholders to empty itself.
        return content
    content = content.replace("__MOD_VAULT_ROLE_ID__", role_id or "")
    content = content.replace("__MOD_VAULT_SECRET_ID__", secret_id or "")
    return content


# api-lint: public-by-design: in-body token check validates token query param against _download_tokens dict with expiry check
@router.get("/agent/install-script", response_model=None)
def get_install_script(
    token: str,
):
    """
    Serve the worker install script. Validates the download token but does
    NOT consume it — the script itself will use the token to download the binary.

    Which script is served follows the token's `os` (set by
    /agent/download-token, not the client): `windows` gets the PowerShell
    installer, `darwin` gets the launchd/macOS bash installer, everything
    else (linux) gets the plain systemd bash one.

    Usage (generated by /agent/download-token):
      linux:
        curl -fsSL 'https://api.example.com/workers/agent/install-script?token=TOKEN' | bash -s -- \\
            --api-url https://api.example.com --token TOKEN --bootstrap-token ENROLLMENT_TOKEN
      darwin (macOS):
        curl -fsSL 'https://api.example.com/workers/agent/install-script?token=TOKEN' | bash -s -- \\
            --api-url https://api.example.com --token TOKEN --bootstrap-token ENROLLMENT_TOKEN
      windows:
        $env:MOD_API_URL='https://api.example.com'; $env:MOD_BOOTSTRAP_TOKEN='ENROLLMENT_TOKEN'; \\
            iex (irm 'https://api.example.com/workers/agent/install-script?token=TOKEN')
    """
    token_hash = _hash_token(token)
    token_data = _download_tokens.get(token_hash)

    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired download token - generate a new command in Dashboard > Workers > Add Worker")
    if _time.time() > token_data["expires"]:
        del _download_tokens[token_hash]
        raise HTTPException(status_code=401, detail="Download token has expired - generate a new command in Dashboard > Workers > Add Worker")

    token_os = token_data.get("os")
    if token_os == "windows":
        script_name = "worker_install.ps1"
        disposition_name = "install.ps1"
    elif token_os == "darwin":
        script_name = "worker_install_macos.sh"
        disposition_name = "install.sh"
    else:
        script_name = "worker_install.sh"
        disposition_name = "install.sh"
    media_type = "text/plain"

    script_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), script_name)

    if not os.path.exists(script_path):
        raise HTTPException(status_code=404, detail="Install script not found")

    with open(script_path, "r") as f:
        content = f.read()

    content = _render_install_script(script_name, content, token_data)

    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{disposition_name}"'},
    )


# api-lint: public-by-design: setup script contains no secrets, prompts for everything at runtime
@router.get("/setup-script", response_model=None)
def get_setup_script(request: Request):
    """
    Serve the interactive setup script. No auth required — the script
    contains no secrets and prompts for everything at runtime.

    The API URL is auto-injected so the user doesn't have to type it
    when piping from curl.

    Usage:
        curl -sSL https://api.example.com/workers/setup-script | sudo bash
    """
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "worker_setup.sh"
    )

    if not os.path.exists(script_path):
        raise HTTPException(status_code=404, detail="Setup script not found")

    with open(script_path, "r") as f:
        content = f.read()

    # NOTE: API_URL is deliberately injected BARE (scheme+host, no /v1) — the
    # worker persists this same value verbatim into its own config as
    # `api_url` (see worker_setup.sh's generated config, and
    # CORE/Worker/internal/config/config.go's API_URL env var), which
    # heartbeat.go's sendHeartbeat() then unconditionally suffixes with
    # "/v1/workers/heartbeat" (no idempotent check, unlike
    # transport.NewAPITransport's HasSuffix guard). Baking /v1 in here would
    # give the running worker "…/v1/v1/workers/heartbeat" once enrolled. The
    # /v1 mount is instead added at each individual API call the *setup
    # script itself* makes (enroll/validate-token, enroll/start, download) —
    # see worker_setup.sh.
    # Same base as the download-token commands (F10): an explicit
    # API_EXTERNAL_URL wins, so a manager behind a front that rewrites Host
    # (colo Caddy -> Host api.localhost) does not hand remote workers an
    # unreachable http://api.localhost.
    api_url = _install_api_base(request)
    content = 'API_URL="${API_URL:-' + api_url + '}"\n' + content

    return StreamingResponse(
        iter([content]),
        media_type="text/plain",
        headers={"Content-Disposition": 'inline; filename="setup-worker.sh"'},
    )


@router.get("/install/macos.sh", response_model=None)
def get_macos_install_script(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    """
    Serve the macOS (launchd) worker installer as a single Terminal line:

        curl -fsSL https://api.example.com/v1/workers/install/macos.sh | \\
            bash -s -- --api-url https://api.example.com --token TOKEN

    Public by design — the script carries no secrets; the download token is
    passed by the operator on the command line (see worker_install_macos.sh's
    --token) and is never baked into what this route serves. The optional
    bearer dependency (get_optional_current_user, same pattern as platform_health
    /health) never rejects a caller: it exists so the public-by-design route is
    explicit to the API lint's R2 auth rule, and so a caller that happens to
    present a token can be recognised/audited later.

    The served copy is CORE/API/worker_install_macos.sh (start-main-server.sh
    copies CORE/Worker/scripts/install-macos.sh into the API app dir, next to
    this router file — the same location convention get_setup_script /
    get_install_script use). Its launchd plist is inlined at the
    ``__MOD_PLIST_TEMPLATE__`` sentinel from templates/macos/
    com.example.modworker.plist.tmpl, so the piped script needs no repo
    checkout and no sibling plist download (see _macos_plist_template).
    """
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "worker_install_macos.sh"
    )

    if not os.path.exists(script_path):
        raise HTTPException(
            status_code=404,
            detail="macOS install script not found — run a server deploy to stage it",
        )

    with open(script_path, "r") as f:
        content = f.read()

    # Same inlining the token-gated /agent/install-script does: the plist
    # template carries no secrets, so it is safe to serve unauthenticated.
    tmpl = _macos_plist_template()
    if tmpl and "__MOD_PLIST_TEMPLATE__" in content:
        content = content.replace("__MOD_PLIST_TEMPLATE__", tmpl)

    # API_URL is injected BARE (scheme+host, no /v1) exactly like get_setup_script
    # — the installer persists it verbatim as the worker's api_url. It REPLACES
    # the installer's own `API_URL=""` init line (rather than being prepended)
    # because the script's arg-parsing block re-initialises the variable and
    # would clobber a prepended default; substituting the init line keeps the
    # shebang at line 1 (body still starts with `#!`) and makes
    # `curl … | bash -s -- --token T` work with no --api-url. --api-url on the
    # command line still wins (the arg parser assigns API_URL after this line).
    api_url = _install_api_base(request)
    if 'API_URL=""' in content:
        content = content.replace('API_URL=""', 'API_URL="${API_URL:-' + api_url + '}"', 1)
    else:
        lines = content.split("\n", 1)
        content = (
            lines[0] + '\nAPI_URL="${API_URL:-' + api_url + '}"\n' + (lines[1] if len(lines) > 1 else "")
        )

    return StreamingResponse(
        iter([content]),
        media_type="text/x-shellscript",
        headers={"Content-Disposition": 'inline; filename="install-macos.sh"'},
    )


# api-lint: public-by-design: serves compiled worker binaries which contain no secrets
@router.get("/download/{os_name}/{arch}", response_model=None)
def download_worker_binary(os_name: str, arch: str):
    """
    Download the worker agent binary. No auth required — the binary
    contains no secrets.

    Used by setup-worker.sh to pull the binary onto a new machine.
    Binaries are stored in MinIO worker-releases bucket by start-main-server.sh.
    """
    os_name = os_name.lower().strip()
    arch = arch.lower().strip()

    if os_name not in ("linux", "darwin"):
        raise HTTPException(status_code=400, detail=f"Unsupported OS: {os_name}")
    if arch not in ("amd64", "arm64"):
        raise HTTPException(status_code=400, detail=f"Unsupported arch: {arch}")

    object_name = f"mod-worker-{os_name}-{arch}"
    stream = _get_minio_binary(object_name)

    if stream is None:
        raise HTTPException(
            status_code=404,
            detail=_binary_missing_detail(os_name, arch),
        )

    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers=_binary_headers(stream, object_name, os_name, arch),
    )

# api-lint: public-by-design: serves official Tailscale static builds, public binaries no secrets
@router.get("/download/tailscale/{os_name}/{arch}", response_model=None)
def download_tailscale(os_name: str, arch: str):
    """
    Download the manager's pinned Tailscale client tarball (official static
    build: tailscale + tailscaled). No auth required — public binaries, no
    secrets.

    Used by setup-worker.sh step 6b so every worker installs the SAME
    tailscale from its manager instead of reaching tailscale.com (blocked on
    filtered/offline site networks). Mirrored into worker-releases by
    startup/ensure-tailscale-mirror.sh during manager install/update; 404
    tells the script to fall back to tailscale.com.
    """
    os_name = os_name.lower().strip()
    arch = arch.lower().strip()

    if os_name != "linux":
        raise HTTPException(status_code=400, detail=f"Unsupported OS: {os_name}")
    if arch not in ("amd64", "arm64"):
        raise HTTPException(status_code=400, detail=f"Unsupported arch: {arch}")

    object_name = f"tailscale-{os_name}-{arch}.tgz"
    stream = _get_minio_binary(object_name)

    if stream is None:
        raise HTTPException(
            status_code=404,
            detail=f"Tailscale mirror not found for {os_name}/{arch}. Run startup/ensure-tailscale-mirror.sh on the manager.",
        )

    return StreamingResponse(
        stream,
        media_type="application/gzip",
        headers=_binary_headers(stream, object_name),
    )


# sz-mac-worker.6 — versioned worker release channel. The unversioned
# /download/{os}/{arch} above only ever serves whatever the manager last
# published; a Mac (or any worker) that wants a SPECIFIC build, or the newest
# build of a channel, asks this route. Keys are
# `worker-releases/v{version}/mod-worker-{os_arch}` (windows keeps its `.exe`
# via `_worker_object_name()`, matching publish-worker-release.sh) so a
# publish never clobbers an older one; `latest` is the rolling channel pointer.
_WORKER_OS_ARCH = frozenset(
    {"linux-amd64", "linux-arm64", "darwin-arm64", "darwin-amd64", "windows-amd64"}
)
_SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


@router.get("/releases/worker/{version}/{os_arch}", response_model=None)
def download_worker_release(
    version: str,
    os_arch: str,
    current_user: User = Depends(get_current_user),
):
    """
    Download a worker binary for any os/arch pair (macOS included), GET only.

    Auth: authenticated user (same `get_current_user` dependency every other
    GET in this router uses), NOT public. The unversioned
    GET /workers/download/{os}/{arch} above is public, but adding a second
    unauthenticated binary route would need a matching entry in
    tests/test_authz_endpoint_coverage.py's PUBLIC_ALLOWLIST (that file is
    outside this job's 2-file budget), so this channel stays credential-bound.
    Installers already hold the enroll/bootstrap token, so a bearer header is
    available at the call site; the token-gated /agent/download stays the
    single-use channel. 404 when the object was never published.
    """
    version = version.strip().lower()
    os_arch = os_arch.strip().lower()

    if os_arch not in _WORKER_OS_ARCH:
        raise HTTPException(
            status_code=404,
            detail=f"No worker release channel for {os_arch}.",
        )
    if version != "latest" and not _SEMVER_RE.fullmatch(version):
        raise HTTPException(
            status_code=404,
            detail=f"Unknown worker release version {version}.",
        )

    os_name, arch = os_arch.rsplit("-", 1)
    filename = _worker_object_name(os_name, arch)
    object_name = f"v{version}/{filename}"
    stream = _get_minio_binary(object_name)

    if stream is None:
        raise HTTPException(
            status_code=404,
            detail=f"Worker binary not found for {os_arch} at {version}.",
        )

    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers=_binary_headers(stream, filename, os_name, arch),
    )
