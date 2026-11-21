/** @type {import('tailwindcss').Config} */
//
// MOD Tech Labs design tokens (launch-day-followup-5 §4 —
// platform-theming).
//
// All color tokens are read from CSS custom properties defined in
// `tokens.css` (which is a build-time copy of /assets/branding/
// tokens.css — the single source of truth for brand colors across
// Dashboard, mod-docs, and WorkflowBuilder).
//
// Naming follows shadcn semantic conventions: primary, accent, muted,
// secondary, foreground, background, card, popover, border, ring,
// destructive, success, warning, info, sidebar.* and chart.* — so any
// component pulled from shadcn/ui drops in without re-mapping.
//
// HSL channel format (e.g. `--primary: 232 96% 42%;`) is used in
// tokens.css so opacity utilities like `bg-primary/10` work naturally.
//
// `darkMode: 'class'` is retained so a future dark-mode flip is one
// body class away. Dashboard runs light-mode-only by default per the
// brand guide §2.

const semantic = {
    border:      "hsl(var(--border))",
    input:       "hsl(var(--input))",
    ring:        "hsl(var(--ring))",
    background:  "hsl(var(--background))",
    foreground:  "hsl(var(--foreground))",
    primary: {
        DEFAULT:    "hsl(var(--primary))",
        foreground: "hsl(var(--primary-foreground))",
    },
    accent: {
        DEFAULT:    "hsl(var(--accent))",
        foreground: "hsl(var(--accent-foreground))",
    },
    secondary: {
        DEFAULT:    "hsl(var(--secondary))",
        foreground: "hsl(var(--secondary-foreground))",
    },
    muted: {
        DEFAULT:    "hsl(var(--muted))",
        foreground: "hsl(var(--muted-foreground))",
    },
    card: {
        DEFAULT:    "hsl(var(--card))",
        foreground: "hsl(var(--card-foreground))",
    },
    popover: {
        DEFAULT:    "hsl(var(--popover))",
        foreground: "hsl(var(--popover-foreground))",
    },
    destructive: {
        DEFAULT:    "hsl(var(--destructive))",
        foreground: "hsl(var(--destructive-foreground))",
    },
    success: {
        DEFAULT:    "hsl(var(--success))",
        foreground: "hsl(var(--success-foreground))",
    },
    warning: {
        DEFAULT:    "hsl(var(--warning))",
        foreground: "hsl(var(--warning-foreground))",
    },
    info: {
        DEFAULT:    "hsl(var(--info))",
        foreground: "hsl(var(--info-foreground))",
    },
    sidebar: {
        DEFAULT:               "hsl(var(--sidebar-background))",
        foreground:            "hsl(var(--sidebar-foreground))",
        primary:               "hsl(var(--sidebar-primary))",
        "primary-foreground":  "hsl(var(--sidebar-primary-foreground))",
        accent:                "hsl(var(--sidebar-accent))",
        "accent-foreground":   "hsl(var(--sidebar-accent-foreground))",
        border:                "hsl(var(--sidebar-border))",
        ring:                  "hsl(var(--sidebar-ring))",
    },
    chart: {
        "1": "hsl(var(--chart-1))",
        "2": "hsl(var(--chart-2))",
        "3": "hsl(var(--chart-3))",
        "4": "hsl(var(--chart-4))",
        "5": "hsl(var(--chart-5))",
    },
};

module.exports = {
    darkMode: "class",
    content: [
        "./index.html",
        "./pages/**/*.html",
        "./modals/**/*.html",
        "./js/**/*.js",
    ],
    theme: {
        extend: {
            colors: semantic,
            fontFamily: {
                sans:    ["Manrope", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
                display: ["Manrope", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
                mono:    ["ui-monospace", "Menlo", "Monaco", "Consolas", "monospace"],
            },
            borderRadius: {
                xs:    "var(--radius-xs)",
                sm:    "var(--radius-sm)",
                md:    "var(--radius-md)",
                lg:    "var(--radius-lg)",
                xl:    "var(--radius-xl)",
                "2xl": "var(--radius-2xl)",
            },
            boxShadow: {
                soft:    "var(--shadow-soft)",
                raised:  "var(--shadow-raised)",
                overlay: "var(--shadow-overlay)",
            },
        },
    },
    plugins: [],
};
