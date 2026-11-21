/**
 * LDF-3 §5 — Build Queue per-user namespacing. Tim reproduced the leak
 * by logging in as one user, kicking off a node build, then signing in
 * as another user on the same browser and seeing the first user's
 * queue entries. The fix derives a per-user storage suffix from the
 * JWT `sub` so each authenticated user gets their own localStorage
 * bucket.
 */

import {
  deriveBuildQueueUserId,
  buildQueueStorageKey,
  LEGACY_GLOBAL_BUILDS_KEY,
  OLD_SINGLE_BUILD_KEY,
} from "@/lib/build-queue-namespace";

function makeJwt(payload: Record<string, unknown>): string {
  const head = Buffer.from(JSON.stringify({ alg: "HS256" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `${head}.${body}.signature-not-verified`;
}

describe("deriveBuildQueueUserId", () => {
  it("returns 'anon' for null / empty / missing token", () => {
    expect(deriveBuildQueueUserId(null)).toBe("anon");
    expect(deriveBuildQueueUserId(undefined)).toBe("anon");
    expect(deriveBuildQueueUserId("")).toBe("anon");
  });

  it("returns 'anon' for malformed token (no 3 parts)", () => {
    expect(deriveBuildQueueUserId("not-a-jwt")).toBe("anon");
    expect(deriveBuildQueueUserId("only.two")).toBe("anon");
  });

  it("returns 'anon' for token with non-JSON payload", () => {
    expect(deriveBuildQueueUserId("a.bm90LWpzb24.b")).toBe("anon");
  });

  it("uses the `sub` claim when present", () => {
    const tok = makeJwt({ sub: "user-42", email: "admin@example.com" });
    expect(deriveBuildQueueUserId(tok)).toBe("user-42");
  });

  it("falls back to user_id, then email", () => {
    const userIdOnly = makeJwt({ user_id: 99 });
    expect(deriveBuildQueueUserId(userIdOnly)).toBe("99");

    const emailOnly = makeJwt({ email: "alice@a.com" });
    expect(deriveBuildQueueUserId(emailOnly)).toBe("alice@a.com");
  });

  it("returns 'anon' when no identity claim is present", () => {
    expect(deriveBuildQueueUserId(makeJwt({ foo: "bar" }))).toBe("anon");
  });

  it("caps the suffix at 64 chars", () => {
    const longSub = "x".repeat(200);
    const tok = makeJwt({ sub: longSub });
    expect(deriveBuildQueueUserId(tok)).toHaveLength(64);
  });

  it("Tim's regression: A and B on same browser get different suffixes", () => {
    const tokA = makeJwt({ sub: "user-1", email: "alice@a.com" });
    const tokB = makeJwt({ sub: "user-2", email: "bob@a.com" });
    expect(deriveBuildQueueUserId(tokA)).not.toBe(deriveBuildQueueUserId(tokB));
  });

  it("Tim's #10 reproduction: UUID-shaped sub claim from real Keycloak JWTs", () => {
    // Real Keycloak access tokens have UUID-shaped `sub` claims. The
    // base64url payload often needs `=` padding restored before atob
    // can decode it; without the padding fix, both users collapsed to
    // "anon" and shared the same Build Queue storage bucket.
    const tokA = makeJwt({
      sub: "e4f9c343-07ba-4591-bbc4-af2af548a8cc",
      preferred_username: "admin@example.com",
    });
    const tokB = makeJwt({
      sub: "8a1b2c3d-4e5f-6789-abcd-ef0123456789",
      preferred_username: "admin@example.com",
    });
    const a = deriveBuildQueueUserId(tokA);
    const b = deriveBuildQueueUserId(tokB);
    expect(a).toBe("e4f9c343-07ba-4591-bbc4-af2af548a8cc");
    expect(b).toBe("8a1b2c3d-4e5f-6789-abcd-ef0123456789");
    expect(a).not.toBe(b);
    // Neither should be "anon" — that was the bug.
    expect(a).not.toBe("anon");
    expect(b).not.toBe("anon");
  });

  it("payloads of varying lengths all decode (=padding restored)", () => {
    // Cycle through payload sizes that produce 0/1/2/3 padding chars
    // when base64url-encoded. Without our padding restore, some of
    // these would atob-fail and fall back to "anon".
    for (let len = 1; len <= 20; len++) {
      const sub = "x".repeat(len);
      const tok = makeJwt({ sub });
      expect(deriveBuildQueueUserId(tok)).toBe(sub);
    }
  });
});

describe("buildQueueStorageKey", () => {
  it("namespaces by user id", () => {
    expect(buildQueueStorageKey("user-42")).toBe("mod.buildQueue.v1.user.user-42");
  });

  it("differs across users so localStorage entries don't overlap", () => {
    expect(buildQueueStorageKey("alice"))
      .not.toBe(buildQueueStorageKey("bob"));
  });

  it("differs from the legacy global key (so old data is ignored on read)", () => {
    expect(buildQueueStorageKey("alice")).not.toBe(LEGACY_GLOBAL_BUILDS_KEY);
    expect(LEGACY_GLOBAL_BUILDS_KEY).toBe("mod.buildQueue.v1");
  });
});

describe("legacy key constants", () => {
  it("expose the v0 single-slot and the v1 global key for migration", () => {
    expect(OLD_SINGLE_BUILD_KEY).toBe("mod.activeBuild.v1");
    expect(LEGACY_GLOBAL_BUILDS_KEY).toBe("mod.buildQueue.v1");
  });
});
