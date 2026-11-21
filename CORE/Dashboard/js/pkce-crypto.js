// PKCE crypto helpers that work in INSECURE contexts (plain http on a LAN IP).
//
// BUG (owner, LAN install http://203.0.113.150:3000): the login button threw
// `TypeError: Cannot read properties of undefined (reading 'digest')` — per the
// Web Crypto spec `crypto.subtle` (and `crypto.randomUUID`) exist ONLY in
// secure contexts (HTTPS / localhost), so the S256 challenge died on every
// plain-HTTP LAN install. `crypto.getRandomValues` IS available in insecure
// contexts, so randomness was never the problem — only the SHA-256 digest and
// UUID helpers were.
//
// This module keeps the native path when present and falls back to a small
// self-contained pure-JS SHA-256 (RFC 6234, no npm dependency, no network).
// Base64url output is byte-identical to the old crypto.subtle path, so
// Keycloak verifies the same challenge either way.
//
// Copy (do not cross-import) into WorkflowBuilder/lib/pkce-crypto.ts.

const _K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function _rotr(x, n) { return (x >>> n) | (x << (32 - n)); }

// Pure-JS SHA-256 over a Uint8Array; returns a 32-byte Uint8Array digest.
function _sha256Pure(bytes) {
    const len = bytes.length;
    const blocks = ((len + 8) >> 6) + 1;
    const buf = new Uint8Array(blocks << 6);
    for (let i = 0; i < len; i++) buf[i] = bytes[i];
    buf[len] = 0x80;
    const dv = new DataView(buf.buffer);
    // 64-bit big-endian bit length (JS strings/verifiers are far under 2^32 bits).
    dv.setUint32(buf.length - 8, (len >>> 29) & 0xffffffff);
    dv.setUint32(buf.length - 4, (len << 3) & 0xffffffff);

    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
    let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    const w = new Uint32Array(64);

    for (let off = 0; off < buf.length; off += 64) {
        for (let t = 0; t < 16; t++) w[t] = dv.getUint32(off + (t << 2));
        for (let t = 16; t < 64; t++) {
            const s0 = _rotr(w[t - 15], 7) ^ _rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
            const s1 = _rotr(w[t - 2], 17) ^ _rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
            w[t] = (w[t - 16] + s1 + w[t - 7] + s0) & 0xffffffff;
        }
        let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
        for (let t = 0; t < 64; t++) {
            const S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25);
            const ch = (e & f) ^ (~e & g);
            const t1 = (h + S1 + ch + _K[t] + w[t]) & 0xffffffff;
            const S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22);
            const maj = (a & b) ^ (a & c) ^ (b & c);
            const t2 = (S0 + maj) & 0xffffffff;
            h = g; g = f; f = e;
            e = (d + t1) & 0xffffffff; d = c; c = b; b = a;
            a = (t1 + t2) & 0xffffffff;
        }
        h0 = (h0 + a) & 0xffffffff; h1 = (h1 + b) & 0xffffffff;
        h2 = (h2 + c) & 0xffffffff; h3 = (h3 + d) & 0xffffffff;
        h4 = (h4 + e) & 0xffffffff; h5 = (h5 + f) & 0xffffffff;
        h6 = (h6 + g) & 0xffffffff; h7 = (h7 + h) & 0xffffffff;
    }

    const out = new Uint8Array(32);
    const odv = new DataView(out.buffer);
    [h0, h1, h2, h3, h4, h5, h6, h7].forEach((v, i) => odv.setUint32(i << 2, v));
    return out;
}

// UTF-8 encode WITHOUT TextEncoder: jsdom (and older vm sandboxes) have no
// global TextEncoder, and PKCE verifiers can contain non-ASCII, so encode
// manually per RFC 364 (same bytes TextEncoder produces).
function _utf8Bytes(str) {
    const out = [];
    for (let i = 0; i < str.length; i++) {
        let cp = str.charCodeAt(i);
        if (cp >= 0xd800 && cp <= 0xdbff && i + 1 < str.length
            && str.charCodeAt(i + 1) >= 0xdc00 && str.charCodeAt(i + 1) <= 0xdfff) {
            cp = 0x10000 + ((cp - 0xd800) << 10) + (str.charCodeAt(++i) - 0xdc00);
        }
        if (cp < 0x80) out.push(cp);
        else if (cp < 0x800) out.push(0xc0 | (cp >> 6), 0x80 | (cp & 0x3f));
        else if (cp < 0x10000) out.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
        else out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
    }
    return Uint8Array.from(out);
}

// SHA-256 digest of a string/bytes. Native crypto.subtle when available
// (secure contexts), pure-JS fallback otherwise. Always a Uint8Array.
export async function sha256Digest(input) {
    // Strings are encoded here; Uint8Array input is copied into THIS realm (vm
    // sandboxes / jsdom have their own Uint8Array, so `instanceof` across
    // realms is false — Uint8Array.from works on any iterable).
    let bytes;
    if (typeof input === 'string') {
        bytes = typeof TextEncoder === 'function'
            ? new TextEncoder().encode(input)
            : _utf8Bytes(input);
    } else {
        bytes = Uint8Array.from(input);
    }
    const subtle = globalThis.crypto?.subtle;
    if (subtle) {
        try {
            const digest = await subtle.digest('SHA-256', bytes);
            if (digest) return new Uint8Array(digest);
        } catch (_) { /* non-secure context or unavailable: fall through */ }
    }
    return _sha256Pure(bytes);
}

// Base64url (no padding) — identical output to the old btoa(...).replace path.
export function b64urlFromBytes(bytes) {
    let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// v4 UUID without crypto.randomUUID (secure-context-only). getRandomValues
// exists in every context, so this works on plain-HTTP LAN installs.
export function uuid4() {
    const c = globalThis.crypto;
    if (c && c.randomUUID) {
        try { return c.randomUUID(); } catch (_) { /* non-secure context */ }
    }
    const b = c.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
