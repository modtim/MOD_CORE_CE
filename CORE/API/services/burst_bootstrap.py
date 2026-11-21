"""GH.3 — burst worker bootstrap: cloud-init user-data for a burst instance
standing up inside the CUSTOMER's own cloud account (Group H burst).

The rendered user-data:
  1. fetches the EXISTING worker install script
     (GET /v1/workers/agent/install-script) with a ONE-TIME enrol token and
     runs it. Enrolment into the tenant's manager/mesh reuses the existing
     mechanism wholesale: the token row's home_anchor picks the mesh anchor,
     and /workers/enroll/start returns the mesh preauth coordinates the
     installer + Go worker already consume (dual-anchor re-home). No parallel
     token or mesh system is introduced.
  2. labels the worker `burst=true` + `burst_instance_id=<id>` by appending
     MOD_BURST / MOD_BURST_INSTANCE_ID to /etc/mod-worker/worker.env — the
     installer's own EnvironmentFile, which the mod-worker systemd unit
     already feeds to the worker process.
  3. installs a local idle self-destruct: a systemd timer that powers the
     instance off (`shutdown -h now`) after `idle_minutes` with no worker
     task activity, plus a hard max-lifetime deadline. The logic is purely
     local (docker task-container label + local clock), so the instance
     self-destructs even if the manager is unreachable.

Token model: the existing `WorkerEnrollmentToken` row, minted
single-use (max_uses=1), auto_approve + auto_license, tenant-owned, with
`expires_at` set. The plaintext token appears in the user-data (the
installer must carry it) and is never logged.
"""
from __future__ import annotations

import re
import secrets as py_secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Tuple

from sqlalchemy.orm import Session

from models import WorkerEnrollmentToken, WorkerOwnerType
from services import worker_management_service

# Defaults per GH.3: 30 min idle self-destruct, 12 h hard max lifetime.
DEFAULT_IDLE_MINUTES = 30
DEFAULT_MAX_LIFETIME_HOURS = 12

# The one-time token must outlive the fetch+download+enroll sequence.
TOKEN_TTL_MINUTES = 30

# The worker's task containers carry this label; its presence means
# "a worker task is running right now" (the local activity signal).
TASK_CONTAINER_LABEL = "com.example.mod-core.task"

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _sanitize_instance_label(instance_label: str) -> str:
    """The instance label becomes part of the worker name and is written
    verbatim into shell user-data — allow a strict safe charset only."""
    label = str(instance_label or "").strip()
    if not _LABEL_RE.fullmatch(label):
        raise ValueError(
            "instance_label must be 1-64 chars of [A-Za-z0-9._-] "
            f"starting with a letter or digit, got {instance_label!r}"
        )
    return label


def mint_burst_enrollment_token(
    db: Session,
    tenant_id: int,
    *,
    worker_name: str,
    ttl_minutes: int = TOKEN_TTL_MINUTES,
) -> Tuple[WorkerEnrollmentToken, str]:
    """Mint a single-use, expiring, auto-approving, tenant-owned enrol token
    on the EXISTING WorkerEnrollmentToken model (same row the admin
    /workers/enrollment-tokens endpoint writes). Returns (row, plain_token).
    The plain token is returned because the user-data must embed it; it is
    never logged."""
    from security import hash_api_key

    token = py_secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    row = worker_management_service.create_enrollment_token_row(
        db,
        label=f"burst {worker_name}",
        created_by_user_id=None,
        max_uses=1,
        auto_approve=True,
        auto_license=True,
        allowed_name_pattern=None,
        owner_type=WorkerOwnerType.TENANT_OWNED.value,
        assigned_tenant_ids=[tenant_id],
        owner_tenant_id=tenant_id,
        token_hash=hash_api_key(token),
        expires_at=now + timedelta(minutes=ttl_minutes),
        # A burst instance lives in the customer's cloud, not on the manager's
        # site LAN — the cloud anchor is its home plane. Both planes still get
        # keys minted by the existing enroll/start dual-home logic.
        home_anchor="remote",
        created_by_system=True,
    )
    return row, token


# ---------------------------------------------------------------------------
# Embedded units (plain constants, expanded into the user-data verbatim)
# ---------------------------------------------------------------------------

_SELFDESTRUCT_SCRIPT = r"""#!/bin/bash
# MOD burst self-destruct (GH.3). Purely local — no manager/API needed, so
# the instance powers itself off even if the manager is unreachable.
set -u
STATE_DIR=/var/lib/mod-burst
LAST_ACTIVE=$STATE_DIR/last-active
DEADLINE_FILE=/etc/mod-worker/burst-deadline
IDLE_SECS=@IDLE_SECS@

mkdir -p "$STATE_DIR"
if [ ! -f "$LAST_ACTIVE" ]; then
    # Boot time is the first activity stamp: an instance that never runs a
    # task still self-destructs after the idle window.
    touch "$LAST_ACTIVE"
fi

# Hard max-lifetime cap: power off no matter what, even if tasks keep it busy.
if [ -f "$DEADLINE_FILE" ]; then
    DEADLINE=$(cat "$DEADLINE_FILE" 2>/dev/null) || DEADLINE=""
    if [ -n "$DEADLINE" ] && [ "$(date +%s)" -ge "$DEADLINE" ]; then
        logger -t mod-burst "burst max lifetime reached; powering off"
        shutdown -h now
        exit 0
    fi
fi

# A worker task container running right now counts as activity.
if command -v docker >/dev/null 2>&1; then
    if docker ps -q --filter "label=@TASK_LABEL@" 2>/dev/null | grep -q .; then
        touch "$LAST_ACTIVE"
        exit 0
    fi
fi

NOW=$(date +%s)
LAST_TS=$(stat -c %Y "$LAST_ACTIVE" 2>/dev/null) || LAST_TS=$NOW
if [ $((NOW - LAST_TS)) -ge "$IDLE_SECS" ]; then
    logger -t mod-burst "burst worker idle for $IDLE_SECS s; powering off"
    shutdown -h now
fi
"""

_SERVICE_UNIT = """[Unit]
Description=MOD burst self-destruct (idle shutdown + max lifetime)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/mod-burst-selfdestruct.sh
"""

_TIMER_UNIT = """[Unit]
Description=MOD burst self-destruct timer

[Timer]
OnBootSec=1min
OnUnitActiveSec=60s
AccuracySec=10s

[Install]
WantedBy=timers.target
"""

# @PLACEHOLDER@ substitution keeps the shell `$VAR`/`$(...)` text untouched.
_USER_DATA_TEMPLATE = r"""#!/bin/bash
# MOD burst worker bootstrap (GH.3) — tenant @TENANT_ID@, instance "@INSTANCE_LABEL@".
# The one-time enrol token below is single-use and expires; it is the only
# secret in this document and it is never logged.
set -euo pipefail

API_URL="@API_BASE_URL@"
WORKER_NAME="@WORKER_NAME@"

echo "==> [1/3] Fetching MOD worker install script (one-time token) ..."
curl -fsSL "@API_BASE_URL@/v1/workers/agent/install-script?token=@TOKEN@" | bash -s -- \
    --api-url "$API_URL" \
    --token "@TOKEN@" \
    --worker-name "$WORKER_NAME"

echo "==> [2/3] Labeling worker as burst ..."
{
    echo "MOD_BURST=true"
    echo "MOD_BURST_INSTANCE_ID=@INSTANCE_LABEL@"
} >> /etc/mod-worker/worker.env

echo "==> [3/3] Installing burst self-destruct (idle @IDLE_MINUTES@ min, max lifetime @MAX_HOURS@ h) ..."
echo "@DEADLINE@" > /etc/mod-worker/burst-deadline
cat > /usr/local/bin/mod-burst-selfdestruct.sh <<'BURSTSD'
@SELFDESTRUCT@
BURSTSD
chmod 0755 /usr/local/bin/mod-burst-selfdestruct.sh
cat > /etc/systemd/system/mod-burst-selfdestruct.service <<'BURSTSV'
@SERVICE@
BURSTSV
cat > /etc/systemd/system/mod-burst-selfdestruct.timer <<'BURSTT'
@TIMER@
BURSTT
systemctl daemon-reload
systemctl enable --now mod-burst-selfdestruct.timer
echo "==> Burst bootstrap complete: worker=$WORKER_NAME"
"""


def render_burst_user_data(
    db: Session,
    tenant_id: int,
    *,
    instance_label: str,
    idle_minutes: int = DEFAULT_IDLE_MINUTES,
    max_lifetime_hours: int = DEFAULT_MAX_LIFETIME_HOURS,
    api_base_url: str,
) -> str:
    """Render cloud-init user-data that bootstraps one burst worker.

    Mints the one-time enrol token (existing WorkerEnrollmentToken model),
    embeds it in the install-script fetch, labels the worker as burst, and
    arms the local idle self-destruct + hard max-lifetime shutdown.
    Never logs the token.
    """
    if not str(api_base_url or "").strip():
        raise ValueError("api_base_url is required")
    if idle_minutes <= 0:
        raise ValueError("idle_minutes must be > 0")
    if max_lifetime_hours <= 0:
        raise ValueError("max_lifetime_hours must be > 0")

    label = _sanitize_instance_label(instance_label)
    worker_name = f"burst-{label}"
    _row, token = mint_burst_enrollment_token(db, tenant_id, worker_name=worker_name)
    deadline = int(time.time()) + int(round(max_lifetime_hours * 3600))

    # Pre-substitute the embedded units FIRST: they carry their own @KEY@
    # placeholders and would otherwise be spliced in after those keys were
    # already consumed by the outer template pass.
    script = (
        _SELFDESTRUCT_SCRIPT
        .replace("@IDLE_SECS@", str(int(idle_minutes) * 60))
        .replace("@TASK_LABEL@", TASK_CONTAINER_LABEL)
    )

    user_data = _USER_DATA_TEMPLATE
    substitutions = {
        "TENANT_ID": str(tenant_id),
        "INSTANCE_LABEL": label,
        "API_BASE_URL": str(api_base_url).strip().rstrip("/"),
        "WORKER_NAME": worker_name,
        "TOKEN": token,
        "IDLE_MINUTES": str(int(idle_minutes)),
        "MAX_HOURS": str(max_lifetime_hours),
        "DEADLINE": str(deadline),
        "IDLE_SECS": str(int(idle_minutes) * 60),
        "TASK_LABEL": TASK_CONTAINER_LABEL,
        "SELFDESTRUCT": script,
        "SERVICE": _SERVICE_UNIT,
        "TIMER": _TIMER_UNIT,
    }
    for key, value in substitutions.items():
        user_data = user_data.replace(f"@{key}@", value)
    return user_data
