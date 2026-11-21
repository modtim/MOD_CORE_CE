// Light/dark theme. Dark by default; user opts into light via the
// top-bar toggle.
//
// Persistence is BOTH localStorage (`mod_theme`, host-scoped) and a
// cookie (`mod_theme`) on the parent domain. The cookie is what
// makes the choice carry across subdomains — Dashboard,
// WorkflowBuilder, and mod-docs all live on different subdomains
// (dashboard.*, workflows.*, docs.*) so localStorage alone wouldn't
// sync. The cookie wins on read so a fresh tab on any subdomain
// picks up the latest pick from any other.

const STORAGE_KEY = 'mod_theme';
const COOKIE_KEY = 'mod_theme';

function _parentCookieDomain() {
    // example.com         → .example.com
    // dashboard.example.com   → .example.com
    // localhost / IP                    → null (host-scoped only)
    const parts = location.hostname.split('.');
    if (parts.length < 2 || /^[0-9.]+$/.test(location.hostname)) return null;
    return '.' + parts.slice(1).join('.');
}

function _readThemeCookie() {
    try {
        const m = document.cookie.match(/(?:^|;\s*)mod_theme=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : null;
    } catch {
        return null;
    }
}

function _writeThemeCookie(theme) {
    try {
        const domain = _parentCookieDomain();
        const domainPart = domain ? `; Domain=${domain}` : '';
        const securePart = location.protocol === 'https:' ? '; Secure' : '';
        // 1y; SameSite=Lax so cross-subdomain navigation reads it.
        document.cookie = `${COOKIE_KEY}=${theme}; Path=/; Max-Age=31536000${domainPart}; SameSite=Lax${securePart}`;
    } catch {}
}

function getStoredTheme() {
    // Cookie wins (cross-subdomain truth). localStorage is the
    // legacy fallback for users who set the theme before the
    // cookie path landed.
    const fromCookie = _readThemeCookie();
    if (fromCookie === 'dark' || fromCookie === 'light') return fromCookie;
    try {
        return localStorage.getItem(STORAGE_KEY);
    } catch {
        return null;
    }
}

function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === 'dark') {
        root.classList.add('dark');
    } else {
        root.classList.remove('dark');
    }
    updateToggleIcon(theme);
}

function updateToggleIcon(theme) {
    const sun = document.getElementById('theme-toggle-sun');
    const moon = document.getElementById('theme-toggle-moon');
    if (!sun || !moon) return;
    if (theme === 'dark') {
        sun.classList.remove('hidden');
        moon.classList.add('hidden');
    } else {
        sun.classList.add('hidden');
        moon.classList.remove('hidden');
    }
}

function toggleTheme() {
    // Default is dark — anything that's not the explicit 'light'
    // string (including null / undefined) is treated as dark, so a
    // first-time user clicking the toggle flips dark → light, not
    // dark → dark.
    const next = getStoredTheme() === 'light' ? 'dark' : 'light';
    try {
        localStorage.setItem(STORAGE_KEY, next);
    } catch {}
    _writeThemeCookie(next);
    applyTheme(next);
}

window.toggleTheme = toggleTheme;
window.applyTheme = applyTheme;

applyTheme(getStoredTheme() === 'light' ? 'light' : 'dark');

document.addEventListener('DOMContentLoaded', () => {
    updateToggleIcon(getStoredTheme() === 'light' ? 'light' : 'dark');
});
