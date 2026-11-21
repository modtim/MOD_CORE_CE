"""node_definitions_domains/geo — Geographic / mapping NodeDefinitions (Phase 6 domain primitive).

Registers six system node definitions backed by the geo-utils container
(`CORE/DockerContainers/geo-utils`): haversine distance, geohash encode /
decode, bounding-box / centroid extraction, point-in-polygon, GeoJSON
validation, and decimal / DMS coordinate conversion.

Posture notes
-------------
- Specs are plain dicts so the module is importable + testable without a DB;
  `register_geo()` is the only DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog speaks:
  the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility,
  AND named ports (`data`) so the UI labels the ports correctly.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it (system-node names are globally
  unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `seed_converters.py` does — existing rows are never
  deleted.

Geocoding / routing require an external service and are out of scope for this
module. Future: tenant-configured provider via egress policy.

Input cap: 10 MB (fields.json). Untrusted input: no eval/exec, no shell=True.
Clear error JSON on failure with non-zero exit.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_REPO_TAG = "geo-utils:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/geo-utils/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via quoted heredoc so the
# container is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# geo-utils entrypoint — one operation per run, selected by GEO_OP.
#
#   export GEO_OP=distance|geohash_encode|geohash_decode|bbox|point_in_polygon|geojson_validate|coord_format
#   python3 /app/geo_process.py
#
# Fields arrive as /data/input/fields.json; results go to /data/output.
# No network access, no eval/exec, no shell. 10 MB input cap.

import json
import math
import os
import re
import sys
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_SIZE = 10 * 1024 * 1024  # 10 MB input cap

# Vet step 5 (fail-closed): every failure path ends in fail(), which prints
# exactly one JSON line on stdout and exits non-zero.
CODE_NO_INPUT = "NO_INPUT"
CODE_BAD_PARAM = "BAD_PARAM"
CODE_PROCESSING = "PROCESSING_FAILURE"
CODE_OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
CODE_INTERNAL = "INTERNAL"


class NodeError(ValueError):
    # Bad-input failure carrying its contract code. Subclasses ValueError so
    # the per-op unit tests (pytest.raises(ValueError, match=...)) keep
    # passing while main() maps each raise to its code.
    def __init__(self, msg, code=CODE_BAD_PARAM):
        super().__init__(msg)
        self.code = code


def _write_text(name, value):
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_text(value, encoding="utf-8")
    except OSError as exc:
        raise NodeError(f"could not write {name}: {exc}", CODE_OUTPUT_NOT_WRITTEN) from exc


def _write_error(msg):
    # Best-effort sidecar; the stdout JSON from fail() is the contract.
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")
    except OSError:
        pass


def fail(msg, code=CODE_INTERNAL):
    # Vet step 5: one JSON line on stdout, then exit non-zero.
    sys.stderr.write(f"ERROR: {msg}\n")
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    _write_error(msg)
    sys.exit(1)


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        raise NodeError("fields.json required on the input port", CODE_NO_INPUT)
    raw = p.read_bytes()
    if len(raw) > MAX_SIZE:
        raise NodeError(f"fields.json exceeds the {MAX_SIZE}-byte input cap", CODE_NO_INPUT)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise NodeError("fields.json must be a JSON object", CODE_BAD_PARAM)
    return _normalize_fields(data)


def _normalize_fields(data):
    out = {}
    for key, value in data.items():
        low = str(key).strip().lower()
        out[low] = value
    return out


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371.0  # km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _encode_geohash(lat, lon, precision=6):
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = [16, 8, 4, 2, 1]
    bit = 0
    ch = 0
    geohash = []
    is_lon = True
    while len(geohash) < precision:
        if is_lon:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                ch |= bits[bit]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                ch |= bits[bit]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        is_lon = not is_lon
        bit += 1
        if bit == 5:
            geohash.append(CHAR_MAP[ch])
            ch = 0
            bit = 0
    return "".join(geohash)


CHAR_MAP = list("0123456789bcdefghjkmnpqrstuvwxyz")


def _decode_geohash(geohash):
    lat = [-90.0, 90.0]
    lon = [-180.0, 180.0]
    is_lon = True
    for ch in geohash:
        idx = CHAR_MAP.index(ch)
        for bit in [16, 8, 4, 2, 1]:
            if idx & bit:
                if is_lon:
                    lon[0] = (lon[0] + lon[1]) / 2
                else:
                    lat[0] = (lat[0] + lat[1]) / 2
            else:
                if is_lon:
                    lon[1] = (lon[0] + lon[1]) / 2
                else:
                    lat[1] = (lat[0] + lat[1]) / 2
            is_lon = not is_lon
    return (lat, lon)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def op_distance(fields):
    lat1 = float(fields["lat1"])
    lon1 = float(fields["lon1"])
    lat2 = float(fields["lat2"])
    lon2 = float(fields["lon2"])
    unit = str(fields.get("unit", "km")).strip().lower()
    dist_km = _haversine(lat1, lon1, lat2, lon2)
    if unit == "mi":
        dist = dist_km * 0.621371
    elif unit == "m":
        dist = dist_km * 1000
    else:
        dist = dist_km
    _write_text("distance_km.txt", str(round(dist_km, 6)))
    _write_text("bearing.txt", str(round(_bearing(lat1, lon1, lat2, lon2), 4)))
    _write_text("distance.txt", str(round(dist, 6)))


def op_geohash_encode(fields):
    lat = float(fields["lat"])
    lon = float(fields["lon"])
    precision = int(fields.get("precision", 6))
    if precision < 1 or precision > 12:
        raise NodeError(f"geohash precision must be 1..12, got {precision}", CODE_BAD_PARAM)
    _write_text("geohash.txt", _encode_geohash(lat, lon, precision))


def op_geohash_decode(fields):
    geohash = str(fields["geohash"]).strip()
    precision = int(fields.get("precision", 6))
    if precision < 1 or precision > 12:
        raise NodeError(f"geohash precision must be 1..12, got {precision}", CODE_BAD_PARAM)
    lat_range, lon_range = _decode_geohash(geohash[:precision])
    lat = (lat_range[0] + lat_range[1]) / 2
    lon = (lon_range[0] + lon_range[1]) / 2
    _write_text("lat.txt", str(round(lat, precision)))
    _write_text("lon.txt", str(round(lon, precision)))


def _extract_coords(geojson):
    # Recursively collect all [lon, lat] coordinate pairs from a GeoJSON object.
    coords = []
    def _collect(obj):
        if isinstance(obj, list):
            if len(obj) >= 2 and isinstance(obj[0], (int, float)) and isinstance(obj[1], (int, float)):
                coords.append(obj)
                return
            for item in obj:
                _collect(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    _collect(v)
    _collect(geojson)
    return coords


def op_bbox(fields):
    geojson = fields["geojson"]
    if isinstance(geojson, str):
        geojson = json.loads(geojson)
    coords = _extract_coords(geojson)
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    bbox = [min(lons), min(lats), max(lons), max(lats)]
    centroid = [(min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2]
    _write_text("bbox.txt", json.dumps(bbox))
    _write_text("centroid.txt", json.dumps(centroid))


def op_point_in_polygon(fields):
    geojson = fields["geojson"]
    if isinstance(geojson, str):
        geojson = json.loads(geojson)
    points = fields["points"]
    if isinstance(points, str):
        points = json.loads(points)
    elif isinstance(points, list):
        pass
    else:
        raise NodeError("points must be a JSON array of [lat,lon] pairs", CODE_BAD_PARAM)
    for p in points:
        if not isinstance(p, list) or len(p) != 2:
            raise NodeError("each point must be [lat, lon]", CODE_BAD_PARAM)
    result = _point_in_polygon_check(geojson, points)
    _write_text("point_in_polygon.txt", json.dumps(result))


def op_geojson_validate(fields):
    geojson = fields["geojson"]
    if isinstance(geojson, str):
        geojson = json.loads(geojson)
    errors = []
    _validate_geojson_recursive(geojson, errors)
    status = "valid" if not errors else "invalid"
    _write_text("validation.txt", json.dumps({"status": status, "errors": errors}))


def _validate_geojson_recursive(obj, errors, depth=0):
    if depth > 10:
        errors.append("excessive nesting")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "type" and isinstance(v, str) and v in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection", "Feature", "FeatureCollection"):
                continue
            if k == "properties" and isinstance(v, dict):
                continue
            if k in ("coordinates",):
                if not isinstance(v, list):
                    errors.append(f"{k} must be an array, got {type(v).__name__}")
                else:
                    _validate_coords(v, errors, depth)
            else:
                _validate_geojson_recursive(v, errors, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _validate_geojson_recursive(item, errors, depth + 1)


def _validate_coords(coords, errors, depth=0):
    if depth > 10:
        errors.append("excessive nesting in coordinates")
        return
    if isinstance(coords, list):
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            for i, c in enumerate(coords):
                if i == 1 and c < -90 or c > 90:
                    errors.append(f"latitude {c} out of range [-90, 90]")
                elif i == 0 and c < -180 or c > 180:
                    errors.append(f"longitude {c} out of range [-180, 180]")
        else:
            for item in coords:
                _validate_coords(item, errors, depth + 1)


def _point_in_polygon_check(geojson, points):
    if isinstance(geojson, str):
        geojson = json.loads(geojson)
    rings = _extract_rings(geojson)
    results = []
    for p in points:
        lat, lon = float(p[0]), float(p[1])
        inside = False
        for ring in rings:
            n = len(ring)
            j = n - 1
            for i in range(n):
                yi, xi = ring[i][1], ring[i][0]
                yj, xj = ring[j][1], ring[j][0]
                if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
                    inside = not inside
                j = i
            if inside:
                break
        results.append(inside)
    return results


def _extract_rings(geojson):
    geom = _find_geometry(geojson)
    if geom is None:
        return []
    ctype = geom.get("type", "")
    coords = geom.get("coordinates", [])
    if ctype == "Polygon":
        return coords if coords else [coords]
    elif ctype == "MultiPolygon":
        rings = []
        for poly in coords:
            rings.extend(poly if poly else [poly])
        return rings
    return []


def _find_geometry(obj):
    if isinstance(obj, dict) and obj.get("type") in ("Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"):
        return obj
    if isinstance(obj, dict) and obj.get("type") == "Feature":
        return obj.get("geometry")
    if isinstance(obj, dict) and obj.get("type") == "FeatureCollection":
        for f in obj.get("features", []) or []:
            g = _find_geometry(f)
            if g:
                return g
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _find_geometry(v)
                if r:
                    return r
    return None


def op_coord_format(fields):
    value = fields["value"]
    mode = str(fields.get("mode", "decimal")).strip().lower()
    if mode in ("dms", "to_dms"):
        lat = float(value) if isinstance(value, str) else float(value.get("lat", 0))
        lon = float(value.get("lon", 0)) if isinstance(value, dict) else 0.0
        result = _to_dms(lat, lon)
    else:
        if not isinstance(value, dict):
            raise NodeError("to_decimal mode requires lat/lon as a JSON object {lat: ..., lon: ...}", CODE_BAD_PARAM)
        lat = _parse_dms_part(value["lat"], value.get("lat_dir", "N"))
        lon = _parse_dms_part(value["lon"], value.get("lon_dir", "E"))
        result = json.dumps({"lat": round(lat, 8), "lon": round(lon, 8)})
    _write_text("converted_value.txt", result)


def _to_dms(lat, lon):
    def fmt(v, pos, neg):
        a = abs(v)
        d = int(a)
        m_full = (a - d) * 60
        m = int(m_full)
        s = round((m_full - m) * 60, 2)
        return f"{d}°{m}'{s}\"{pos}" if v >= 0 else f"{d}°{m}'{s}\"{neg}"
    return json.dumps({"lat": fmt(lat, "N", "S"), "lon": fmt(lon, "E", "W")})


def _parse_dms_part(text, direction):
    text = str(text)
    m = re.match(r"(\d+)\s*(?:°)?\s*(\d+)\s*['\'°]?\s*(\d+(?:\.\d+)?)\s*(?:\"|\")?", text)
    if m:
        d, m_val, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
    else:
        parts = text.replace(",", " ").replace(";", " ").split()
        d, m_val, s = float(parts[0]), float(parts[1]) if len(parts) > 1 else 0.0, float(parts[2]) if len(parts) > 2 else 0.0
    val = d + m_val / 60.0 + s / 3600.0
    if direction.upper() in ("S", "W"):
        val = -val
    return val


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

OPS = {
    "distance": op_distance,
    "geohash_encode": op_geohash_encode,
    "geohash_decode": op_geohash_decode,
    "bbox": op_bbox,
    "point_in_polygon": op_point_in_polygon,
    "geojson_validate": op_geojson_validate,
    "coord_format": op_coord_format,
}


def main():
    try:
        op = os.getenv("GEO_OP", "").strip().lower()
        # The spec default is the node's own op key ("geo_distance"); the
        # container speaks the short name. Accept either spelling.
        if op not in OPS and op.startswith("geo_"):
            op = op[len("geo_"):]
        if op not in OPS:
            fail(f"unknown GEO_OP {op!r}", CODE_BAD_PARAM)
        fields = _read_fields()
        OPS[op](fields)
    except SystemExit:
        raise
    except NodeError as exc:
        fail(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        fail(f"{type(exc).__name__}: {exc}", CODE_INTERNAL)


if __name__ == "__main__":
    main()
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

GEO_DISTANCE_SPEC = {
    "name": "Geo Distance",
    "description": (
        "Haversine great-circle distance between two geographic coordinates "
        "(lat/lon pairs) with bearing. Accepts `lat1`, `lon1`, `lat2`, `lon2` "
        "and optional `unit` (km / mi / m, default km). Outputs "
        "`distance_km.txt` (always), `bearing.txt` (initial bearing), and "
        "`distance.txt` (converted to the requested unit). Stdlib math, no "
        "network. Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_distance",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Point coordinates (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "distance_km": {"type": "text", "description": "Distance in kilometers."},
        "bearing": {"type": "text", "description": "Initial bearing in degrees."},
        "distance": {"type": "text", "description": "Distance in requested unit."},
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_distance",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "unit": {
                "type": "string",
                "default": "km",
                "enum": ["km", "mi", "m"],
                "description": (
                    "Distance unit. km (kilometers, default), mi (miles), "
                    "m (metres)."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

GEO_GEOHASH_ENCODE_SPEC = {
    "name": "Geohash Encode",
    "description": (
        "Encode a lat/lon coordinate pair into a geohash string. Accepts "
        "`lat` (-90..90), `lon` (-180..180), and optional `precision` "
        "(1..12, default 6). Outputs `geohash.txt` — a case-sensitive base32 "
        "string that serves as a hierarchical spatial index. Pure Python "
        "(std lib math), no network. Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_geohash_encode",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Coordinates (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "geohash": {"type": "text", "description": "Encoded geohash string."},
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_geohash_encode",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "precision": {
                "type": "integer",
                "default": 6,
                "enum": list(range(1, 13)),
                "description": (
                    "Geohash string length 1..12. Higher precision = "
                    "smaller cells but longer strings."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

GEO_GEOHASH_DECODE_SPEC = {
    "name": "Geohash Decode",
    "description": (
        "Decode a geohash string back into lat/lon centre coordinates. "
        "Accepts `geohash` and optional `precision` (1..12). Outputs "
        "`lat.txt` and `lon.txt` with the decoded centre position. "
        "Pure Python (std lib), no network. Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_geohash_decode",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Geohash (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "lat": {"type": "text", "description": "Decoded latitude."},
        "lon": {"type": "text", "description": "Decoded longitude."},
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_geohash_decode",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "precision": {
                "type": "integer",
                "default": 6,
                "enum": list(range(1, 13)),
                "description": (
                    "Geohash string length 1..12. Should match or be "
                    "less than the encoding precision."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

GEO_BBOX_SPEC = {
    "name": "Geo Bbox / Centroid",
    "description": (
        "Extract a bounding box and centroid from a GeoJSON object "
        "(Point, LineString, Polygon, MultiPolygon, Feature, or "
        "FeatureCollection). Accepts `geojson` as a JSON string or "
        "dict. Outputs `bbox.txt` (array of [min_lon, min_lat, max_lon, "
        "max_lat]) and `centroid.txt` (array of [centroid_lon, centroid_lat]). "
        "Pure Python (std lib), no network. Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_bbox",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "GeoJSON (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "bbox": {"type": "text", "description": "[minLon, minLat, maxLon, maxLat]."},
        "centroid": {"type": "text", "description": "[centroidLon, centroidLat]."},
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_bbox",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

GEO_POINT_IN_POLYGON_SPEC = {
    "name": "Geo Point In Polygon",
    "description": (
        "Test whether lat/lon points fall inside a GeoJSON Polygon or "
        "MultiPolygon using the ray-casting algorithm. Accepts `geojson` "
        "(Polygon/MultiPolygon) and `points` (array of [lat, lon] pairs). "
        "Outputs `point_in_polygon.txt` — JSON array of booleans (true if "
        "inside, false otherwise). Pure Python (std lib), no network. "
        "Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_point_in_polygon",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "GeoJSON and points (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "point_in_polygon": {"type": "text", "description": "JSON array of booleans."},
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_point_in_polygon",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

GEO_GEOJSON_VALIDATE_SPEC = {
    "name": "Geo JSON Validate",
    "description": (
        "Validate GeoJSON structure and coordinate ranges (lat -90..90, "
        "lon -180..180). Accepts `geojson` as a JSON string or dict. "
        "Outputs `validation.txt` with status (\"valid\" / \"invalid\") "
        "and a list of error strings. No network — pure Python (stdlib). "
        "Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_geojson_validate",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "GeoJSON to validate (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "validation": {
            "type": "text",
            "description": "JSON with status and errors.",
        },
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_geojson_validate",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

GEO_COORD_FORMAT_SPEC = {
    "name": "Geo Coord Format",
    "description": (
        "Convert between decimal degrees and DMS (degrees, minutes, "
        "seconds). Mode `to_dms` accepts lat/lon numbers and outputs "
        "DMS strings like \"51°30'26.64\\\"N\". Mode `to_decimal` accepts "
        "DMS strings or objects and outputs decimal lat/lon as JSON. "
        "Outputs `converted_value.txt`. Pure Python (stdlib), no network. "
        "Backed by the geo-utils container."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "geo_coord_format",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "data": {"type": "text", "description": "Coordinates (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "converted_value": {
            "type": "text",
            "description": "DMS string or decimal JSON.",
        },
    },
    "configuration_schema": {
        "GEO_OP": {
            "type": "string",
            "default": "geo_coord_format",
            "description": (
                "Fixed for this node: selects the geo-utils operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "mode": {
                "type": "string",
                "default": "to_decimal",
                "enum": ["to_dms", "to_decimal"],
                "description": (
                    "'to_dms': decimal → DMS string. "
                    "'to_decimal': DMS → decimal JSON."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

# ---------------------------------------------------------------------------
# Registry lists
# ---------------------------------------------------------------------------

NODE_DEFINITIONS = [
    GEO_DISTANCE_SPEC,
    GEO_GEOHASH_ENCODE_SPEC,
    GEO_GEOHASH_DECODE_SPEC,
    GEO_BBOX_SPEC,
    GEO_POINT_IN_POLYGON_SPEC,
    GEO_GEOJSON_VALIDATE_SPEC,
    GEO_COORD_FORMAT_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "geo_distance": GEO_DISTANCE_SPEC,
    "geo_geohash_encode": GEO_GEOHASH_ENCODE_SPEC,
    "geo_geohash_decode": GEO_GEOHASH_DECODE_SPEC,
    "geo_bbox": GEO_BBOX_SPEC,
    "geo_point_in_polygon": GEO_POINT_IN_POLYGON_SPEC,
    "geo_geojson_validate": GEO_GEOJSON_VALIDATE_SPEC,
    "geo_coord_format": GEO_COORD_FORMAT_SPEC,
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


def register_geo(db: Session) -> int:
    """Register the 7 geo operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system
    row with the same name has its schema/description/cost reconciled to
    this file (so a spec edit reaches a live deployment without a manual
    DB edit), and is not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping geo domain-primitive seed"
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
