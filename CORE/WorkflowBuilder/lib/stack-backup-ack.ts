/**
 * Stack wizard — "no backup plan" acknowledgement (GA.ASW.6b).
 *
 * The API's persistent-stack create paths (POST /stacks, POST
 * /stacks/from-template with `preview` unset/false, POST
 * /stacks/import-compose/finalize) refuse to create a stack that runs a
 * known database image (postgres/mysql/mariadb/mongo — see
 * `CORE/API/services/stack_logical_backup.py:backup_ack_required`) with no
 * backup posture declared, unless the caller sets `acknowledge_no_backup:
 * true`. Refusal is HTTP 409 with
 * `{ detail: { requires_ack: true, reason, services: [{service, image,
 * engine}] } }` — see `_require_backup_ack` in
 * `CORE/API/routers/persistent_stacks/_shared.py`.
 *
 * This mirrors the Dashboard's `_psBackupAckShow` (CORE/Dashboard/js/
 * persistent-stacks.js) for the WorkflowBuilder's two callers of those
 * endpoints (stack-wizard-panel's Deploy, stack-compose-import's finalize):
 * detect the 409 shape, format the inline-warning copy, and build the
 * identical retry body with the acknowledgement appended. Kept
 * side-effect-free where possible so it's unit-tested with jest and shared
 * instead of re-implemented per caller.
 */

export interface BackupAckService {
  service: string;
  image: string;
  engine: string;
}

export interface BackupAckDetail {
  requires_ack: true;
  reason: string;
  services: BackupAckService[];
}

/**
 * Detect the GA.ASW.6 409 shape from an already-parsed response body. Returns
 * null for anything else (a different status, a non-object body, a `detail`
 * that isn't the requires_ack shape) so callers fall through to their normal
 * error handling unchanged.
 */
export function parseBackupAckDetail(status: number, body: unknown): BackupAckDetail | null {
  if (status !== 409) return null;
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const d = detail as Record<string, unknown>;
  if (d.requires_ack !== true) return null;
  const services = Array.isArray(d.services)
    ? d.services.filter(
        (s): s is BackupAckService =>
          !!s &&
          typeof s === "object" &&
          typeof (s as Record<string, unknown>).service === "string" &&
          typeof (s as Record<string, unknown>).image === "string",
      )
    : [];
  return {
    requires_ack: true,
    reason:
      typeof d.reason === "string" && d.reason
        ? d.reason
        : "No backup posture declared for this stack.",
    services,
  };
}

/** "service (image), service (image)" — the list this stack's warning names. */
export function formatBackupAckServices(detail: BackupAckDetail): string {
  return detail.services.map((s) => `${s.service} (${s.image})`).join(", ");
}

/**
 * Re-send an IDENTICAL request body with acknowledge_no_backup: true
 * appended — never re-derive the body from current form state, so a
 * "Create anyway" click can't smuggle in edits the user made after the 409.
 */
export function withBackupAck<T extends object>(body: T): T & { acknowledge_no_backup: true } {
  return { ...body, acknowledge_no_backup: true };
}

/**
 * Classify a failed (non-ok) create response: either the GA.ASW.6
 * requires_ack shape (`backupAck` set, `message` empty) or a normal error
 * message string to surface as-is. A single `resp.json()` read so callers
 * never risk consuming the body twice.
 */
export async function classifyBackupAckResponse(
  resp: Response,
): Promise<{ backupAck: BackupAckDetail | null; message: string }> {
  let data: unknown = null;
  try {
    data = await resp.json();
  } catch {
    return { backupAck: null, message: `HTTP ${resp.status}` };
  }
  const backupAck = parseBackupAckDetail(resp.status, data);
  if (backupAck) return { backupAck, message: "" };
  const detail = (data as { detail?: unknown } | null)?.detail;
  return {
    backupAck: null,
    message: typeof detail === "string" ? detail : `HTTP ${resp.status}`,
  };
}
