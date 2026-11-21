'use client'

import { useEffect } from 'react'
import { useTheme } from 'next-themes'

// Bidirectional half of the cross-subdomain theme sync. The
// pre-hydration script in app/layout.tsx handles the read side
// (cookie → next-themes localStorage); this component handles the
// write side: whenever next-themes resolves to dark or light, we
// persist that choice into the parent-domain `mod_theme` cookie
// so Dashboard and mod-docs pick it up on next navigation.
export function ThemeCookieSync() {
  const { resolvedTheme } = useTheme()

  useEffect(() => {
    if (resolvedTheme !== 'dark' && resolvedTheme !== 'light') return
    try {
      const parts = window.location.hostname.split('.')
      const isIp = /^[0-9.]+$/.test(window.location.hostname)
      const domain = parts.length >= 2 && !isIp
        ? '.' + parts.slice(1).join('.')
        : ''
      const domainPart = domain ? `; Domain=${domain}` : ''
      const securePart = window.location.protocol === 'https:' ? '; Secure' : ''
      document.cookie = `mod_theme=${resolvedTheme}; Path=/; Max-Age=31536000${domainPart}; SameSite=Lax${securePart}`
    } catch {
      // best-effort — cookie is a nice-to-have for cross-subdomain;
      // local localStorage already covers the in-app case.
    }
  }, [resolvedTheme])

  return null
}
