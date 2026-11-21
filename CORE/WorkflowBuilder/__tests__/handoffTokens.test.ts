// GSEC R-074 — Dashboard → WB handoff tokens move out of the URL query
// string (where nginx/uvicorn access logs record them) into the URL
// fragment (never sent to the server). Covers the WB receiver's
// parse + strip helpers in lib/handoff-tokens.ts:
//   - fragment tokens read (new senders)
//   - legacy query tokens read (old links, one-release fallback)
//   - both forms stripped via history.replaceState
//   - non-token query params preserved

import { parseHandoffTokens, stripHandoffTokens } from '@/lib/handoff-tokens';

function makeHistory() {
    const calls: Array<Parameters<History['replaceState']>> = [];
    return {
        calls,
        replaceState: (...args: Parameters<History['replaceState']>) => {
            calls.push(args);
        },
    };
}

describe('parseHandoffTokens', () => {
    it('reads tokens from the URL fragment (new handoff form)', () => {
        const loc = { search: '', hash: '#token=ACC&refresh_token=REF' };
        expect(parseHandoffTokens(loc)).toEqual({ accessToken: 'ACC', refreshToken: 'REF' });
    });

    it('reads tokens from the legacy query string (old links still work)', () => {
        const loc = { search: '?token=ACC&refresh_token=REF', hash: '' };
        expect(parseHandoffTokens(loc)).toEqual({ accessToken: 'ACC', refreshToken: 'REF' });
    });

    it('prefers the fragment over a stale query copy', () => {
        const loc = { search: '?token=OLD', hash: '#token=NEW' };
        expect(parseHandoffTokens(loc)).toEqual({ accessToken: 'NEW', refreshToken: null });
    });

    it('handles fragment-only access token and query-only refresh token', () => {
        expect(parseHandoffTokens({ search: '', hash: '#token=A' })).toEqual({
            accessToken: 'A',
            refreshToken: null,
        });
        expect(parseHandoffTokens({ search: '?refresh_token=B', hash: '' })).toEqual({
            accessToken: null,
            refreshToken: 'B',
        });
    });

    it('decodes URL-encoded values', () => {
        const loc = { search: '', hash: '#token=a%2Bb%3D&refresh_token=c%2Fd' };
        expect(parseHandoffTokens(loc)).toEqual({ accessToken: 'a+b=', refreshToken: 'c/d' });
    });

    it('returns nulls when the URL carries no tokens', () => {
        const loc = { search: '?workflow=42', hash: '#something' };
        expect(parseHandoffTokens(loc)).toEqual({ accessToken: null, refreshToken: null });
    });
});

describe('stripHandoffTokens', () => {
    it('strips fragment tokens and drops the empty hash', () => {
        const history = makeHistory();
        const loc = { href: 'http://w.local/?workflow=42#token=A&refresh_token=B', search: '?workflow=42', hash: '#token=A&refresh_token=B' };
        stripHandoffTokens(loc, history);
        const url = new URL(String(history.calls[0][2]));
        expect(url.pathname).toBe('/');
        expect(url.searchParams.get('workflow')).toBe('42');
        expect(url.searchParams.get('token')).toBeNull();
        expect(url.searchParams.get('refresh_token')).toBeNull();
        expect(url.hash).toBe('');
    });

    it('strips legacy query tokens (no fragment involved)', () => {
        const history = makeHistory();
        const loc = { href: 'http://w.local/?workflow=7&token=A&refresh_token=B', search: '?workflow=7&token=A&refresh_token=B', hash: '' };
        stripHandoffTokens(loc, history);
        const url = new URL(String(history.calls[0][2]));
        expect(url.searchParams.get('workflow')).toBe('7');
        expect(url.searchParams.get('token')).toBeNull();
        expect(url.searchParams.get('refresh_token')).toBeNull();
        expect(url.hash).toBe('');
    });

    it('strips BOTH forms at once and preserves non-token params', () => {
        const history = makeHistory();
        const loc = {
            href: 'http://w.local/?node_search=ffmpeg&token=OLD#token=A&refresh_token=B',
            search: '?node_search=ffmpeg&token=OLD',
            hash: '#token=A&refresh_token=B',
        };
        stripHandoffTokens(loc, history);
        const url = new URL(String(history.calls[0][2]));
        expect(url.searchParams.get('node_search')).toBe('ffmpeg');
        expect(url.searchParams.get('token')).toBeNull();
        expect(url.searchParams.get('refresh_token')).toBeNull();
        expect(url.hash).toBe('');
    });

    it('keeps other fragment params and only removes token/refresh_token', () => {
        const history = makeHistory();
        const loc = {
            href: 'http://w.local/?resume=9#token=A&refresh_token=B&section=queue',
            search: '?resume=9',
            hash: '#token=A&refresh_token=B&section=queue',
        };
        stripHandoffTokens(loc, history);
        const url = new URL(String(history.calls[0][2]));
        expect(url.hash).toBe('#section=queue');
        expect(url.searchParams.get('resume')).toBe('9');
    });
});
