"use client";

/**
 * Standalone light/dark toggle for the WorkflowBuilder topbar — sits between
 * the assistant launcher and the notification bell. Matches the Dashboard's
 * #theme-toggle behaviour: the choice writes through to the parent-domain
 * `mod_theme` cookie (via next-themes + theme-cookie-sync.tsx), so the theme
 * stays consistent across the Dashboard / Builder subdomains.
 *
 * Styled to match the bell (relative p-2 ... rounded-lg) so the topbar icons
 * line up. Uses the standard next-themes mounted-guard to avoid a hydration
 * mismatch (server can't know the client's resolved theme).
 */
import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { Sun, Moon } from "lucide-react";

export default function ThemeToggleButton() {
    const { resolvedTheme, setTheme } = useTheme();
    const [mounted, setMounted] = useState(false);
    useEffect(() => { setMounted(true); }, []);
    const isDark = mounted && resolvedTheme === "dark";

    return (
        <button
            type="button"
            onClick={() => setTheme(isDark ? "light" : "dark")}
            title="Toggle light / dark theme"
            aria-label="Toggle theme"
            className="relative p-2 text-gray-600 hover:text-gray-800 hover:bg-gray-100 rounded-lg transition-colors focus:outline-none"
        >
            {/* Before mount, render the moon (matches defaultTheme="dark")
                to avoid a flicker / hydration mismatch. */}
            {isDark ? <Sun className="w-6 h-6" /> : <Moon className="w-6 h-6" />}
        </button>
    );
}
