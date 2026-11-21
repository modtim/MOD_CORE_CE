#!/bin/bash
set -e

echo "=== Input Validator ===" >&2
echo "Input Type: ${INPUT_TYPE}" >&2
echo "Workflow Run: ${WORKFLOW_RUN_ID}" >&2
echo "Tenant: ${TENANT_ID}" >&2

mkdir -p /data/output

python3 /app/validate.py

echo "=== Validation complete ===" >&2
