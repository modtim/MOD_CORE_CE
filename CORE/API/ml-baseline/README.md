# ml-baseline/ — baked-in scheduler ONNX baseline

This directory holds a **stage-trained scheduler ONNX bundle** that
gets baked into the API container image at build time
(`Dockerfile:ARG ML_BASELINE_VERSION`). On first boot in a deployment
with an empty MinIO `platform-models/` bucket, the API seeds these
files into MinIO so the hot-reload thread has a model to load —
preventing the "process clumps badly because there's no model"
failure mode on fresh on-prem installs.

## What's in here

For the version pinned by `ML_BASELINE_VERSION`:

- `scheduler-v<N>.onnx` — the model itself
- `scheduler-v<N>.json` — training metadata (version, deployed_at,
  metrics)
- `scheduler-v<N>-normalization.json` — feature normalization params

Bundle size: ~3 MB total (mostly the ONNX file).

## Updating the baseline

1. Identify the stage-blessed version in MinIO:
   ```bash
   docker exec my_fastapi_app python -c "
   from minio import Minio; import os
   c = Minio((os.environ.get('S3_INTERNAL_URL') or os.environ.get('MINIO_INTERNAL_URL')).replace('http://',''),
       access_key=os.environ.get('S3_ROOT_USER') or os.environ.get('MINIO_ROOT_USER'),
       secret_key=os.environ.get('S3_ROOT_PASSWORD') or os.environ.get('MINIO_ROOT_PASSWORD'), secure=False)
   for o in c.list_objects('platform-models', prefix='scheduler-v'):
       print(o.object_name, o.size)
   "
   ```

2. Pull the three files for the chosen `<N>` into a temp dir:
   ```bash
   for f in scheduler-v<N>.onnx scheduler-v<N>.json scheduler-v<N>-normalization.json; do
     docker exec my_fastapi_app python -c "
   from minio import Minio; import os
   c = Minio((os.environ.get('S3_INTERNAL_URL') or os.environ.get('MINIO_INTERNAL_URL')).replace('http://',''),
       access_key=os.environ.get('S3_ROOT_USER') or os.environ.get('MINIO_ROOT_USER'),
       secret_key=os.environ.get('S3_ROOT_PASSWORD') or os.environ.get('MINIO_ROOT_PASSWORD'), secure=False)
   open('/tmp/$f', 'wb').write(c.get_object('platform-models', '$f').read())
   "
     docker cp my_fastapi_app:/tmp/$f CORE/API/ml-baseline/$f
   done
   ```

3. Bump the Dockerfile default:
   ```diff
   -ARG ML_BASELINE_VERSION=2
   +ARG ML_BASELINE_VERSION=<N>
   ```

4. Optionally remove the older versioned files from this directory if
   you're not keeping them for rollback. Build will fail with a clear
   error if `ML_BASELINE_VERSION` doesn't match files present here.

5. Rebuild the API image. New deployments will seed `<N>` instead of
   the prior version. Existing deployments are unaffected — the seed
   step is a no-op if any model already exists in MinIO.

## Why not pull from MinIO at build time?

- Air-gapped builds wouldn't reach a network MinIO.
- Pinning the artifact in git means every commit reproducibly builds
  the same image. Pulling from a network endpoint at build time
  doesn't.
- Operators reviewing the image-size diff in a PR see the actual
  artifact change.

The trade-off is a small manual refresh step when new baselines ship.
That's intentional — bumping the customer's baseline is a release
decision, not a continuous one.
