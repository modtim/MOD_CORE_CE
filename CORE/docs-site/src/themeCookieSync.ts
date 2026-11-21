// Cross-subdomain theme sync — write side.
//
// Docusaurus owns its own theme toggle and writes the result to
// `localStorage['theme']` + `<html data-theme="…">`. The Dashboard
// and WorkflowBuilder live on different subdomains and don't share
// localStorage with us, so we mirror Docusaurus's choice into a
// cookie scoped to the parent domain (`mod_theme`). All three apps
// read that cookie at page load (see docusaurus.config.ts headTags
// for the docs read-side, Dashboard's theme.js, and WorkflowBuilder's
// app/layout.tsx for theirs).
//
// Watching `data-theme` mutations on `<html>` is the cheapest way to
// catch every theme change without hooking into Docusaurus's React
// internals — both initial paint and the toggle button update the
// attribute, so the observer covers both.

if (typeof document !== 'undefined') {
  const writeCookie = (theme: string) => {
    try {
      const isIp = /^[0-9.]+$/.test(window.location.hostname);
      const parts = window.location.hostname.split('.');
      const domain = parts.length >= 2 && !isIp ? '.' + parts.slice(1).join('.') : '';
      const domainPart = domain ? `; Domain=${domain}` : '';
      const securePart = window.location.protocol === 'https:' ? '; Secure' : '';
      document.cookie =
        `mod_theme=${theme}; Path=/; Max-Age=31536000${domainPart}; SameSite=Lax${securePart}`;
    } catch {
      // best-effort: cookie persistence is a nice-to-have for
      // cross-subdomain; in-app theme still works without it.
    }
  };

  const sync = () => {
    const theme = document.documentElement.getAttribute('data-theme');
    if (theme === 'dark' || theme === 'light') writeCookie(theme);
  };

  // Initial — capture whatever the pre-hydration script + Docusaurus
  // settled on so the cookie reflects the live state even if the user
  // hasn't toggled yet on this subdomain.
  sync();

  const observer = new MutationObserver(sync);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });
}

export {};
