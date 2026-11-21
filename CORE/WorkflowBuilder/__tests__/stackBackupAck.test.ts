/**
 * stack-backup-ack (GA.ASW.6b) — pure glue for the "no backup plan" 409.
 */

import {
  classifyBackupAckResponse,
  formatBackupAckServices,
  parseBackupAckDetail,
  withBackupAck,
} from "@/lib/stack-backup-ack";

const jsonResp = (status: number, body: unknown) =>
  ({ status, json: async () => body }) as unknown as Response;

const DETAIL_BODY = {
  detail: {
    requires_ack: true,
    reason:
      "pg (postgres:15) — this stack runs a known database image with no backup posture declared, so its data is not backed up. Retry with acknowledge_no_backup: true to create it anyway.",
    services: [{ service: "pg", image: "postgres:15", engine: "postgres" }],
  },
};

describe("parseBackupAckDetail", () => {
  it("parses the requires_ack 409 shape", () => {
    const parsed = parseBackupAckDetail(409, DETAIL_BODY);
    expect(parsed).toEqual({
      requires_ack: true,
      reason: DETAIL_BODY.detail.reason,
      services: [{ service: "pg", image: "postgres:15", engine: "postgres" }],
    });
  });

  it("returns null for a non-409 status even with a matching body", () => {
    expect(parseBackupAckDetail(422, DETAIL_BODY)).toBeNull();
  });

  it("returns null for a plain-string detail 409 (a different conflict)", () => {
    expect(parseBackupAckDetail(409, { detail: "stack name already exists" })).toBeNull();
  });

  it("returns null when requires_ack is missing or falsy", () => {
    expect(parseBackupAckDetail(409, { detail: { reason: "x", services: [] } })).toBeNull();
    expect(
      parseBackupAckDetail(409, { detail: { requires_ack: false, reason: "x", services: [] } }),
    ).toBeNull();
  });

  it("returns null for a non-object body", () => {
    expect(parseBackupAckDetail(409, null)).toBeNull();
    expect(parseBackupAckDetail(409, "oops")).toBeNull();
  });

  it("falls back to a generic reason and drops malformed service entries", () => {
    const parsed = parseBackupAckDetail(409, {
      detail: {
        requires_ack: true,
        services: [{ service: "pg", image: "postgres:15" }, { service: "no-image" }, "junk"],
      },
    });
    expect(parsed).toEqual({
      requires_ack: true,
      reason: "No backup posture declared for this stack.",
      services: [{ service: "pg", image: "postgres:15" }],
    });
  });
});

describe("formatBackupAckServices", () => {
  it("joins service (image) pairs", () => {
    const parsed = parseBackupAckDetail(409, {
      detail: {
        requires_ack: true,
        reason: "x",
        services: [
          { service: "pg", image: "postgres:15", engine: "postgres" },
          { service: "cache-db", image: "mongo:7", engine: "mongo" },
        ],
      },
    })!;
    expect(formatBackupAckServices(parsed)).toBe("pg (postgres:15), cache-db (mongo:7)");
  });

  it("is empty for no services", () => {
    const parsed = parseBackupAckDetail(409, DETAIL_BODY)!;
    parsed.services = [];
    expect(formatBackupAckServices(parsed)).toBe("");
  });
});

describe("withBackupAck", () => {
  it("appends acknowledge_no_backup: true without mutating the original", () => {
    const body = { name: "mystack", compose: "services: {}" };
    const out = withBackupAck(body);
    expect(out).toEqual({ ...body, acknowledge_no_backup: true });
    expect(body).not.toHaveProperty("acknowledge_no_backup");
  });
});

describe("classifyBackupAckResponse", () => {
  it("classifies a requires_ack 409 as backupAck with no message", async () => {
    const { backupAck, message } = await classifyBackupAckResponse(jsonResp(409, DETAIL_BODY));
    expect(backupAck).toEqual({
      requires_ack: true,
      reason: DETAIL_BODY.detail.reason,
      services: DETAIL_BODY.detail.services,
    });
    expect(message).toBe("");
  });

  it("classifies a string-detail error as a plain message", async () => {
    const { backupAck, message } = await classifyBackupAckResponse(
      jsonResp(422, { detail: "did not validate: bad readiness port" }),
    );
    expect(backupAck).toBeNull();
    expect(message).toBe("did not validate: bad readiness port");
  });

  it("falls back to an HTTP status message on unparsable JSON", async () => {
    const resp = { status: 500, json: async () => { throw new Error("no body"); } } as unknown as Response;
    const { backupAck, message } = await classifyBackupAckResponse(resp);
    expect(backupAck).toBeNull();
    expect(message).toBe("HTTP 500");
  });

  it("falls back to an HTTP status message when detail is absent", async () => {
    const { backupAck, message } = await classifyBackupAckResponse(jsonResp(409, {}));
    expect(backupAck).toBeNull();
    expect(message).toBe("HTTP 409");
  });
});
