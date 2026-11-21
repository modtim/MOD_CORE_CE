import type { Metadata } from 'next'
import Script from 'next/script'
import { Toaster } from "@/components/ui/toaster"
import { ThemeProvider } from "@/components/theme-provider"
import { ThemeCookieSync } from "@/components/theme-cookie-sync"
import './globals.css'

export const metadata: Metadata = {
  title: 'MOD · Builder',
  description: 'MOD Core — Workflow Builder',
  icons: {
    icon: [
      { url: '/branding/favicons/icon.svg', type: 'image/svg+xml', media: '(prefers-color-scheme: light)' },
      { url: '/branding/favicons/icon-dark.svg', type: 'image/svg+xml', media: '(prefers-color-scheme: dark)' },
      { url: '/branding/favicons/icon.svg', type: 'image/svg+xml' },
    ],
    shortcut: '/branding/favicons/icon.svg',
    apple: '/branding/favicons/icon.svg',
  },
}

// Cross-subdomain theme sync. Reads the `mod_theme` cookie that
// Dashboard / Docs / WorkflowBuilder all share on the parent
// domain, seeds next-themes' localStorage entry, and pre-paints
// the `dark` class on <html> so there's no flash of the wrong
// theme before next-themes hydrates. When no cookie is present
// (first-time visitor, fresh browser), this defaults to dark so
// pre-paint matches the ThemeProvider's defaultTheme="dark" and
// the user doesn't see a flash of light → dark on first paint.
const THEME_COOKIE_BOOTSTRAP = `(function(){try{var m=document.cookie.match(/(?:^|;\\s*)mod_theme=([^;]+)/);var t=m?decodeURIComponent(m[1]):null;var resolved=(t==='dark'||t==='light')?t:'dark';try{localStorage.setItem('theme',resolved);}catch(_){};if(resolved==='dark')document.documentElement.classList.add('dark');else document.documentElement.classList.remove('dark');}catch(e){}})();`

// GT.B1 fix — the customer config used to arrive via a SEPARATE
// /runtime-config.js request (next/script beforeInteractive), which on a
// shared slow box can be deferred and fetch-start while app code is already
// running: any module-scope apiUrl() call that evaluates before the config
// script wins races the "http://api.localhost" fallback (CSP-blocked on the
// stage), silently misdirecting the first fetch of the page. So the config
// is ALSO injected INLINE here (same values docker-entrypoint.sh renders
// into public/runtime-config.js from the same container env — the served
// file stays as a belt-and-suspenders for non-SSR consumers). This classic
// script runs during HTML parse, strictly before any module chunk, so
// window.__MOD_RUNTIME_CONFIG__ is always set before app code can read it.
// (script-src allows 'unsafe-inline' — the theme bootstrap proves it.)
const RUNTIME_CONFIG_BOOTSTRAP = (() => {
  const readEnv = (name: string): string => {
    try {
      const v = process.env[name];
      return typeof v === "string" ? v.trim().replace(/\/+$/, "") : "";
    } catch {
      return "";
    }
  };
  const cfg = {
    apiUrl: readEnv("MOD_API_URL") || readEnv("NEXT_PUBLIC_API_URL"),
    dashboardUrl: readEnv("MOD_DASHBOARD_URL") || readEnv("NEXT_PUBLIC_DASHBOARD_URL"),
  };
  // A statically prerendered page sees the BUILD env (no domain baked since
  // GT.B1): emit nothing then, and never clobber a config already set.
  if (!cfg.apiUrl) return "";
  const json = JSON.stringify(cfg).replace(/</g, "\\u003c");
  return `window.__MOD_RUNTIME_CONFIG__=window.__MOD_RUNTIME_CONFIG__||${json};`;
})();

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* GT.B1: runtime customer config (apiUrl/dashboardUrl) rendered from
            container env by docker-entrypoint.sh; must load before app code.
            Missing in dev/local — the helper falls back to NEXT_PUBLIC_*. */}
        <Script src="/runtime-config.js" strategy="beforeInteractive" />
        {RUNTIME_CONFIG_BOOTSTRAP && (
          <script dangerouslySetInnerHTML={{ __html: RUNTIME_CONFIG_BOOTSTRAP }} />
        )}
        <script dangerouslySetInnerHTML={{ __html: THEME_COOKIE_BOOTSTRAP }} />
        <style>{`
html {
  font-family: 'Manrope', ui-sans-serif, system-ui, -apple-system, sans-serif;
}
        `}</style>
      </head>
      <body>
        {/* R-128 — skip-to-content link. Visually hidden until focused;
            the sr-only + focus-visible classes come from Tailwind (used
            elsewhere in the app, e.g. components/ui/breadcrumb.tsx). */}
        <a
          href="#wb-main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-4 focus:left-4 focus:z-[100] focus:rounded-md focus:bg-slate-900 focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-white focus:outline-none focus:ring-2 focus:ring-white"
        >
          Skip to content
        </a>
        <ThemeProvider attribute="class" defaultTheme="dark" enableSystem={false}>
          <ThemeCookieSync />
          {children}
          <Toaster />
        </ThemeProvider>
      </body>
    </html>
  )
}
