/**
 * PKCE on plain-HTTP LAN installs — lib/pkce-crypto.ts + lib/silent-sso.ts.
 *
 * Owner bug: on http://<LAN-IP> the login/silent-SSO path threw
 * `Cannot read properties of undefined (reading 'digest')` — crypto.subtle
 * (and crypto.randomUUID) exist ONLY in secure contexts (HTTPS / localhost).
 * Asserts:
 *   (a) the pure-JS SHA-256 fallback matches node crypto.createHash for
 *       '', 'abc', a 1000-char string and a non-ASCII string;
 *   (b) base64url output is the same 43-char no-padding form Keycloak expects,
 *       and the fallback challenge equals the crypto.subtle challenge;
 *   (c) generateCodeChallenge works with crypto.subtle DELETED from the global;
 *   (d) uuid4() works without crypto.randomUUID (getRandomValues only).
 */

import { sha256Digest, b64urlFromBytes, uuid4 } from "@/lib/pkce-crypto";
import { generateCodeChallenge } from "@/lib/silent-sso";
import nodeCrypto from "node:crypto";

const hexOf = (bytes: Uint8Array) => Buffer.from(bytes).toString("hex");
const nodeSha = (s: string) => nodeCrypto.createHash("sha256").update(s).digest("hex");
const b64urlBuf = (buf: Buffer) =>
  buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

// Emulate an insecure browser context: crypto.subtle / crypto.randomUUID ABSENT
// (Chrome on http://<LAN-IP>), getRandomValues still present.
// jsdom's crypto object properties are non-configurable, so swap the WHOLE
// global `crypto` for a getRandomValues-only stand-in (what Chrome exposes on
// http://<LAN-IP>). pkce-crypto reads globalThis.crypto, and every jest module
// shares the same vm global object, so the swap reaches it.
function makeWebCryptoInsecure(): { restore: () => void } {
  const c = globalThis.crypto;
  if (!c) return { restore: () => {} };
  // jsdom's WebIDL binding checks `this` — bind it to the real Crypto instance.
  const insecure = { getRandomValues: c.getRandomValues.bind(c) };
  try {
    delete globalThis.crypto;
    globalThis.crypto = insecure;
  } catch {
    try {
      Object.defineProperty(globalThis, "crypto", { value: insecure, configurable: true, writable: true });
    } catch {
      throw new Error("cannot stub crypto.subtle in this jsdom");
    }
  }
  if (globalThis.crypto?.subtle) throw new Error("cannot stub crypto.subtle in this jsdom");
  return {
    restore: () => {
      try {
        Object.defineProperty(globalThis, "crypto", { value: c, configurable: true, writable: true });
      } catch {
        globalThis.crypto = c;
      }
    },
  };
}

describe("pkce-crypto sha256 fallback", () => {
  for (const [label, vec] of [
    ["empty string", ""],
    ["abc", "abc"],
    ["1000-char string", "a".repeat(1000)],
    ["non-ASCII string", "héllo — 日本語 🎃"],
  ]) {
    it(`fallback matches node crypto.createHash for ${label}`, async () => {
      const stub = makeWebCryptoInsecure(); // force the pure-JS path
      try {
        const digest = await sha256Digest(vec);
        expect(hexOf(digest)).toBe(nodeSha(vec));
      } finally {
        stub.restore();
      }
    });
  }

  it("base64url output matches node base64url for the digest", async () => {
    const stub = makeWebCryptoInsecure();
    try {
      const digest = await sha256Digest("abc");
      expect(b64urlFromBytes(digest)).toBe(
        b64urlBuf(nodeCrypto.createHash("sha256").update("abc").digest())
      );
    } finally {
      stub.restore();
    }
  });

  it("subtle path and fallback path agree for the same verifier", async () => {
    const verifier = "a".repeat(128); // PKCE verifiers are ASCII
    const native = hexOf(await sha256Digest(verifier));
    const stub = makeWebCryptoInsecure();
    try {
      const fallback = hexOf(await sha256Digest(verifier));
      expect(fallback).toBe(native);
      expect(fallback).toBe(nodeSha(verifier));
    } finally {
      stub.restore();
    }
  });

  it("generateCodeChallenge works with crypto.subtle deleted from the global", async () => {
    const stub = makeWebCryptoInsecure();
    try {
      const verifier = "test_verifier_for_silent_sso";
      const challenge = await generateCodeChallenge(verifier);
      expect(challenge).toMatch(/^[A-Za-z0-9_-]{43}$/);
      expect(challenge).toBe(
        b64urlBuf(nodeCrypto.createHash("sha256").update(verifier).digest())
      );
    } finally {
      stub.restore();
    }
  });

  it("uuid4 works without crypto.randomUUID", () => {
    const stub = makeWebCryptoInsecure();
    try {
      const u = uuid4();
      expect(u).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
      );
    } finally {
      stub.restore();
    }
  });
});
