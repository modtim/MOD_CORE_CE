#!/bin/bash
# δ DAG-wizard B4 — multi-stage entrypoint dispatcher.
# Worker invokes this with env vars:
#   STAGE              one of: build_variant | smoke_variant |
#                              push_variant | sample_test | register_node
#   BUILD_SPEC_ID      build_specs row id to fetch the spec from
#   VARIANT            (build/smoke/push only) which variant key
#   API_URL            internal API URL for fetching the spec
#   API_KEY            service token for API auth
set -euo pipefail

# CE node builder isolation: the build stages run the untrusted Dockerfile in the
# rootless BuildKit sidecar (compose service `buildkitd`) through a buildx
# remote builder, NOT in the host Docker daemon (root on the host). The built
# image is still loaded into the host daemon (`--output=type=docker`) so the
# smoke/push stages can use it. build_command() (stages/_common.py) already
# honours BUILD_BUILDER. See docs/editions/ce-cut-audit/node-builder-buildkit.md.
case "${STAGE:-}" in
    build_variant|test_and_repair_variant)
        if [ -n "${BUILDKIT_HOST:-}" ]; then
            docker buildx rm mod-buildkit >/dev/null 2>&1 || true
            docker buildx create --name mod-buildkit --driver remote "$BUILDKIT_HOST" >/dev/null
            if ! timeout 60 docker buildx inspect mod-buildkit --bootstrap >/dev/null 2>&1; then
                echo "ERROR: BuildKit sidecar $BUILDKIT_HOST is not reachable (docker compose --profile builder up -d)" >&2
                echo '{"error":"buildkit sidecar unreachable","code":"BUILDER_UNAVAILABLE"}'
                exit 1
            fi
            export BUILD_BUILDER=mod-buildkit
        elif [ "${MOD_REQUIRE_ISOLATED_BUILD:-}" = "1" ]; then
            echo "ERROR: MOD_REQUIRE_ISOLATED_BUILD=1 but BUILDKIT_HOST is not set; refusing to build in the host Docker daemon" >&2
            echo '{"error":"isolated build required but no buildkit sidecar configured","code":"BUILDER_UNAVAILABLE"}'
            exit 1
        fi
        ;;
esac

STAGE="${STAGE:-}"
if [ -z "$STAGE" ]; then
    echo "ERROR: STAGE env var required" >&2
    # Contract JSON on stdout (CORE/scripts/vet-container.sh fail-closed smoke).
    echo '{"error":"STAGE env var required","code":"BAD_PARAM"}'
    exit 1
fi

case "$STAGE" in
    build_variant)
        exec python3 /app/stages/build_one_variant.py
        ;;
    smoke_variant)
        exec python3 /app/stages/smoke_one_variant.py
        ;;
    push_variant)
        exec python3 /app/stages/push_one_variant.py
        ;;
    sample_test)
        exec python3 /app/stages/sample_one_test.py
        ;;
    sample_test_variant)
        exec python3 /app/stages/sample_test_one_variant.py
        ;;
    register_node)
        exec python3 /app/stages/register_node.py
        ;;
    test_and_repair_variant)
        exec python3 /app/stages/test_and_repair_one_variant.py
        ;;
    ingest_artifacts)
        exec python3 /app/stages/ingest_artifacts.py
        ;;
    *)
        echo "ERROR: unknown STAGE '$STAGE'" >&2
        echo '{"error":"unknown STAGE","code":"BAD_PARAM"}'
        exit 1
        ;;
esac
