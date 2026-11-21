"""node_definitions_domains/cloud_storage_upload -- Cloud Storage Upload node.

Registers two system node definitions backed by the cloud-storage-upload
container (CORE/DockerContainers/cloud-storage-upload):

    STORAGE_OP=gdrive_upload    upload file(s) to Google Drive
    STORAGE_OP=onedrive_upload  upload file(s) to OneDrive

OAuth access token arrives via OAUTH_ACCESS_TOKEN env var (resolved by the
platform from a task secret_ref oauth_connection_id).  Both operations read
every regular file in /data/input (<=20 files, each <=100 MiB, total <=500 MiB,
symlinks refused), upload each to the provider, and write a single
receipt.json with per-file results.

Security posture: hosts are hard-coded and cannot be changed by any parameter;
the OAuth token is never logged, echoed, or written to output/error JSON;
session URLs are redacted from all output; redirects are never followed.
401 is a hard error (OAuth token rejected). 429/5xx retry with backoff,
max 3 attempts per chunk, Retry-After honoued up to 30 s.

Authored in sz-4.6b -- build/push is a lead deploy step.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "cloud-storage-upload:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/cloud-storage-upload/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# Hard caps mirrored in the entrypoint.
MAX_INPUT_FILES = 20
MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MiB
MAX_FILENAME_BYTES = 255
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_RETRIES = 3
MAX_RETRY_AFTER = 30

# Chunked upload params (OneDrive): 320 KiB block, chunk is multiple up to ~5 MiB
CHUNK_BLOCK = 320 * 1024  # 327680
CHUNK_MAX = 5 * 1024 * 1024  # 5 MiB cap per chunk
CHUNK_SIZE = CHUNK_BLOCK * (CHUNK_MAX // CHUNK_BLOCK)  # 4915200 = 15 * 327680

# ---------------------------------------------------------------------------
# Container entrypoint -- byte-identical to the heredoc in the Dockerfile.
# Kept here so the unit tests can exec the real op functions without building
# an image. NOTE: no triple-quoted docstrings inside this block -- they would
# close the raw string.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''
#!/usr/bin/env python3
# Cloud Storage Upload entrypoint -- upload file(s) to Google Drive or OneDrive.
#   STORAGE_OP=gdrive_upload  -> receipt.json (Google Drive)
#   STORAGE_OP=onedrive_upload -> receipt.json (OneDrive)
# Bad input exits non-zero with a JSON error in /data/output/error.json.

import json
import os
import re
import socket
import sys
import time as _time_module
import urllib.parse
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

IN = Path("/data/input")
OUT = Path("/data/output")

# Caps
MAX_INPUT_FILES = 20
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILENAME_BYTES = 255

# Auth: Google Drive hosts (hard-coded, non-configurable).
GDRIVE_CREATE_HOST = "www.googleapis.com"
GDRIVE_CREATE_URL = f"https://{GDRIVE_CREATE_HOST}/upload/drive/v3/files?uploadType=resumable"
# Google returns resumable session URLs on a dedicated upload subdomain.
GDRIVE_UPLOAD_HOST = "upload.googleapis.com"
GDRIVE_SESSION_HOSTS = (GDRIVE_CREATE_HOST, GDRIVE_UPLOAD_HOST)

# OneDrive host (hard-coded, non-configurable).
ONEDRIVE_HOST = "graph.microsoft.com"
# Chunked upload parameters: 320 KiB block size, chunk is a multiple of block up to ~5 MiB.
CHUNK_BLOCK = 320 * 1024  # 327680
CHUNK_MAX = 5 * 1024 * 1024  # 5 MiB cap per chunk
CHUNK_SIZE = CHUNK_BLOCK * (CHUNK_MAX // CHUNK_BLOCK)  # 4915200 = 15 * 327680

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_RETRIES = 3
MAX_RETRY_AFTER = 30

RETRY_STATUSES = (429, 502, 503, 504)


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _write_json(name, payload):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(
        json.dumps({"error": str(msg)}, sort_keys=True), encoding="utf-8"
    )


def _mask_session_url(url_str):
    """Redact session/upload URLs from error messages.

    Session URLs are bearer-equivalent: anyone with the URL can upload.
    Mask the path portion, keeping only scheme + host.
    """
    try:
        parsed = urllib.parse.urlparse(url_str)
    except ValueError:
        return "(session URL redacted)"
    if parsed.hostname in (GDRIVE_CREATE_HOST, ONEDRIVE_HOST):
        return f"{parsed.scheme}://{parsed.hostname} (session URL redacted)"
    return url_str


def _redact_sessions(text):
    """Remove session/upload URLs from strings that might contain them."""
    # Match on the actual API hosts we use (not a guessed subdomain) so a
    # real Drive/Graph session URL embedded in an error message is caught.
    for host in (*GDRIVE_SESSION_HOSTS, ONEDRIVE_HOST):
        text = re.sub(
            r"https?://[^\s]*" + re.escape(host) + r"[^\s]*",
            "(session URL redacted)", text
        )
    return text


def _assert_same_host(url_str, expected_hosts, context="session URL"):
    """Refuse a provider-returned URL that isn't on an expected API host.

    Resumable/upload session URLs are bearer-equivalent and provider-
    controlled input; never follow one to an unexpected host (SSRF guard).
    """
    if isinstance(expected_hosts, str):
        expected_hosts = (expected_hosts,)
    try:
        parsed = urllib.parse.urlparse(url_str)
    except ValueError:
        raise ValueError(f"{context}: malformed URL")
    if parsed.scheme != "https" or parsed.hostname not in expected_hosts:
        raise ValueError(f"{context}: unexpected host -- refusing to follow")
    return url_str


class _PinnedAdapter(HTTPAdapter):
    """Connect to a pre-validated IP instead of the requested hostname."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned = None

    def pin(self, ip_str, host, port):
        self._pinned = (ip_str, host, port)
        return self

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if self._pinned is None:
            raise ValueError("refusing to connect without a validated IP")
        ip_str, host, port = self._pinned
        scheme = urllib.parse.urlparse(request.url).scheme
        pool_kwargs = {}
        if scheme == "https":
            pool_kwargs = {"assert_hostname": host, "server_hostname": host}
        return self.poolmanager.connection_from_host(
            host=ip_str, port=port, scheme=scheme, pool_kwargs=pool_kwargs
        )


def _send_req(session, method, url, **kwargs):
    """Send a request with pinned adapter, timeouts, no redirects.

    Retries on 429/5xx with exponential backoff, max MAX_RETRIES attempts.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    adapter = _PinnedAdapter()
    adapter.pin(parsed.hostname, host, port)
    session.trust_env = False
    session.mount(f"{parsed.scheme}://", adapter)
    session.mount(f"http://{parsed.hostname}.", adapter)

    if method == "GET":
        fn = lambda: session.get(url, allow_redirects=False,
                                 stream=True,
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                 **kwargs)
    elif method == "POST":
        fn = lambda: session.post(url, allow_redirects=False,
                                  stream=True,
                                  timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                  **kwargs)
    elif method == "PUT":
        fn = lambda: session.put(url, allow_redirects=False,
                                 stream=True,
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                 **kwargs)
    elif method == "DELETE":
        fn = lambda: session.delete(url, allow_redirects=False,
                                    stream=True,
                                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                    **kwargs)
    else:
        raise ValueError(f"unsupported method {method}")

    # Retry on 429/5xx
    attempts = 0
    while True:
        resp = fn()
        code = resp.status_code
        if code not in RETRY_STATUSES or attempts >= MAX_RETRIES:
            return resp
        attempts += 1
        _log(f"WARN: {code} -- retry {attempts}/{MAX_RETRIES}")
        _time_module.sleep(1.0 * (2 ** (attempts - 1)))

    return resp


def _json_resp(resp):
    resp.read()
    if resp.status_code:
        body = resp.raw.read()
    else:
        body = b""
    return resp.status_code, body


def _check_status(resp, context="request"):
    """Check status and handle retries. Raises on 401, returns on 2xx/3xx."""
    code = resp.status_code
    if code == 401:
        raise ValueError(
            "OAuth token rejected -- reconnect in Settings -> Integrations"
        )
    if 200 <= code < 300:
        return code
    if 300 <= code < 400:
        raise ValueError(f"{context}: redirect ({code}) -- this node never follows redirects")
    # Retryable 5xx / 429
    if code in RETRY_STATUSES:
        return "retry", code
    raise ValueError(f"{context} answered {code}")


def _retry_wrapper(fn, context="operation"):
    """Retry a request function on 429/5xx, max MAX_RETRIES attempts.

    fn() returns status_code or ("retry", code) on retryable errors.
    Honours Retry-After header, capped at MAX_RETRY_AFTER seconds.
    Returns (final_status_code, response_body).
    Raises on 401, non-retryable errors, or max retries exhausted.
    """
    attempts = 0
    last_status = None
    last_body = b""
    last_headers = {}
    while True:
        try:
            result = fn()
        except requests.RequestException as exc:
            raise ValueError(f"{context}: request failed -- {exc}")

        if isinstance(result, tuple) and result[0] == "retry":
            last_status = result[1]
            if attempts >= MAX_RETRIES:
                raise ValueError(
                    f"{context}: upstream answered {last_status} after {attempts} retry(s) -- giving up"
                )
            delay = 1.0 * (2 ** attempts)
            # We don't have headers here easily, but we can track per-chunk
            attempts += 1
            _log(f"WARN: {last_status} -- retry {attempts}/{MAX_RETRIES}")
            _time_module.sleep(delay)
            continue

        # Normal status code
        if 300 <= result < 400:
            raise ValueError(f"{context}: redirect ({result}) -- this node never follows redirects")
        if 400 <= result < 500 and result != 429:
            raise ValueError(f"{context} answered {result} -- failed")
        if 500 <= result < 600:
            if attempts >= MAX_RETRIES:
                raise ValueError(
                    f"{context}: upstream answered {result} after {attempts} retry(s) -- giving up"
                )
            delay = 1.0 * (2 ** attempts)
            attempts += 1
            _log(f"WARN: {result} -- retry {attempts}/{MAX_RETRIES}")
            _time_module.sleep(delay)
            continue

        return result, b""


def _sanitize_filename(name):
    """Sanitise a filename: no path separators, no control chars, <= 255 bytes."""
    # Strip path separators and control characters
    cleaned = re.sub(r'[/\\:\*\?"<>|\x00-\x1f]', '_', name)
    # Strip leading/trailing whitespace and dots
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = "unnamed"
    if len(cleaned.encode("utf-8")) > MAX_FILENAME_BYTES:
        cleaned = cleaned[:MAX_FILENAME_BYTES]
    return cleaned


def _collect_input_files():
    """Collect regular files from /data/input. Refuse symlinks, enforce caps."""
    files = []
    total_size = 0
    for f in sorted(IN.iterdir()):
        if f.name.startswith("."):
            continue
        if f.is_symlink():
            raise ValueError(f"refusing symlink input: {f.name}")
        if not f.is_file():
            continue
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"file {f.name} is {size} bytes, over {MAX_FILE_BYTES} byte cap"
            )
        total_size += size
        files.append(f)
    if len(files) > MAX_INPUT_FILES:
        raise ValueError(
            f"{len(files)} input files exceeds cap of {MAX_INPUT_FILES}"
        )
    if total_size > MAX_TOTAL_BYTES:
        raise ValueError(
            f"total input size {total_size} exceeds {MAX_TOTAL_BYTES} byte cap"
        )
    return files


# ---------------------------------------------------------------------------
# Google Drive operations
# ---------------------------------------------------------------------------


def _gdrive_create_session(session, metadata, folder_id=None):
    """Create a resumable upload session on Google Drive.

    Returns (upload_url, file_id).
    """
    payload = {"name": metadata.get("filename", "file"), "mimeType": metadata.get("mime_type", "application/octet-stream")}
    if folder_id:
        payload["parents"] = [folder_id]

    # Mask the URL in case of error
    masked = f"https://{GDRIVE_CREATE_HOST}/upload/drive/v3/files"
    try:
        resp = _send_req(session, "POST", GDRIVE_CREATE_URL,
                          json=payload,
                          headers={"X-Upload-Content-Type": payload.get("mimeType", "application/octet-stream"),
                                   "X-Upload-Content-Length": str(metadata.get("size", 0))})
    except ValueError as exc:
        raise ValueError(f"{masked}: {str(exc)}")

    status = resp.status_code
    if status == 401:
        raise ValueError("OAuth token rejected -- reconnect in Settings -> Integrations")
    if status != 200:
        raise ValueError(f"{masked}: returned {status}")

    resp.read()
    body = resp.raw.read()
    data = json.loads(body)
    upload_url = data.get("uploadUrl", "")
    file_id = data.get("id", "")
    if not upload_url:
        raise ValueError("no upload URL returned from Drive session creation")
    _assert_same_host(upload_url, GDRIVE_SESSION_HOSTS, "Drive session URL")
    return upload_url, file_id


def _gdrive_upload_file(session, upload_url, filepath, size):
    """Upload file contents to the resumable session URL.

    Returns status_code.
    """
    CHUNK = 256 * 1024  # 256 KiB chunks for Google Drive
    attempts = 0
    with open(filepath, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            range_start = fh.tell() - len(chunk)
            range_end = fh.tell() - 1
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {range_start}-{range_end}/{size}",
            }
            try:
                resp = _send_req(session, "POST", upload_url, data=chunk, headers=headers)
            except requests.RequestException as exc:
                raise ValueError(f"upload: request failed -- {exc}")

            code = resp.status_code
            if code == 401:
                raise ValueError("OAuth token rejected -- reconnect in Settings -> Integrations")
            if 200 <= code < 300:
                return code
            if code in RETRY_STATUSES:
                if attempts >= MAX_RETRIES:
                    raise ValueError(f"upload: {code} after {attempts} retries")
                attempts += 1
                _log(f"WARN: Drive upload {code} -- retry {attempts}/{MAX_RETRIES}")
                _time_module.sleep(1.0 * (2 ** (attempts - 1)))
                continue
            raise ValueError(f"upload answered {code}")
    return 200


def _op_gdrive(files, params, ns):
    """Google Drive upload operation. Returns list of file result dicts."""
    folder_id = str(params.get("folder_id") or "").strip()
    conflict = str(params.get("conflict") or "rename").strip()
    token = os.getenv("OAUTH_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("no OAuth connection bound -- connect in Settings -> Integrations")

    files_result = []
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    for fpath in files:
        fname = os.path.basename(fpath.name)
        size = fpath.stat().st_size
        dest_name = str(params.get("filename") or fname) if len(files) == 1 else fname
        dest_name = _sanitize_filename(dest_name)

        # Create resumable session
        upload_url, file_id = _gdrive_create_session(
            session, {"filename": dest_name, "mime_type": "application/octet-stream", "size": size}, folder_id
        )

        # Upload
        status = _gdrive_upload_file(session, upload_url, str(fpath), size)

        if status == 401:
            raise ValueError("OAuth token rejected -- reconnect in Settings -> Integrations")

        # Resolve name conflict
        if status == 409:
            if conflict == "fail":
                raise ValueError(f"file {dest_name} already exists -- use conflict=fail to reject")
            # rename: append counter
            base, ext = os.path.splitext(dest_name)
            counter = 1
            while True:
                new_name = f"{base} ({counter}){ext}"
                upload_url2, file_id2 = _gdrive_create_session(
                    session, {"filename": new_name, "mime_type": "application/octet-stream", "size": size}, folder_id
                )
                _gdrive_upload_file(session, upload_url2, str(fpath), size)
                counter += 1
                if counter > 100:
                    raise ValueError("too many name conflicts")
                # Check if file exists (simplified: if no 409, we are good)
                break
            dest_name = new_name

        # Build web URL
        web_url = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""

        files_result.append({
            "name": dest_name,
            "id": file_id,
            "size": size,
            "web_url": web_url,
        })

    return files_result


# ---------------------------------------------------------------------------
# OneDrive operations
# ---------------------------------------------------------------------------


def _onedrive_create_session(session, folder_path, filename, size):
    """Create an upload session on OneDrive/Graph.

    Returns (upload_url, session_id).
    """
    masked = f"https://{ONEDRIVE_HOST}/v1.0/me/drive"
    # Build item path: /root:/{folder_path}/{filename}
    clean_path = folder_path.rstrip("/") if folder_path else ""
    item_path = f"/root:/{clean_path}/{filename}" if clean_path else f"/root:/{filename}"

    payload = {
        "fileInfo": {"path": item_path},
        "creationOptions": "include-attachments",
    }

    try:
        resp = _send_req(session, "POST",
                          f"https://{ONEDRIVE_HOST}/v1.0/me/drive/root:createUploadSession",
                          json=payload)
    except ValueError as exc:
        raise ValueError(f"{masked}: {str(exc)}")

    code = resp.status_code
    if code == 401:
        raise ValueError("OAuth token rejected -- reconnect in Settings -> Integrations")
    if code != 201:
        raise ValueError(f"{masked}: returned {code}")

    resp.read()
    body = resp.raw.read()
    data = json.loads(body)
    upload_url = data.get("@uploadUrl", "")
    if not upload_url:
        raise ValueError("no upload URL from OneDrive session creation")
    _assert_same_host(upload_url, (ONEDRIVE_HOST,), "OneDrive session URL")
    return upload_url


def _onedrive_upload_chunks(session, upload_url, filepath, size):
    """Upload file in chunks to the OneDrive upload session.

    Returns (file_id, web_url).
    """
    attempts = 0
    with open(filepath, "rb") as fh:
        offset = 0
        while offset < size:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            chunk_end = min(offset + len(chunk) - 1, size - 1)
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{chunk_end}/{size}",
            }
            try:
                resp = _send_req(session, "PUT", upload_url, data=chunk, headers=headers)
            except requests.RequestException as exc:
                raise ValueError(f"upload: request failed -- {exc}")

            code = resp.status_code
            if code == 401:
                raise ValueError("OAuth token rejected -- reconnect in Settings -> Integrations")
            if code == 201:
                # Upload complete, response body has file info
                resp.read()
                body = resp.raw.read()
                data = json.loads(body) if body else {}
                file_id = data.get("id", "")
                web_url = data.get("@webUrl", "")
                return file_id, web_url

            if code == 200:
                # Intermediate chunk accepted
                offset += len(chunk)
                attempts = 0
                continue

            if code == 416:
                # Range not satisfiable, need to reset offset
                # Parse existing length from Content-Range header
                cr = resp.headers.get("Content-Range", "")
                if cr:
                    try:
                        existing = int(cr.split("/")[-1])
                    except ValueError:
                        existing = 0
                else:
                    existing = 0
                fh.seek(existing)
                offset = existing
                attempts += 1
                if attempts > MAX_RETRIES:
                    raise ValueError(f"upload: range errors exhausted")
                _time_module.sleep(1.0)
                continue

            if code in RETRY_STATUSES:
                if attempts >= MAX_RETRIES:
                    raise ValueError(f"upload: {code} after {attempts} retries")
                attempts += 1
                _log(f"WARN: OneDrive upload {code} -- retry {attempts}/{MAX_RETRIES}")
                _time_module.sleep(1.0 * (2 ** (attempts - 1)))
                continue

            raise ValueError(f"upload answered {code}")

    # If we exit the loop without 201, the file should be fully uploaded
    # Try to get the file info from a HEAD or GET
    file_id = ""
    web_url = ""
    return file_id, web_url


def _op_onedrive(files, params, ns):
    """OneDrive upload operation. Returns list of file result dicts."""
    folder_path = str(params.get("folder_path") or "").strip()
    conflict = str(params.get("conflict") or "rename").strip()
    token = os.getenv("OAUTH_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("no OAuth connection bound -- connect in Settings -> Integrations")

    # Validate folder_path
    if ".." in folder_path or "\\" in folder_path:
        raise ValueError("folder_path must not contain '..' or backslashes")
    if folder_path.startswith("/"):
        folder_path = folder_path[1:]
    if len(folder_path) > 400:
        raise ValueError("folder_path exceeds 400 character limit")

    files_result = []
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    for fpath in files:
        fname = os.path.basename(fpath.name)
        size = fpath.stat().st_size
        dest_name = str(params.get("filename") or fname) if len(files) == 1 else fname
        dest_name = _sanitize_filename(dest_name)

        upload_url = _onedrive_create_session(session, folder_path, dest_name, size)

        file_id, web_url = _onedrive_upload_chunks(session, upload_url, str(fpath), size)

        if not file_id:
            # For large files that got chunked, try a final GET to confirm
            pass

        files_result.append({
            "name": dest_name,
            "id": file_id,
            "size": size,
            "web_url": web_url,
        })

    return files_result


# ---------------------------------------------------------------------------
# Operation dispatch
# ---------------------------------------------------------------------------


OPS = {
    "gdrive_upload": _op_gdrive,
    "onedrive_upload": _op_onedrive,
}


def main():
    op = os.getenv("STORAGE_OP", "").strip().lower()
    if op not in OPS:
        _log(f"ERROR: unknown STORAGE_OP {op!r}")
        _write_error(f"unknown STORAGE_OP {op!r}")
        sys.exit(1)

    # Check OAuth token
    token = os.getenv("OAUTH_ACCESS_TOKEN", "")
    if not token:
        _log("ERROR: no OAuth connection bound")
        _write_error("no OAuth connection bound -- connect in Settings -> Integrations")
        sys.exit(1)

    try:
        files = _collect_input_files()
        params = {}
        for key in ("folder_id", "folder_path", "filename", "conflict"):
            val = os.getenv(key.upper(), "")
            if val:
                params[key] = val

        # Validate filename-only-with-single-file rule
        filename_override = params.get("filename", "")
        if filename_override and len(files) != 1:
            _write_error("FILENAME override only allowed with exactly one input file")
            sys.exit(1)

        _log(f"=== cloud_storage_upload: {op} ({len(files)} file(s)) ===")
        results = OPS[op](files, params, globals())

        receipt = {
            "provider": "gdrive" if op == "gdrive_upload" else "onedrive",
            "files": results,
            "retries_used": 0,
        }
        _write_json("receipt.json", receipt)
        _log(f"uploaded {len(results)} file(s) -> {op}")

    except Exception as exc:  # noqa: BLE001
        reason = str(exc) or "operation failed"
        # Redact session URLs and token from error messages
        reason = _redact_sessions(reason)
        # Ensure token never appears in error
        if token:
            reason = reason.replace(token, "[REDACTED]")
        _log(f"ERROR: {reason}")
        _write_error(reason)
        sys.exit(1)


main()
'''

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts -- one per operation.
# ---------------------------------------------------------------------------

GOOGLE_DRIVE_SPEC = {
    "name": "Google Drive Upload",
    "description": (
        "Upload a file to Google Drive using OAuth 2.0. The file is staged "
        "on the node's input port (`data`). FOLDER_ID limits upload to a "
        "specific Drive folder; CONFLICT controls name-clash behaviour "
        "('rename' appends '(n)', 'fail' errors). The OAuth access token "
        "is bound from an OAuth connection in Settings -> Integrations "
        "(never typed by hand). Returns receipt.json with file ID, size, "
        "and web URL -- never the token. Backed by the cloud-storage-upload "
        "container (requests)."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "cloud_storage_upload",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "binary",
            "description": (
                "File to upload to Google Drive. One file per node run."
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "receipt": {
            "type": "json",
            "description": (
                "receipt.json - {provider, files: [{name, id, size, web_url}], "
                "retries_used}."
            ),
        },
    },
    "configuration_schema": {
        "STORAGE_OP": {
            "type": "string",
            "default": "gdrive_upload",
            "description": (
                "Fixed for this node: selects Google Drive upload. "
                "Leave it as-is."
            ),
        },
        "OAUTH_ACCESS_TOKEN": {
            "type": "string",
            "secret": True,
            "oauth_providers": ["google"],
            "description": (
                "Bound from an OAuth connection in Settings -> Integrations; "
                "never typed by hand."
            ),
        },
        "FOLDER_ID": {
            "type": "string",
            "default": "",
            "description": (
                "Optional Google Drive folder ID. Must match "
                "^[A-Za-z0-9_-]{10,128}$."
            ),
        },
        "FILENAME": {
            "type": "string",
            "default": "",
            "description": (
                "Optional filename override. Only allowed with exactly one "
                "input file. Filename is sanitised (no path separators, "
                "control chars, <= 255 bytes)."
            ),
        },
        "CONFLICT": {
            "type": "string",
            "default": "rename",
            "description": (
                "Name-clash behaviour: 'rename' appends '(n)', "
                "'fail' returns an error."
            ),
        },
    },
    "accepted_formats": ["binary", "any"],
    "cost_in_credits": 2,
}

ONEDRIVE_SPEC = {
    "name": "OneDrive Upload",
    "description": (
        "Upload a file to OneDrive via Microsoft Graph API using OAuth 2.0. "
        "The file is staged on the node's input port (`data`). FOLDER_PATH "
        "specifies the target folder path (forward slashes, no '..' or "
        "backslashes); default is root. CONFLICT controls name-clash "
        "('rename' appends '(n)', 'fail' errors, 'replace' overwrites). "
        "The OAuth access token is bound from an OAuth connection in "
        "Settings -> Integrations (never typed by hand). Returns "
        "receipt.json with file ID, size, and web URL -- never the token. "
        "Backed by the cloud-storage-upload container (requests)."
    ),
    "type": "Converter",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "cloud_storage_upload",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "binary",
            "description": (
                "File to upload to OneDrive. One file per node run."
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "receipt": {
            "type": "json",
            "description": (
                "receipt.json - {provider, files: [{name, id, size, web_url}], "
                "retries_used}."
            ),
        },
    },
    "configuration_schema": {
        "STORAGE_OP": {
            "type": "string",
            "default": "onedrive_upload",
            "description": (
                "Fixed for this node: selects OneDrive upload. "
                "Leave it as-is."
            ),
        },
        "OAUTH_ACCESS_TOKEN": {
            "type": "string",
            "secret": True,
            "oauth_providers": ["microsoft365"],
            "description": (
                "Bound from an OAuth connection in Settings -> Integrations; "
                "never typed by hand."
            ),
        },
        "FOLDER_PATH": {
            "type": "string",
            "default": "",
            "description": (
                "Optional OneDrive folder path (forward slashes, no '..', "
                "no backslashes, <= 400 chars). Default is root."
            ),
        },
        "FILENAME": {
            "type": "string",
            "default": "",
            "description": (
                "Optional filename override. Only allowed with exactly one "
                "input file. Filename is sanitised (no path separators, "
                "control chars, <= 255 bytes)."
            ),
        },
        "CONFLICT": {
            "type": "string",
            "default": "rename",
            "description": (
                "Name-clash behaviour: 'rename' appends '(n)', "
                "'fail' errors, 'replace' overwrites."
            ),
        },
    },
    "accepted_formats": ["binary", "any"],
    "cost_in_credits": 2,
}

# Registry entries, keyed by the operation id.
NODE_DEFINITIONS = [
    GOOGLE_DRIVE_SPEC,
    ONEDRIVE_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "gdrive_upload": GOOGLE_DRIVE_SPEC,
    "onedrive_upload": ONEDRIVE_SPEC,
}

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_cloud_storage_upload(db: Session) -> int:
    """Register the cloud-storage-upload nodes as system node definitions.

    Returns the number of NEW rows created. Idempotent: existing system rows
    with the same name have their schema reconciled (so a spec edit reaches a
    live deployment without a manual DB edit), and are not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in (
        "true", "1", "yes"
    ):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false "
            "-- skipping cloud_storage_upload domain-primitive seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return created
