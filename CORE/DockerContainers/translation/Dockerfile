# translation — MOD node container for the translation domain primitive
# (launch-completion-megaplan sz-6.8.1). One operation:
#
#   TRANSLATE_OP=translate_text   fields.json{text,SOURCE,TARGET} -> translated.txt + meta.json
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output. Parameters
# arrive as /data/input/fields.json (the node's `text` port plus SOURCE/TARGET).
#
# OFFLINE posture: argostranslate language packages are downloaded ONCE at
# IMAGE BUILD time (pinned URLs, sha256-verified) and installed with
# argostranslate.package.install_from_path. At runtime nothing reaches out —
# the engine is loaded only from ARGOS_PACKAGES_DIR, an unbaked pair is
# rejected (never fetched), and SOURCE=auto is not supported.
#
# Baked pairs: en<->es, en<->fr, en<->de. Input capped at 50k chars, chunked
# by paragraph (<=5k chars per engine call). Bad input exits non-zero with
# JSON in /data/output/error.json.
#
# Authored only in sz-6.8.1 — build/push is a lead deploy step (no disk space
# for a build in the agent lane). The sha256 pins below MUST be filled from the
# argos package release the lead pins; the install step refuses to run on
# placeholder digests so a forgotten pin can never ship silently.

FROM python:3.11-slim

# Pinned engine. argostranslate 1.9.6 is the release the packages baked below
# were generated against; ctranslate2 is its inference backend.
RUN pip install --no-cache-dir argostranslate==1.9.6 ctranslate2==4.4.0

RUN mkdir -p /data/input /data/output /app /app/argos-packages

# Language packages baked at build time. Each pair: pinned URL + pinned sha256.
# URLs verified against the argospm index (argosopentech/argospm-index
# index.json): each pair's .argosmodel is a zip archive served from
# argos-net.com, NOT a .tar.gz — it is saved as-is (never extracted) so
# _baked_package_path() can glob it directly, and its own filename already
# carries the "<src>_<tgt>" pattern that glob matches against.
# The lead fills the six digests from the argos release it pins; anything left
# as a placeholder makes this RUN step fail (loudly, at build time).
ENV ARGOS_PKG_URLS="https://argos-net.com/v1/translate-en_es-1_0.argosmodel https://argos-net.com/v1/translate-es_en-1_9.argosmodel https://argos-net.com/v1/translate-en_fr-1_9.argosmodel https://argos-net.com/v1/translate-fr_en-1_9.argosmodel https://argos-net.com/v1/translate-en_de-1_3.argosmodel https://argos-net.com/v1/translate-de_en-1_3.argosmodel"
ENV ARGOS_PKG_SHA256="d698d0ef87ad70d5d184b7fa6965905bf4368f09a2bb9ffb165a79bac96af0c4 c54df2b62fceaf54a3ce5d97db6bf56efd7940063329f6778f4212d2acb370d4 3a65ed83364f4e7b06e30f9dd823db1934899ed3ce839e63f46dc7b09dc797b4 3b3052fee6bb1e8e8e632a26a723eb2a2c7710dfe73ba61ffd9b83e85d4f14c1 6cd847f0c06c9c66013e6b0932e07fd54a6d90894659c02bf6c5247b72fb25b1 becc2b0011f8249fcb89be9ecb75ba0d876b1fab93c28ee6ff0420936897d637"

RUN set -eu; \
    i=1; \
    for url in $ARGOS_PKG_URLS; do \
        sha=$(printf "%s" "$ARGOS_PKG_SHA256" | cut -d" " -f"$i"); \
        i=$((i + 1)); \
        case "$sha" in TBD*) echo "FATAL: sha256 pin for $url is unfilled"; exit 1;; esac; \
        dest="/app/argos-packages/$(basename "$url")"; \
        python3 -c "import shutil, sys, urllib.request; r = urllib.request.urlopen(urllib.request.Request(sys.argv[1], headers={'User-Agent': 'curl/8.5.0'}), timeout=600); shutil.copyfileobj(r, open(sys.argv[2], 'wb'))" "$url" "$dest"; \
        echo "$sha  $dest" | sha256sum -c -; \
    done

# The entrypoint is written here rather than COPYed so the container is
# self-contained (one file, no sidecar to keep in sync). This block is a
# verbatim mirror of ENTRYPOINT_SOURCE in
# CORE/API/node_definitions_domains/translation.py.
RUN cat > /app/translate_process.py <<'PYEOF'
#!/usr/bin/env python3
# translation entrypoint — one operation per run, selected by TRANSLATE_OP.
#
#   export TRANSLATE_OP=translate_text
#   python3 /app/translate_process.py
#
# I/O contract is the same as every other MOD node container: the worker stages
# the node's input files into /data/input and collects /data/output. Parameters
# arrive as /data/input/fields.json (the node's `text` port plus SOURCE/TARGET).
#
# Offline posture: the engine is argostranslate and the six language packages
# (en<->es, en<->fr, en<->de) are baked into the image at BUILD time, sha256-
# verified. Nothing here ever reaches out to the internet: the engine is loaded
# ONLY from ARGOS_PACKAGES_DIR, and a pair that is not baked is rejected, never
# fetched. SOURCE=auto is not supported (the engine must load a specific baked
# package). Bad input exits non-zero with a JSON error in
# /data/output/error.json.

import json
import os
import sys
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_CHARS = 50_000            # input cap
CHUNK_MAX_CHARS = 5_000       # per engine call
ENGINE = "argostranslate"

# Baked pairs only. "auto" is deliberately NOT in here: see the header.
ALLOWED_PAIRS = ("en>es", "en>fr", "en>de", "es>en", "fr>en", "de>en")
ARGOS_PACKAGES_DIR = os.environ.get("ARGOS_PACKAGES_DIR", "/app/argos-packages")


def _log(msg):
    print(msg, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def fail(msg, code="BAD_PARAM"):
    # one line of error JSON on stdout, then a non-zero exit. Keep writing the
    # error.json sidecar other callers (and the golden-path test) expect.
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    _write_error(msg)
    sys.exit(1)


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        raise ValueError("fields.json required on the input port")
    raw = p.read_bytes()
    if len(raw) > MAX_CHARS * 4:
        raise ValueError(f"fields.json exceeds the {MAX_CHARS * 4}-byte input cap")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fields.json must be a JSON object")
    return _normalize_fields(data)


# The platform hands us fields.json keyed by the spec's parameter names, which
# this domain spells in caps (SOURCE, TARGET). Accept the lowercase spelling
# too, and fold the text-port spellings onto one `text` key so the op stays a
# single pure function.
FIELD_ALIASES = {
    "source_language": "source",
    "target_language": "target",
    "payload": "text",
    "input_text": "text",
}


def _normalize_fields(data):
    out = {}
    for key, value in data.items():
        low = str(key).strip().lower()
        out[low] = value
        out[FIELD_ALIASES.get(low, low)] = value
    return out


def _text(fields):
    raw = str(fields.get("text", ""))
    if not raw.strip():
        raise ValueError(
            "Missing text: supply the text to translate on the node's text port"
        )
    if len(raw) > MAX_CHARS:
        raise ValueError(
            f"text is {len(raw)} chars, over the {MAX_CHARS}-char input cap — "
            "split it upstream (Text Split) or raise the cap deliberately"
        )
    return raw


def _pair(fields):
# Validate SOURCE/TARGET against the baked-pair allowlist.
# 
# "auto" is rejected on both ends: the engine has to load one specific baked
# package, so it cannot guess either language.
    source = str(fields.get("source", "")).strip().lower()
    target = str(fields.get("target", "")).strip().lower()
    if not source or not target:
        raise ValueError(
            "SOURCE and TARGET are both required (SOURCE=auto is not supported — "
            "the engine loads one specific baked package)"
        )
    if source == target:
        raise ValueError("SOURCE and TARGET must differ")
    pair = f"{source}>{target}"
    if pair not in ALLOWED_PAIRS:
        raise ValueError(
            f"pair {source}->{target} is not baked in this image "
            f"(allowed: {', '.join(ALLOWED_PAIRS)}); unbaked pairs are rejected, "
            "never downloaded, and never pivoted through a third language"
        )
    return source, target


def _chunks(text):
# Split on paragraph boundaries, packing paragraphs into <=CHUNK_MAX_CHARS
# calls. Order is preserved; a single oversized paragraph is hard-split on
# character boundaries as a last resort.
    paragraphs = [p for p in text.split("\n\n") if p.strip() != ""]
    chunks = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= CHUNK_MAX_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(para) > CHUNK_MAX_CHARS:
            chunks.append(para[:CHUNK_MAX_CHARS])
            para = para[CHUNK_MAX_CHARS:]
        current = para
    if current:
        chunks.append(current)
    return chunks


def _baked_package_path(source, target):
# Locate the .argosmodel baked for this pair, or None.
# 
# Only ever looks inside ARGOS_PACKAGES_DIR — the baked dir. There is no
# download path in this file at all.
    base = Path(ARGOS_PACKAGES_DIR)
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.argosmodel")):
        name = path.name.lower()
        if f"{source}2{target}" in name or (
            f"{source}_{target}" in name or f"{source}-to-{target}" in name
        ):
            return str(path)
    return None


def _load_engine(source, target):
# Return the argostranslate translation object for this baked pair.
    path = _baked_package_path(source, target)
    if path is None:
        raise ValueError(
            f"no baked package found for {source}->{target} in {ARGOS_PACKAGES_DIR}"
        )
    from argostranslate import package, translate

    # Offline: install_from_path on the already-baked local file just indexes it.
    # There is no URL anywhere in this file — argostranslate never fetches here.
    package.install_from_path(path)
    engine = translate.get_translation_from_codes(source, target)
    if engine is None:
        raise ValueError(f"installed package for {source}->{target} not found after install")
    return engine


def op_translate_text(fields):
# Translate the payload -> translated.txt + meta.json.
    text = _text(fields)
    source, target = _pair(fields)
    engine = _load_engine(source, target)
    pieces = [engine.translate(chunk) for chunk in _chunks(text)]
    translated = "\n\n".join(p for p in pieces if p is not None)
    _write_text("translated.txt", translated)
    _write_text(
        "meta.json",
        json.dumps(
            {
                "source": source,
                "target": target,
                "chars": len(translated),
                "engine": ENGINE,
            },
            indent=2,
        ),
    )
    _log(
        f"translated {len(text)} chars {source}->{target} in {len(pieces)} chunk(s) "
        f"-> translated.txt ({len(translated)} chars)"
    )


OPS = {
    "translate_text": op_translate_text,
}


def main():
    op = os.getenv("TRANSLATE_OP", "translate_text").strip().lower()
    # The spec default is the node's own op key ("translate_text"); the container
    # speaks the same name. Accept a "translation_" prefix just in case.
    if op not in OPS and op.startswith("translation_"):
        op = op[len("translation_"):]
    if op not in OPS:
        fail(f"unknown TRANSLATE_OP {op!r}", code="BAD_PARAM")
    try:
        fields = _read_fields()
        OPS[op](fields)
    except ValueError as exc:
        fail(str(exc) or exc.__class__.__name__, code="BAD_PARAM")
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        fail(f"{type(exc).__name__}: {exc}", code="INTERNAL")


if __name__ == "__main__":
    main()
PYEOF

# Where the engine looks for baked packages. Set explicitly (not left to argos'
# default ~/.local/share) so runtime never consults a user-writable path.
ENV ARGOS_PACKAGES_DIR=/app/argos-packages

WORKDIR /data
ENTRYPOINT ["python3", "/app/translate_process.py"]
