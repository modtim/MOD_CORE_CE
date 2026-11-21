---
sidebar_position: 1
slug: /getting-started
title: Getting Started
description: Up and running with MOD Core (Community Edition) — about 5 minutes of clicking once the images are on your computer (the first download can take tens of minutes) — boot the stack, sign in with the seeded admin, upload a file, build a workflow, run it, see results. Plus an API quickstart in curl, Python, and JavaScript.
dashboard_route: '#home'
---

# Getting Started

Get up and running with MOD Core in about 5 minutes once the images are on this
computer — first in the UI, then with the API. The first start downloads about
9 GB of images and can take tens of minutes depending on your connection (about
43 minutes was measured on one clean-room install), and the first
`docker compose up -d` took about 12 minutes on a 30-core box; later starts are
much faster. See the README Quickstart, "What to expect". The 9 GB is the install
minimum (plan for about 20 GiB free), not a one-time figure: plan for more free disk over time, because container
logs, the audit log, image layers from each upgrade and your own data all grow.

GPU nodes need GPU memory on the worker they run on: the scheduler starts a task on a worker only when its planned peak VRAM fits that worker's available VRAM, and a task that does not fit at that moment is given a later start, so a card with less free VRAM runs fewer of them at once. Which nodes use a GPU is listed in the [Node Library](/node-library).

MOD Core runs **single-tenant**: there is no signup. One install *is* your
tenant, and it ships with one pre-seeded user.

## 1. Boot the stack

Follow the [README quickstart](https://github.com/modtim/MOD_CORE_CE#-from-download-to-running-in-three-steps)
at the root of this repository. In short:

```bash
./scripts/install.sh
```

The installer creates your passwords (every infra credential, including the optional
image-registry login `REGISTRY_PASSWORD`, is generated randomly into `.env`, mode 0600),
downloads the images and starts MOD. It asks
one question: **"Open this install to other computers on your network? [Y/n]"**.
Press Enter to accept the default: MOD opens to your network at the address the
installer detects (it prints it once). Answer `n` to keep MOD on this computer
only. To skip the question use `./scripts/install.sh --lan` (open to the
network), `./scripts/install.sh --host mod.office.lan` (open, use this name or
IP) or `./scripts/install.sh --no-lan` (this computer only). Without a terminal
and without a flag, a fresh install opens to the detected address (with no
detectable address it stays on this computer only and says so).

Other install flags: `--no-pull` (offline use: start only the images already on
this computer), `--no-start` (write `.env` and start nothing), `--bind ADDR`
(the address the ports listen on, with `--lan`), `--keycloak-port N` (move the
Keycloak host port off 8080) and `--enable-builder` (add the node builder to
`.env` and exit). Run `./scripts/install.sh --help` for the full list.

When you open it to your network, anyone on that network can reach the sign-in
pages and traffic is plain HTTP, so use it on a network you trust. The
databases, Valkey, OpenBao and the mail catcher are never opened. If the
computer's address changes, run `./scripts/install.sh --lan` again. Check the
setup any time with `./scripts/ce-doctor.sh`. More detail:
[Open MOD to other computers on your network](lan-access).

A no-flag install opens the ports to your network at the detected address
(bind `0.0.0.0`); `--no-lan` keeps all ports on `127.0.0.1` (this computer only).
When the ports are open, the installer prints the address to use in place of
`localhost`; replace `localhost` below with that address.
The two optional compose profiles are the exception: with the `registry` profile
on, the image registry listens on `0.0.0.0:5000` (every interface) on purpose, so
other worker machines can pull built nodes — set `REGISTRY_BIND=127.0.0.1` to keep
it on this machine. With the `stack-ingress` profile on, the edge listens on
`8880`/`8443` at the same address as the browser ports (`CE_BIND_ADDR`):

| Surface | URL |
| --- | --- |
| API | `http://localhost:8000` (OpenAPI / Swagger explorer at `/docs`) |
| Dashboard | `http://localhost:3000` |
| WorkflowBuilder | `http://localhost:3001` |
| Docs site | `http://localhost:3002` |
| Keycloak | `http://localhost:8080` (or the port you passed with `--keycloak-port N`) |
With `--lan` the Keycloak admin console on the same port is also reachable from
your network; its password does not rotate online (see the [operations guide](https://github.com/modtim/MOD_CORE_CE/blob/main/docs/operations.md)).
| File storage | `http://localhost:7070` |

## 2. Sign in

There is no registration page. One user is pre-seeded in the `modtex` realm:

- **`admin@core.local`**, realm role `admin`.

The first sign-in shows a **Complete Your Profile** form: pick a username (not
`admin` or `root`, those are reserved) and an organisation name; the first
person to finish it becomes the platform admin.

The service account `service-account-fastapi-admin-service` is a machine account
with no password; it cannot sign in.

Its password is the value of `KEYCLOAK_REALM_ADMIN_PASSWORD` in your `.env`,
generated by `scripts/first-run-secrets.sh`. Read it on the host with
`grep '^KEYCLOAK_REALM_ADMIN_PASSWORD=' .env` — never paste it into a shell
history you intend to keep, a commit, or an issue. Keycloak may ask for a new
password on first login, because the realm import marks the seeded user's
password `temporary` (this is not yet re-proven on a clean CE install). If it
does, set a new one when prompted; you can also change it any time in the
account console.

Open the Dashboard at `http://localhost:3000` and the WorkflowBuilder at
`http://localhost:3001`; both send you to Keycloak and sign you in as that
user.

## 3. Upload your first file

1. In the Dashboard sidebar, click **The Cellar - Files** (the page title reads *Data*).
2. Click **Upload** and pick your files.
3. Supported formats include images (PNG, JPG, TIFF, EXR), 3D meshes (OBJ, FBX,
   GLB, PLY), video (MP4, MOV), and audio (MP3, WAV).

## 4. Create a workflow

1. Click **The Road - Orchestration** in the sidebar to open the Workflow Builder.
2. The node palette is grouped in collapsed categories: click **Input**, then drag an **Image Input** node onto the canvas.
3. Drag a processing node onto the canvas — the nodes that ship in the
   Community Edition are the ones in the CE seed-list,
   `CORE/API/services/node-seed-keeplist.yaml` (for example
   **Image Crop**, **Image Rotate**, **Image Metadata**, **Image Strip Metadata**,
   **Image Watermark**, **Text LLM**, **FFmpeg (CPU)**, **Human Approval**, and
   web search).
4. Click **Export**, then drag a **Return Assets Export** node onto the canvas.
5. Connect Image Input → processing node → Return Assets Export.
6. Click **Save** and enter a **Name** in the *Save As New Workflow* dialog. If a second dialog with **Save as-is** appears, click it.

## 5. Run it

1. Click the **Image Input** node, click **Choose Files**, open **My Files** in the *Select Files* dialog, click your uploaded file and click **Select**.
2. Click **Execute** in the toolbar.
3. Watch the run in **The Crossroads - Process Queue** (the nodes on the canvas do not change colour); its status reads `completed` when done.
4. Right-click the **Return Assets Export** node, choose **Output versions…** and use the download link.

---

## Quickstart with the API

Everything above is also available over the [REST API](/api-reference). Here's
the full loop — **upload → run → poll → download** — in three languages.

First, create an API key in **Settings → API Keys → Create API Key** (it starts with `sk_` and
is shown once). API requests use a short-lived **Bearer token** that you get by
exchanging that key — see [Authentication](/api-reference#authentication).

### curl

```bash
BASE=http://localhost:8000/v1

# 0. Exchange your sk_ key for a ~1h Bearer token
TOKEN=$(curl -s -X POST "$BASE/api-keys/token" -H "Content-Type: application/json" \
  -d '{"api_key": "sk_your_key_here"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# 1. Upload a file -> returns the new File; its "cog_id" is the Cog created for it
COG=$(curl -s -X POST "$BASE/files/upload" -H "$AUTH" \
  -F "file=@./input.png" | jq -r .cog_id)

# 2. Pick a saved workflow version: each workflow in the list carries its
#    latest_version_id (42 in the examples is only an example id to replace)
WORKFLOW_VERSION_ID=$(curl -s "$BASE/road/" -H "$AUTH" | jq -r '.[0].latest_version_id')

# 3. Run a saved workflow version against that file (202 Accepted)
RUN=$(curl -s -X POST "$BASE/road/workflow-versions/$WORKFLOW_VERSION_ID/run" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d "{\"cog_ids\": [$COG]}" | jq -r .workflow_run_id)

# 4. Poll until the run reaches a terminal status
curl -s "$BASE/road/workflow-runs/$RUN" -H "$AUTH" | jq '{status, run_files}'

# 5. Output files are listed in run_files; download one by its file id
FILE_ID=$(curl -s "$BASE/road/workflow-runs/$RUN" -H "$AUTH" | jq -r '.run_files[0].file_id')
curl -s "$BASE/files/$FILE_ID/content" -H "$AUTH" -o result.png
```

### Python

```python
import time
import requests

BASE = "http://localhost:8000/v1"

# 0. Exchange your sk_ key for a Bearer token
token = requests.post(f"{BASE}/api-keys/token",
                      json={"api_key": "sk_your_key_here"}).json()["access_token"]
HEADERS = {"Authorization": f"Bearer {token}"}

# 1. Upload
with open("input.png", "rb") as f:
    uploaded = requests.post(f"{BASE}/files/upload", headers=HEADERS,
                             files={"file": f}).json()
cog_id = uploaded["cog_id"]
print("uploaded cog", cog_id)

# 2. Pick a saved workflow version (latest_version_id from the list call) and run it
version_id = requests.get(f"{BASE}/road/", headers=HEADERS).json()[0]["latest_version_id"]
run = requests.post(f"{BASE}/road/workflow-versions/{version_id}/run", headers=HEADERS,
                    json={"cog_ids": [cog_id]}).json()
run_id = run["workflow_run_id"]

# 3. Poll until the run reaches a terminal status
TERMINAL = ("completed", "failed")
while True:
    r = requests.get(f"{BASE}/road/workflow-runs/{run_id}", headers=HEADERS).json()
    print(r["status"])
    if r["status"] in TERMINAL:
        break
    time.sleep(3)

# 4. Output files are listed in run_files
for rf in r.get("run_files", []):
    print("output:", rf["title"], rf["id"])
```

### JavaScript (Node 18+ / browser)

```javascript
const BASE = "http://localhost:8000/v1";

// 0. Exchange your sk_ key for a Bearer token
const { access_token } = await fetch(`${BASE}/api-keys/token`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ api_key: "sk_your_key_here" }),
}).then(r => r.json());
const headers = { Authorization: `Bearer ${access_token}` };

// 1. Upload
const form = new FormData();
form.append("file", fileBlob, "input.png");
const uploaded = await fetch(`${BASE}/files/upload`, { method: "POST", headers, body: form })
  .then(r => r.json());

// 2. Pick a saved workflow version (latest_version_id from the list call) and run it
const workflows = await fetch(`${BASE}/road/`, { headers }).then(r => r.json());
const versionId = workflows[0].latest_version_id;
const { workflow_run_id } = await fetch(`${BASE}/road/workflow-versions/${versionId}/run`, {
  method: "POST",
  headers: { ...headers, "Content-Type": "application/json" },
  body: JSON.stringify({ cog_ids: [uploaded.cog_id] }),
}).then(r => r.json());

// 3. Poll until terminal
const TERMINAL = ["completed", "failed"];
let run;
do {
  await new Promise(r => setTimeout(r, 3000));
  run = await fetch(`${BASE}/road/workflow-runs/${workflow_run_id}`, { headers })
    .then(r => r.json());
  console.log(run.status);
} while (!TERMINAL.includes(run.status));

console.log("outputs:", run.run_files);
```

> `42` in the examples is only an example **workflow version id**: use your own.
> The curl example reads it from `latest_version_id` in `GET /road/`, or
> copy it from the Workflow Builder URL when you open your saved workflow. The
> token inherits your permissions, and
> you can run any endpoint interactively in the
> [Swagger explorer](http://localhost:8000/docs).

## What is not in Community Edition

MOD Core is open-core. Features that belong to the paid editions are **not** in
this repository — see
`docs/community-vs-paid.md` in the repository for the full boundary map.

## Next steps

- [Workflow Builder Guide](/workflow-builder) — nodes, connections, batch processing.
- [Bind a model to the Wizard](/bind-a-model-to-the-wizard) — pick the AI model the Node Wizard uses.
- [Node Library](/node-library) — the catalog of processing nodes.
- [API Reference](/api-reference) — every endpoint, with a live "try it" explorer.
- [Worker Setup](/worker-setup) — add your own processing hardware.
