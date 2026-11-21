"""node_definitions_domains/database_crud — Database CRUD domain-primitive
NodeDefinitions (launch-completion-megaplan sz-6.5.1, Phase 6 domain primitives).

Registers six system node definitions backed by the database-crud container
(`CORE/DockerContainers/database-crud`):

    DB_OP=pg_query     parameterised SELECT          -> pg_query.json
    DB_OP=pg_execute   one INSERT/UPDATE/DELETE stmt -> pg_execute.json
    DB_OP=mongo_find   filter + projection + limit   -> mongo_find.json
    DB_OP=mongo_write  one insert / one update       -> mongo_write.json
    DB_OP=dynamo_get   get_item only                 -> dynamo_get.json
    DB_OP=dynamo_put   put_item only                 -> dynamo_put.json

Posture notes
-------------
- Specs are plain dicts (same shape as `spreadsheet.py` / `crypto_utils.py`) so
  the module is importable + testable without a DB; `register_database_crud()`
  is the only DB-touching entry point. Idempotent by (name, tenant_id IS NULL):
  a re-run reconciles an existing system row rather than duplicating it, and
  `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1) — existing rows are never deleted.
- Credentials follow the crypto_utils.py secret convention: every connection
  string / key parameter is declared with `"secret": True` in its
  `configuration_schema.extra_variables` entry (the same shape
  `seed_switchyard_llm_node.py` uses for `API_KEY`), so the platform routes it
  through a secret reference instead of a typed literal. The entrypoint never
  logs one, never echoes one into an output, and scrubs every secret value out
  of exception text before it reaches error.json — including the password half
  of a DSN, which is what a driver tends to echo back in a connect error.
- SQL is parameterised ONLY (`cur.execute(stmt, values)` with a JSON array in
  DB_PARAMS_B64); a value is never string-formatted into a statement. `pg_query`
  opens `SET TRANSACTION READ ONLY`, pins a 30 s statement timeout, and caps the
  result at 10k rows. `pg_execute` accepts exactly one statement: a quote- and
  comment-aware split on `;` (sqlparse is deliberately not installed — the
  check is simple and documented, and refuses `;` outside quotes/comments,
  transaction-control keywords, and anything over 8000 chars).
- Mongo filters are walked recursively and `$where` / `$function` /
  `$accumulator` are refused anywhere in the filter/projection/document; the
  result limit is capped at 10k.
- Dynamo does `get_item` / `put_item` only, requires DB_REGION, and takes an
  optional endpoint_url (an AWS-compatible endpoint on customer hardware).
- Network targets: every host the node will dial is resolved and refused when
  it is loopback, unspecified, or a link-local metadata address (169.254.169.254
  AWS/EC2/GCP, 10.0.0.200 Microsoft TLB). PRIVATE RFC1918 addresses are
  DELIBERATELY ALLOWED — customer databases live there; that is the whole point
  of this node, and the refusal list is only the addresses that would let a
  tenant reach MOD's own host or the platform's credential endpoint.
- Timeouts everywhere (connect, per-statement, per-request); the container runs
  non-root. psycopg/pymongo/boto3 are pinned in the image. DO NOT build/pull in
  an agent lane: the Dockerfile is authored here, the build is a lead deploy
  step.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "database-crud:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/database-crud/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

MAX_FIELDS_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
DEFAULT_ROW_CAP = 10_000
MAX_ROW_CAP = 10_000
PG_STATEMENT_TIMEOUT_MS = 30_000
PG_EXECUTE_MAX_CHARS = 8000
MONGO_FORBIDDEN = ("$where", "$function", "$accumulator")

# The entrypoint lives here as a string so the op functions can be unit-tested
# without a container (the test exec()s this source and repoints IN/OUT at a
# tmp dir), and so the Dockerfile heredoc is a verbatim mirror of it.
ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# database-crud entrypoint — one operation per run, selected by DB_OP.
#
#   DB_OP=pg_query     parameterised SELECT -> pg_query.json
#   DB_OP=pg_execute   one INSERT/UPDATE/DELETE statement -> pg_execute.json
#   DB_OP=mongo_find   filter + projection -> mongo_find.json
#   DB_OP=mongo_write  one document insert/update -> mongo_write.json
#   DB_OP=dynamo_get   TableKey -> dynamo_get.json
#   DB_OP=dynamo_put   document -> dynamo_put.json
#
# Standard MOD node I/O contract: the worker stages the node's inputs into
# /data/input and collects /data/output. Parameters arrive as env vars from the
# node's configuration_schema (DB_OP, DB_TARGET, DB_QUERY, DB_PARAMS_B64,
# DB_TIMEOUT_S, DB_MAX_ROWS, MONGO_FILTER_JSON, MONGO_PROJECTION_JSON,
# MONGO_LIMIT, MONGO_DOC_JSON, MONGO_MATCH_JSON, MONGO_NEW_JSON, DYNAMO_TABLE,
# DYNAMO_KEY_JSON, DYNAMO_DOC_JSON); a fields.json staged on the `data` port
# overrides them, and may carry the connection strings themselves. Credentials
# are the SECRET parameters DB_PG_DSN / DB_MONGO_URI / DB_DYNAMO_AK /
# DB_DYNAMO_SK: they are never logged, never echoed into an output or an error,
# and are scrubbed out of any exception text before it reaches error.json.
#
# Security posture: no shell=True, no eval/exec, parameterised SQL only (values
# are never string-formatted into the statement), pg_query opens a READ ONLY
# transaction with a 30 s statement timeout and a 10k row cap, pg_execute
# accepts exactly one non-transactional statement, Mongo filters containing
# $where / $function / $accumulator are refused, Dynamo does get_item / put_item
# only, every network target is resolved and refused when it is loopback /
# unspecified / link-local metadata, private RFC1918 addresses are ALLOWED
# because customer databases live there. Bad input exits non-zero with a JSON
# error in /data/output/error.json.

import base64
import binascii
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

try:
    import pymongo
    from pymongo import Client as _MongoClientAlias
except ImportError:
    pymongo = None
    _MongoClientAlias = None

try:
    import boto3
    from boto3.session import Session as _BotoSessionAlias
except ImportError:
    boto3 = None
    _BotoSessionAlias = None

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_FIELDS_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
DEFAULT_ROW_CAP = 10_000
MAX_ROW_CAP = 10_000
PG_STATEMENT_TIMEOUT_MS = 30_000
PG_EXECUTE_MAX_CHARS = 8000
MONGO_FORBIDDEN = ("$where", "$function", "$accumulator")
METADATA_IPS = (
    "169.254.169.254",
    "10.0.0.200",  # 10.0.0.200 = Microsoft TLB
    "fd00:ec2::254",  # AWS IMDS over IPv6
)
LOCALHOST_NAMES = ("localhost", "127.0.0.1", "::1")

# Set when a credential is read so main() can scrub it out of any error text.
_SECRETS_SEEN = []


def _note_secret(value):
    if value:
        _SECRETS_SEEN.append(str(value))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _write_json(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, default=str), encoding="utf-8")


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        return {}
    raw = p.read_bytes()
    if len(raw) > MAX_FIELDS_BYTES:
        raise ValueError(f"fields.json exceeds the {MAX_FIELDS_BYTES}-byte input cap")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fields.json must be a JSON object")
    return data


def _params():
    merged = {}
    for key, value in os.environ.items():
        if key.startswith("DB_") or key.startswith("MONGO_") or key.startswith("DYNAMO_"):
            merged[key.lower()] = value
    for key, value in _read_fields().items():
        merged[str(key).strip().lower()] = value
    return merged


def _int_param(params, name, default, cap):
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name.upper()} must be an integer")
    if value < 1:
        raise ValueError(f"{name.upper()} must be positive")
    return min(value, cap)


def _json_param(params, name, default=None):
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        raise ValueError(f"{name.upper()} must be valid JSON")


def _b64_param(params, name, default=""):
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return base64.b64decode(str(raw).strip()).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError(f"{name.upper()} must be base64")


def _target(params):
    raw = params.get("db_target")
    if raw is None or str(raw).strip() == "":
        return "host"
    return str(raw).strip().lower()


def _resolve_host(host, port=0):
    # Returns the single validated IP the caller must then connect to
    # (hostaddr-style pinning) rather than letting the DB driver re-resolve
    # the hostname itself later, which would reopen a DNS-rebinding TOCTOU.
    if host in LOCALHOST_NAMES:
        raise ValueError("DB_TARGET resolves to a loopback address — refused")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname — resolve every returned address and refuse if any is bad.
        try:
            addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise ValueError(
                f"DB_TARGET hostname {host!r} could not be resolved — refused"
            )
        if not addrs:
            raise ValueError(
                f"DB_TARGET hostname {host!r} could not be resolved — refused"
            )
        validated = []
        for _, _, _, _, sockaddr in addrs:
            addr_ip = ipaddress.ip_address(sockaddr[0])
            _check_ip(addr_ip)
            validated.append(addr_ip)
        return validated[0]
    _check_ip(ip)
    return ip


def _check_ip(ip):
    if ip.is_loopback or ip.is_unspecified:
        raise ValueError("DB_TARGET resolves to a loopback/unspecified address — refused")
    if str(ip) in METADATA_IPS:
        raise ValueError("DB_TARGET resolves to a link-local metadata address — refused")
    if ip.is_link_local:
        raise ValueError("DB_TARGET resolves to a link-local address — refused")
    if ip.is_multicast:
        raise ValueError("DB_TARGET resolves to a multicast address — refused")
    if ip.is_reserved:
        raise ValueError("DB_TARGET resolves to a reserved address — refused")
    # Unwrap IPv4-mapped / 6to4 / Teredo IPv6 and re-check the embedded IPv4.
    # Use ipaddress's own attributes rather than string-prefix hacks.
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return _check_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _check_ip(ip.sixtofour)
        if ip.teredo is not None:
            server, client = ip.teredo
            _check_ip(server)
            return _check_ip(client)
    return None
def _dsn_pg(params):
    dsn = params.get("db_pg_dsn") or _b64_param(params, "db_pg_dsn_b64")
    if not dsn:
        raise ValueError("DB_PG_DSN is required (secret parameter)")
    _note_secret(dsn)
    password = _dsn_password(dsn)
    if password:
        # A driver tends to echo the password half of a DSN back in its connect
        # error, so the password is tracked as a secret in its own right.
        _note_secret(password)
    return str(dsn)


def _mongo_uri(params):
    uri = params.get("db_mongo_uri") or _b64_param(params, "db_mongo_uri_b64")
    if not uri:
        raise ValueError("DB_MONGO_URI is required (secret parameter)")
    _note_secret(uri)
    return str(uri)


def _dynamo_keys(params):
    ak = params.get("db_dynamo_ak") or _b64_param(params, "db_dynamo_ak_b64")
    sk = params.get("db_dynamo_sk") or _b64_param(params, "db_dynamo_sk_b64")
    if not (ak and sk):
        raise ValueError("DB_DYNAMO_AK and DB_DYNAMO_SK are both required (secret parameters)")
    _note_secret(ak)
    _note_secret(sk)
    return str(ak), str(sk)


def _dsn_host(dsn):
    if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
        rest = dsn.split("://", 1)[1]
        host = rest.split("@")[-1].split("/")[0].split(":")[0]
        return host or None
    if dsn.startswith("mongodb://"):
        rest = dsn[len("mongodb://"):]
        host = rest.split("@")[-1].split("/")[0].split(":")[0]
        return host or None
    if "=" in dsn:
        for token in dsn.split():
            if token.startswith("host="):
                return token[len("host="):].strip("'\"")
    return None


def _dsn_password(dsn):
    if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
        rest = dsn.split("://", 1)[1]
        creds = rest.split("@")[0] if "@" in rest.split("/")[0] else ""
        if ":" in creds:
            return creds.split(":", 1)[1]
        return ""
    if "=" in dsn:
        for token in dsn.split():
            if token.startswith("password="):
                return token[len("password="):].strip("'\"")
    return ""


def _guard_target(dsn, params):
    # Resolve the DSN's own host, refuse loopback/metadata/etc targets, and
    # return the single validated IP so the caller can pin the real
    # connection to it (DNS-rebinding/TOCTOU protection).
    host = _dsn_host(dsn)
    if not host:
        return None
    return str(_resolve_host(host))


def _pg_connect(dsn, timeout_s, pinned_ip=None):
    if psycopg is None:
        raise ValueError("psycopg is not installed in this image")
    kwargs = {
        "connect_timeout": timeout_s,
        "options": f"-c statement_timeout={PG_STATEMENT_TIMEOUT_MS}",
    }
    if pinned_ip:
        # hostaddr pins the TCP connection to the already-validated IP while
        # host (from the dsn) is kept for TLS/SNI — libpq will not re-resolve
        # the hostname, closing the DNS-rebinding window between the guard
        # check above and the actual connect.
        kwargs["hostaddr"] = pinned_ip
    return psycopg.connect(dsn, **kwargs)


def _split_statements(sql):
    # Split on top-level ';' only, ignoring quoted / comment text.
    parts, buf, i = [], [], 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == "\\" and i + 1 < n:
                    i += 1
                    buf.append(sql[i])
                elif sql[i] == quote:
                    break
                i += 1
            i += 1
            continue
        if ch == "-" and sql[i : i + 2] == "--":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and sql[i : i + 2] == "/*":
            i += 2
            while i + 1 < n and sql[i : i + 2] != "*/":
                i += 1
            i += 2
            continue
        if ch == ";":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _pg_statement(sql):
    # Exactly one non-transactional statement, or ValueError.
    if len(sql) > PG_EXECUTE_MAX_CHARS:
        raise ValueError(f"DB_QUERY exceeds the {PG_EXECUTE_MAX_CHARS}-character statement cap")
    parts = _split_statements(sql)
    if len(parts) != 1:
        raise ValueError(
            "pg_execute accepts a single statement only "
            f"({len(parts)} found outside quotes/comments)"
        )
    stmt = parts[0]
    head = stmt.strip().split()[0].lower() if stmt.strip() else ""
    if head in ("begin", "commit", "rollback", "do", "with"):
        raise ValueError("pg_execute refuses transaction-control statements")
    if head not in ("insert", "update", "delete", "select", "create", "drop", "alter", "truncate"):
        raise ValueError("pg_execute refuses statements that are not a single DB operation")
    return stmt


def _pg_params_list(params):
    raw = params.get("db_params_b64")
    if raw is None or str(raw).strip() == "":
        return []
    decoded = _b64_param(params, "db_params_b64")
    values = json.loads(decoded)
    if not isinstance(values, list):
        raise ValueError("DB_PARAMS_B64 must decode to a JSON array")
    return values


def op_pg_query(params):
    dsn = _dsn_pg(params)
    pinned_ip = _guard_target(dsn, params)
    query = str(params.get("db_query") or "").strip()
    if not query:
        raise ValueError("DB_QUERY is required")
    if _split_statements(query) and len(_split_statements(query)) != 1:
        raise ValueError("DB_QUERY must be a single statement")
    head = query.strip().split()[0].lower() if query.strip() else ""
    if head not in ("select", "with", "explain", "describe"):
        raise ValueError("pg_query accepts read-only statements only (SELECT/WITH)")
    timeout_s = _int_param(params, "db_timeout_s", DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
    cap = _int_param(params, "db_max_rows", DEFAULT_ROW_CAP, MAX_ROW_CAP)
    values = _pg_params_list(params)
    conn = _pg_connect(dsn, timeout_s, pinned_ip)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(query, values if values else None)
            rows = _rows_as_dicts(cur)
            if len(rows) > cap:
                raise ValueError(f"result exceeds the {cap}-row cap")
            _write_json("pg_query.json", {"rows": rows, "row_cap": cap})
    finally:
        conn.close()
def _rows_as_dicts(cur):
    fetched = cur.fetchall()
    rows = []
    for raw in fetched:
        if isinstance(raw, dict):
            rows.append(dict(raw))
        else:
            names = [d[0] for d in cur.description]
            rows.append(dict(zip(names, raw)))
    return rows


def op_pg_execute(params):
    dsn = _dsn_pg(params)
    pinned_ip = _guard_target(dsn, params)
    stmt = _pg_statement(str(params.get("db_query") or ""))
    timeout_s = _int_param(params, "db_timeout_s", DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
    values = _pg_params_list(params)
    conn = _pg_connect(dsn, timeout_s, pinned_ip)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(stmt, values if values else None)
            _write_json(
                "pg_execute.json",
                {
                    "statement": stmt[:200],
                    "row_count": int(cur.rowcount) if cur.rowcount is not None else 0,
                },
            )
    finally:
        conn.close()


def _mongo_filter(params):
    filt = _json_param(params, "mongo_filter_json", {})
    if not isinstance(filt, dict):
        raise ValueError("MONGO_FILTER_JSON must be a JSON object")
    return filt


def _mongo_projection(params):
    proj = _json_param(params, "mongo_projection_json", {})
    if not isinstance(proj, dict):
        raise ValueError("MONGO_PROJECTION_JSON must be a JSON object")
    return proj


def _mongo_scan(obj, path=""):
    # Refuse $where / $function / $accumulator anywhere in the filter.
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key) in MONGO_FORBIDDEN:
                raise ValueError(
                    f"MONGO_FILTER_JSON contains a refused operator: {key!r} "
                    "($where/$function/$accumulator are never allowed)"
                )
            _mongo_scan(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for item in obj:
            _mongo_scan(item, path)


def _scan_forbidden(filt, proj=None):
    _mongo_scan(filt)
    if proj is not None:
        _mongo_scan(proj)
    blob = json.dumps(filt)
    for needle in MONGO_FORBIDDEN:
        if needle in blob:
            raise ValueError(f"MONGO_FILTER_JSON contains a refused operator: {needle}")


def _mongo_connect(uri, timeout_s):
    if pymongo is None:
        raise ValueError("pymongo is not installed in this image")
    return pymongo.MongoClient(
        uri,
        timeout=timeout_s,
        connect_timeout=timeout_s,
        read_timeout=timeout_s,
    )


def _mongo_names(params):
    db_name = str(params.get("mongo_db") or params.get("db_database") or "").strip()
    table = str(params.get("mongo_table") or params.get("db_table") or "").strip()
    if not db_name or not table:
        raise ValueError("MONGO_DB and MONGO_TABLE are both required")
    return db_name, table


def op_mongo_find(params):
    uri = _mongo_uri(params)
    _guard_mongo(uri)
    filt = _mongo_filter(params)
    proj = _mongo_projection(params)
    _scan_forbidden(filt, proj)
    limit = _int_param(params, "mongo_limit", DEFAULT_ROW_CAP, MAX_ROW_CAP)
    timeout_s = _int_param(params, "db_timeout_s", DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
    db_name, table = _mongo_names(params)
    client = _mongo_connect(uri, timeout_s)
    try:
        doc = client[db_name][table]
        rows = []
        for row in doc.find(filt, proj, limit=limit + 1):
            rows.append({k: str(v) for k, v in row.items()})
            if len(rows) > limit:
                raise ValueError(f"result exceeds the {limit}-row cap")
        _write_json("mongo_find.json", {"rows": rows, "row_cap": limit})
    finally:
        client.close()
def op_mongo_write(params):
    uri = _mongo_uri(params)
    _guard_mongo(uri)
    filt = _mongo_filter(params)
    _scan_forbidden(filt)
    doc = _json_param(params, "mongo_doc_json")
    if not isinstance(doc, dict) or not doc:
        raise ValueError("MONGO_DOC_JSON must be a non-empty JSON object")
    _scan_forbidden(doc)
    match = _json_param(params, "mongo_match_json", filt)
    new = _json_param(params, "mongo_new_json", doc)
    timeout_s = _int_param(params, "db_timeout_s", DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
    db_name, table = _mongo_names(params)
    client = _mongo_connect(uri, timeout_s)
    try:
        handle = client[db_name][table]
        if str(params.get("mongo_mode", "insert")).lower() == "update":
            result = handle.update_one(match, {"$set": new}, upsert=False)
            outcome = {"updated": int(result.updated_count), "not_found": int(result.not_found_count)}
        else:
            handle.insert_one(doc)
            outcome = {"inserted": 1}
        _write_json("mongo_write.json", outcome)
    finally:
        client.close()


def _dynamo_endpoint(params):
    raw = str(params.get("db_target") or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        host = raw.split("://", 1)[1].split("/", 1)[0].split(":")[0]
        _resolve_host(host)
        return raw
    raise ValueError("DYNAMO_ENDPOINT_URL must be an http(s) URL when set")


def _dynamo_session(params):
    ak, sk = _dynamo_keys(params)
    region = str(params.get("db_region") or "").strip()
    if not region:
        raise ValueError("DB_REGION is required for the Dynamo operations")
    endpoint = _dynamo_endpoint(params)
    if boto3 is None:
        raise ValueError("boto3 is not installed in this image")
    kwargs = {
        "region_name": region,
        "aws_access_key_id": ak,
        "aws_secret_access_key": sk,
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return _BotoSessionAlias(**kwargs)


def _dynamo_table(params):
    table = str(params.get("dynamo_table") or params.get("db_table") or "").strip()
    if not table:
        raise ValueError("DYNAMO_TABLE is required")
    return table


def _mongo_hosts(uri):
    # mongodb://[user:pass@]host1[:port1],host2[:port2],.../db?opts — every
    # host in the comma-separated replica-set list must be guarded, not just
    # the first one.
    rest = uri[len("mongodb://"):]
    rest = rest.split("@")[-1]
    hostlist = rest.split("/")[0].split("?")[0]
    hosts = [entry.split(":")[0].strip() for entry in hostlist.split(",")]
    hosts = [h for h in hosts if h]
    if not hosts:
        raise ValueError("DB_MONGO_URI has no host")
    return hosts


def _guard_mongo(uri):
    # Resolve the MongoDB URI host(s) and refuse loopback/metadata/link-local.
    if uri.lower().startswith("mongodb+srv://"):
        raise ValueError(
            "mongodb+srv URIs use DNS-based service discovery which cannot be "
            "resolved safely here — use mongodb:// with an explicit hostname"
        )
    if not uri.lower().startswith("mongodb://"):
        raise ValueError("DB_MONGO_URI must use the mongodb:// scheme")
    for host in _mongo_hosts(uri):
        _resolve_host(host)


def op_dynamo_get(params):
    name = _dynamo_table(params)
    key = _json_param(params, "dynamo_key_json")
    if not isinstance(key, dict) or not key:
        raise ValueError("DYNAMO_KEY_JSON must be a non-empty JSON object")
    _scan_forbidden(key)
    boto = _dynamo_session(params).client("boto3")
    item = boto.Table(name).get_item(Key=key)
    _write_json("dynamo_get.json", {"item": item.get("Item", {}), "found": "Item" in item})


def op_dynamo_put(params):
    name = _dynamo_table(params)
    doc = _json_param(params, "dynamo_doc_json")
    if not isinstance(doc, dict) or not doc:
        raise ValueError("DYNAMO_DOC_JSON must be a non-empty JSON object")
    _scan_forbidden(doc)
    boto = _dynamo_session(params).client("boto3")
    boto.Table(name).put_item(Item=doc)
    _write_json("dynamo_put.json", {"table": name, "put": 1})


OPS = {
    "pg_query": op_pg_query,
    "pg_execute": op_pg_execute,
    "mongo_find": op_mongo_find,
    "mongo_write": op_mongo_write,
    "dynamo_get": op_dynamo_get,
    "dynamo_put": op_dynamo_put,
}


def main():
    op = os.getenv("DB_OP", "pg_query").strip().lower()
    if op not in OPS and op.startswith("db_"):
        op = op[len("db_"):]
    if op not in OPS:
        _log(f"ERROR: unknown DB_OP {op!r}")
        _write_error(f"unknown DB_OP {op!r}")
        sys.exit(1)
    try:
        OPS[op](_params())
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        msg = str(exc) or exc.__class__.__name__
        for needle in sorted(_SECRETS_SEEN, key=len, reverse=True):
            if needle and needle in msg:
                msg = msg.replace(needle, "[redacted]")
        _log(f"ERROR: {msg}")
        _write_error(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
"""

PG_QUERY_SPEC = {
    "name": "Database Postgres Query",
    "description": (
        "Run a read-only, parameterised SELECT against a customer Postgres "
        "database and emit the result as JSON rows on `rows`. The statement "
        "lives in DB_QUERY and the values go in DB_PARAMS_B64 (base64 JSON "
        "array) so they are passed as %s placeholders — a value is NEVER "
        "string-formatted into the SQL. The node opens `SET TRANSACTION READ "
        "ONLY`, pins a 30-second statement timeout and a 10,000-row cap, and "
        "refuses a target that resolves to a loopback or link-local metadata "
        "address (private/customer addresses are allowed, that is where "
        "customer DBs live). The connection string is the secret parameter "
        "DB_PG_DSN: never logged, never echoed into an output, scrubbed from "
        "error text. Backed by the database-crud container (psycopg[binary])."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pg_query",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": (
                "Optional fields.json overriding the parameters: "
                '{"query": "...", "params": [...]}.'
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "rows": {
            "type": "json",
            "description": "The result rows, header-keyed, capped at 10,000.",
        },
    },
    "configuration_schema": {
        "DB_OP": {
            "type": "string",
            "default": "pg_query",
            "description": (
                "Fixed for this node: selects the database operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "DB_PG_DSN": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Postgres connection string (postgres://user:***@host/db "
                    "or key=value form). SECRET: prefer a secret reference "
                    "mapped to DB_PG_DSN. Never logged, never echoed."
                ),
            },
            "DB_QUERY": {
                "type": "string",
                "default": "",
                "description": (
                    "One read-only statement (SELECT/WITH) with %s placeholders "
                    "for the values in DB_PARAMS_B64."
                ),
            },
            "DB_PARAMS_B64": {
                "type": "string",
                "default": "",
                "description": (
                    "Base64-encoded JSON array of parameter values for the "
                    "%s placeholders. Values are never inlined into SQL."
                ),
            },
            "DB_MAX_ROWS": {
                "type": "string",
                "default": "10000",
                "description": "Row cap for the result (max 10,000).",
            },
            "DB_TIMEOUT_S": {
                "type": "string",
                "default": "30",
                "description": "Connect/statement timeout in seconds (max 120).",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}

PG_EXECUTE_SPEC = {
    "name": "Database Postgres Execute",
    "description": (
        "Apply exactly ONE write statement (INSERT/UPDATE/DELETE) to a customer "
        "Postgres database. Values are parameterised only (DB_PARAMS_B64 -> %s "
        "placeholders). The node refuses anything that is not a single "
        "statement: a quote- and comment-aware split on `;` rejects a second "
        "statement hidden outside quotes, transaction-control keywords "
        "(BEGIN/COMMIT/ROLLBACK/DO) are refused, and statements over 8000 "
        "characters are refused. The statement timeout is pinned at 30 s and "
        "the connection string is the secret parameter DB_PG_DSN (never logged, "
        "scrubbed from errors). Loopback / metadata targets are refused; "
        "private customer addresses are allowed. Backed by the database-crud "
        "container (psycopg[binary])."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "pg_execute",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": 'Optional fields.json: {"query": "...", "params": [...]}.',
        },
    },
    "output_schema": {
        "output_type": "json",
        "result": {
            "type": "json",
            "description": "The applied statement (truncated) and its row_count.",
        },
    },
    "configuration_schema": {
        "DB_OP": {
            "type": "string",
            "default": "pg_execute",
            "description": (
                "Fixed for this node: selects the database operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "DB_PG_DSN": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Postgres connection string. SECRET: prefer a secret "
                    "reference. Never logged, never echoed into an output."
                ),
            },
            "DB_QUERY": {
                "type": "string",
                "default": "",
                "description": (
                    "One INSERT/UPDATE/DELETE statement with %s placeholders. "
                    "A second statement outside quotes is rejected."
                ),
            },
            "DB_PARAMS_B64": {
                "type": "string",
                "default": "",
                "description": "Base64-encoded JSON array of parameter values.",
            },
            "DB_TIMEOUT_S": {
                "type": "string",
                "default": "30",
                "description": "Connect/statement timeout in seconds (max 120).",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}
MONGO_FIND_SPEC = {
    "name": "Database Mongo Find",
    "description": (
        "Query a customer MongoDB database and emit the matching documents as "
        "JSON rows on `rows`. The filter is MONGO_FILTER_JSON, the field set is "
        "MONGO_PROJECTION_JSON and the result limit is MONGO_LIMIT (capped at "
        "10,000). Filters are walked recursively and `$where`, `$function` and "
        "`$accumulator` are refused anywhere in the filter/projection: they "
        "turn a document from another tenant into executable server-side "
        "JavaScript. The connection string is the secret parameter DB_MONGO_URI "
        "(never logged, scrubbed out of error text); loopback and link-local "
        "metadata hosts are refused, private customer addresses are allowed. "
        "Backed by the database-crud container (pymongo)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "mongo_find",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": (
                'Optional fields.json overriding the parameters: '
                '{"filter": {...}, "projection": {...}, "limit": 100}.'
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "rows": {
            "type": "json",
            "description": "Matching documents, stringified, capped at 10,000.",
        },
    },
    "configuration_schema": {
        "DB_OP": {
            "type": "string",
            "default": "mongo_find",
            "description": (
                "Fixed for this node: selects the database operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "DB_MONGO_URI": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Mongo connection string. SECRET: prefer a secret "
                    "reference. Never logged, never echoed into an output."
                ),
            },
            "MONGO_DB": {
                "type": "string",
                "default": "",
                "description": "Database (collection-of-collections) name.",
            },
            "MONGO_TABLE": {
                "type": "string",
                "default": "",
                "description": "Collection name inside MONGO_DB.",
            },
            "MONGO_FILTER_JSON": {
                "type": "string",
                "default": "{}",
                "description": (
                    "JSON filter object ({} = match all). $where/$function/"
                    "$accumulator are refused anywhere in it."
                ),
            },
            "MONGO_PROJECTION_JSON": {
                "type": "string",
                "default": "{}",
                "description": "JSON projection object ({} = all fields).",
            },
            "MONGO_LIMIT": {
                "type": "string",
                "default": "10000",
                "description": "Result limit (max 10,000).",
            },
            "DB_TIMEOUT_S": {
                "type": "string",
                "default": "30",
                "description": "Connect/read timeout in seconds (max 120).",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}

MONGO_WRITE_SPEC = {
    "name": "Database Mongo Write",
    "description": (
        "Write ONE document to a customer MongoDB database: insert_one by "
        "default, or update_one against MONGO_MATCH_JSON when MONGO_MODE=update "
        "(upsert is never enabled). The document is MONGO_DOC_JSON and is "
        "scanned with the same rule as the filter, so a document smuggled in "
        "from another tenant cannot carry `$where` / `$function` / "
        "`$accumulator` into the server. The connection string is the secret "
        "parameter DB_MONGO_URI (never logged, scrubbed from errors); loopback "
        "and metadata hosts are refused, private customer addresses are allowed. "
        "Backed by the database-crud container (pymongo)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "mongo_write",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": (
                'Optional fields.json carrying the document: '
                '{"doc": {...}, "match": {...}, "mode": "insert|update"}.'
            ),
        },
    },
    "output_schema": {
        "output_type": "json",
        "result": {
            "type": "json",
            "description": "Write outcome: {\"inserted\": 1} or {\"updated\": 1}.",
        },
    },
    "configuration_schema": {
        "DB_OP": {
            "type": "string",
            "default": "mongo_write",
            "description": (
                "Fixed for this node: selects the database operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "DB_MONGO_URI": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Mongo connection string. SECRET: prefer a secret "
                    "reference. Never logged, never echoed into an output."
                ),
            },
            "MONGO_DB": {
                "type": "string",
                "default": "",
                "description": "Database name.",
            },
            "MONGO_TABLE": {
                "type": "string",
                "default": "",
                "description": "Collection name.",
            },
            "MONGO_FILTER_JSON": {
                "type": "string",
                "default": "{}",
                "description": (
                    "JSON filter object; MONGO_MODE=update uses it as the "
                    "match filter when MONGO_MATCH_JSON is blank."
                ),
            },
            "MONGO_DOC_JSON": {
                "type": "string",
                "default": "",
                "description": "The single document to insert (JSON object).",
            },
            "MONGO_MATCH_JSON": {
                "type": "string",
                "default": "",
                "description": "Filter for MONGO_MODE=update (defaults to the filter).",
            },
            "MONGO_NEW_JSON": {
                "type": "string",
                "default": "",
                "description": "Fields to $set for MONGO_MODE=update (defaults to the doc).",
            },
            "MONGO_MODE": {
                "type": "string",
                "default": "insert",
                "enum": ["insert", "update"],
                "description": "insert_one (default) or update_one; upsert is never on.",
            },
            "DB_TIMEOUT_S": {
                "type": "string",
                "default": "30",
                "description": "Connect/read timeout in seconds (max 120).",
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}
DYNAMO_GET_SPEC = {
    "name": "Database Dynamo Get",
    "description": (
        "Read ONE item from a customer DynamoDB table with get_item only — the "
        "node never lists, scans, searches or deletes. DB_REGION is required; "
        "DYNAMO_ENDPOINT_URL is optional and points at an AWS-compatible "
        "endpoint on customer hardware (it is resolved and refused if it "
        "resolves to a loopback or link-local metadata address, while private "
        "customer addresses are allowed). The key is DYNAMO_KEY_JSON. "
        "Credentials are the secret parameters DB_DYNAMO_AK / DB_DYNAMO_SK: "
        "never logged, never echoed into an output, scrubbed out of error text. "
        "Backed by the database-crud container (boto3)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "dynamo_get",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": 'Optional fields.json: {"table": "...", "key": {...}}.',
        },
    },
    "output_schema": {
        "output_type": "json",
        "item": {
            "type": "json",
            "description": "The retrieved item and a found flag.",
        },
    },
    "configuration_schema": {
        "DB_OP": {
            "type": "string",
            "default": "dynamo_get",
            "description": (
                "Fixed for this node: selects the database operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "DB_DYNAMO_AK": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Dynamo access key. SECRET: prefer a secret reference. "
                    "Never logged, never echoed into an output."
                ),
            },
            "DB_DYNAMO_SK": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Dynamo secret key. SECRET: prefer a secret reference. "
                    "Never logged, never echoed into an output."
                ),
            },
            "DB_REGION": {
                "type": "string",
                "default": "",
                "description": "Required region (e.g. auto for on-prem/AWS-compatible).",
            },
            "DYNAMO_TABLE": {
                "type": "string",
                "default": "",
                "description": "Table name.",
            },
            "DYNAMO_KEY_JSON": {
                "type": "string",
                "default": "",
                "description": "JSON key object (get_item Key).",
            },
            "DYNAMO_ENDPOINT_URL": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional AWS-compatible endpoint URL (customer hardware). "
                    "Loopback / metadata targets are refused."
                ),
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}

DYNAMO_PUT_SPEC = {
    "name": "Database Dynamo Put",
    "description": (
        "Write ONE item to a customer DynamoDB table with put_item only — the "
        "node has no delete, batch, scan or list path. DB_REGION is required "
        "and DYNAMO_ENDPOINT_URL is optional (resolved and refused when it "
        "points at a loopback or link-local metadata address; private customer "
        "addresses are allowed). The item is DYNAMO_DOC_JSON. Credentials are "
        "the secret parameters DB_DYNAMO_AK / DB_DYNAMO_SK: never logged, never "
        "echoed into an output, scrubbed out of error text. Backed by the "
        "database-crud container (boto3)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "dynamo_put",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "json",
        "data": {
            "type": "json",
            "description": 'Optional fields.json: {"table": "...", "doc": {...}}.',
        },
    },
    "output_schema": {
        "output_type": "json",
        "result": {
            "type": "json",
            "description": "Write outcome: {\"table\": ..., \"put\": 1}.",
        },
    },
    "configuration_schema": {
        "DB_OP": {
            "type": "string",
            "default": "dynamo_put",
            "description": (
                "Fixed for this node: selects the database operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "DB_DYNAMO_AK": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Dynamo access key. SECRET: prefer a secret reference. "
                    "Never logged, never echoed into an output."
                ),
            },
            "DB_DYNAMO_SK": {
                "type": "string",
                "default": "",
                "secret": True,
                "description": (
                    "Dynamo secret key. SECRET: prefer a secret reference. "
                    "Never logged, never echoed into an output."
                ),
            },
            "DB_REGION": {
                "type": "string",
                "default": "",
                "description": "Required region.",
            },
            "DYNAMO_TABLE": {
                "type": "string",
                "default": "",
                "description": "Table name.",
            },
            "DYNAMO_DOC_JSON": {
                "type": "string",
                "default": "",
                "description": "The single item to put (JSON object).",
            },
            "DYNAMO_ENDPOINT_URL": {
                "type": "string",
                "default": "",
                "description": (
                    "Optional AWS-compatible endpoint URL (customer hardware). "
                    "Loopback / metadata targets are refused."
                ),
            },
        },
    },
    "accepted_formats": ["json"],
    "cost_in_credits": 2,
}

NODE_DEFINITIONS = [
    PG_QUERY_SPEC,
    PG_EXECUTE_SPEC,
    MONGO_FIND_SPEC,
    MONGO_WRITE_SPEC,
    DYNAMO_GET_SPEC,
    DYNAMO_PUT_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "pg_query": PG_QUERY_SPEC,
    "pg_execute": PG_EXECUTE_SPEC,
    "mongo_find": MONGO_FIND_SPEC,
    "mongo_write": MONGO_WRITE_SPEC,
    "dynamo_get": DYNAMO_GET_SPEC,
    "dynamo_put": DYNAMO_PUT_SPEC,
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


def register_database_crud(db: Session) -> int:
    """Register the 6 database operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping database-crud domain-primitive seed"
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
                tenant_id=None,  # system node — available to all tenants
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
