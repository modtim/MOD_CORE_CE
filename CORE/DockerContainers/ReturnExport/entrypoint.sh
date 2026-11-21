#!/bin/bash
set -e

echo "=== Export Container ===" >&2
echo "Workflow Run: ${WORKFLOW_RUN_ID}" >&2
echo "Tenant: ${TENANT_ID}" >&2

mkdir -p /data/output

# Calibration mode: real export path talks to platform API (DNS + auth
# + manifest POST), fails in the calibration sandbox. Synthetic
# manifest preserves the I/O-bound wall-clock profile. Mirrors
# unity-export / unreal-export handlers — all three share export.py.
if [ "${CALIBRATION_RUN,,}" = "true" ]; then
    echo "CALIBRATION_RUN=true → running synthetic Return Assets export" >&2
    exec python3 -c "
import json, os, time, hashlib
started = time.time()
manifest = {
    'workflow_run_id': os.environ.get('WORKFLOW_RUN_ID', '0'),
    'tenant_id': os.environ.get('TENANT_ID', '0'),
    'assets': [
        {'name': f'asset_{i:03d}', 'kind': 'mixed',
         'hash': hashlib.sha256(f'a{i}'.encode()).hexdigest()}
        for i in range(50)
    ],
    'calibration_run': True,
}
time.sleep(float(os.environ.get('CALIBRATION_SLEEP_S', '1.0')))
with open('/data/output/manifest.json', 'w') as f:
    json.dump(manifest, f, indent=2)
duration = time.time() - started
print(f'[return-export] calibration synthetic complete ({duration:.2f}s)')
"
fi

python3 /app/export.py

echo "=== Export complete ===" >&2
