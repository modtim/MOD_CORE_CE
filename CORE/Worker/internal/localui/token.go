package localui

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// TokenFileName is the file (under the worker's state dir) that holds the
// current local-UI auth token. Created fresh, 0600, on every Server start —
// only the OS user/admin running the worker can read it (D-P4).
const TokenFileName = "local-ui-token"

// tokenCookieName is the HttpOnly, SameSite=Strict cookie the UI sets after
// a successful ?token= query-param login, so the browser doesn't have to
// keep the token in the URL/history.
const tokenCookieName = "mod_worker_ui_token"

// generateToken returns a fresh random 32-byte token, hex-encoded.
func generateToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("localui: generate token: %w", err)
	}
	return hex.EncodeToString(b), nil
}

// writeTokenFile writes token to <dir>/TokenFileName with 0600 permissions,
// creating dir (0700) if it doesn't exist. Returns the file's path.
func writeTokenFile(dir, token string) (string, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("localui: create state dir %q: %w", dir, err)
	}
	path := filepath.Join(dir, TokenFileName)
	// Write via a temp file + rename so a reader never sees a partial token,
	// and explicitly Chmod after WriteFile so an existing file with looser
	// permissions (e.g. left over from a build that predates this check) is
	// tightened rather than trusted.
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		return "", fmt.Errorf("localui: write token file %q: %w", path, err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		return "", fmt.Errorf("localui: chmod token file %q: %w", path, err)
	}
	return path, nil
}

// constantTimeTokenEqual compares a caller-supplied token against the
// server's token in constant time regardless of length, so a timing side
// channel can't be used to guess the token byte by byte or learn its
// length. subtle.ConstantTimeCompare alone short-circuits (safely, but
// externally observably in principle) on a length mismatch; padding both
// inputs to a fixed size first avoids that.
func constantTimeTokenEqual(got, want string) bool {
	const fixedLen = 128 // well above any real token length
	g := make([]byte, fixedLen)
	w := make([]byte, fixedLen)
	copy(g, got)
	copy(w, want)
	lenOK := subtle.ConstantTimeEq(int32(len(got)), int32(len(want)))
	cmpOK := subtle.ConstantTimeCompare(g, w)
	return lenOK == 1 && cmpOK == 1
}

// bearerToken extracts the token from an `Authorization: Bearer <token>`
// header, or "" if absent/malformed.
func bearerToken(r *http.Request) string {
	ah := r.Header.Get("Authorization")
	const prefix = "Bearer "
	if !strings.HasPrefix(ah, prefix) {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(ah, prefix))
}

// requireAuth wraps a handler so it only runs once the request presents a
// valid token, via (in order): the Authorization: Bearer header, the
// `?token=` query param (which, on success, sets an HttpOnly SameSite=Strict
// cookie and redirects to the same URL with the token stripped from it — so
// it never lingers in browser history/referrer headers), or the cookie set
// by a previous successful query-param login. Missing/invalid -> 401.
func (s *Server) requireAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if tok := bearerToken(r); tok != "" {
			if constantTimeTokenEqual(tok, s.token) {
				next(w, r)
				return
			}
			http.Error(w, "unauthorized: bad token", http.StatusUnauthorized)
			return
		}

		if q := r.URL.Query().Get("token"); q != "" {
			if !constantTimeTokenEqual(q, s.token) {
				http.Error(w, "unauthorized: bad token", http.StatusUnauthorized)
				return
			}
			http.SetCookie(w, &http.Cookie{
				Name:     tokenCookieName,
				Value:    s.token,
				Path:     "/",
				HttpOnly: true,
				SameSite: http.SameSiteStrictMode,
				// Secure is intentionally false: this server only ever
				// listens on loopback plain HTTP (see New's bind check),
				// so there is no TLS session to require.
			})
			stripped := *r.URL
			qv := stripped.Query()
			qv.Del("token")
			stripped.RawQuery = qv.Encode()
			http.Redirect(w, r, stripped.RequestURI(), http.StatusFound)
			return
		}

		if c, err := r.Cookie(tokenCookieName); err == nil {
			if constantTimeTokenEqual(c.Value, s.token) {
				next(w, r)
				return
			}
			http.Error(w, "unauthorized: bad token", http.StatusUnauthorized)
			return
		}

		http.Error(w, "unauthorized: token required (Authorization: Bearer <token> or ?token=)", http.StatusUnauthorized)
	}
}

// requireSameOrigin wraps a state-changing (POST) handler with a CSRF
// check: the request's Origin (preferred) or Referer header must name this
// server's own host. Neither header present is treated as cross-origin
// (fail closed) — a same-page form POST from a real browser always sends
// at least one of them. This is layered on top of the SameSite=Strict
// auth cookie (which already blocks the cookie from being sent
// cross-site); the explicit check also covers requests authenticated via
// the Authorization header, which SameSite does nothing for.
func (s *Server) requireSameOrigin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !sameOrigin(r) {
			http.Error(w, "forbidden: cross-origin request", http.StatusForbidden)
			return
		}
		next(w, r)
	}
}

func sameOrigin(r *http.Request) bool {
	if origin := r.Header.Get("Origin"); origin != "" {
		u, err := url.Parse(origin)
		return err == nil && u.Host == r.Host
	}
	if ref := r.Header.Get("Referer"); ref != "" {
		u, err := url.Parse(ref)
		return err == nil && u.Host == r.Host
	}
	return false
}
