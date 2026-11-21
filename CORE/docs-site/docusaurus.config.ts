import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// MOD Platform Documentation

// GT.B1: DASHBOARD_URL is a RUNTIME value. The static build bakes in the
// placeholder token and the container entrypoint (docker-entrypoint.sh)
// rewrites it from $DASHBOARD_URL at container start, so a domain change
// needs only a container recreate, not a rebuild.
//
// The placeholder is a full https:// URL (not a bare token) on purpose:
// Docusaurus runs with onBrokenLinks: 'throw' and validates the SSR'd navbar
// href in the built HTML. A scheme-less token would be read as the internal
// route "/__MOD_DASHBOARD_URL__" and fail the build; a scheme-bearing URL is
// classified as EXTERNAL and is never validated. The entrypoint replaces the
// whole token (scheme included) with the real URL.
const DASHBOARD_URL =
  process.env.DASHBOARD_URL || 'https://__MOD_DASHBOARD_URL__';

const config: Config = {
  title: 'MOD Docs',
  tagline: 'Documentation',
  favicon: 'img/favicon.svg',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Set the production url of your site here
  url: 'https://docs.modtechlabs.com',
  // Set the /<baseUrl>/ pathname under which your site is served
  baseUrl: '/',

  organizationName: 'modtechlabs',
  projectName: 'mod-platform',

  // Fail the build on a broken internal link rather than warning — a 404 in
  // the docs is exactly the "page not found" class of bug we just fixed, so
  // catch it at build time instead of shipping it.
  onBrokenLinks: 'throw',

  // Render ```mermaid fences as diagrams. Lets us draw real node-wiring graphs
  // (Input -> Node -> Output) inline instead of relying on UI screenshots.
  markdown: {
    mermaid: true,
  },

  // Cross-subdomain theme sync. The Dashboard and WorkflowBuilder
  // both write a `mod_theme` cookie on the parent domain; this
  // pre-hydration script reads it and seeds Docusaurus's own
  // localStorage 'theme' key + the `data-theme` attribute on
  // <html> before Docusaurus's React hydrates, so the docs page
  // matches whatever the user picked elsewhere.
  headTags: [
    {
      tagName: 'script',
      attributes: {},
      innerHTML:
        "(function(){try{var m=document.cookie.match(/(?:^|;\\s*)mod_theme=([^;]+)/);" +
        "var t=m?decodeURIComponent(m[1]):null;" +
        "if(t==='dark'||t==='light'){" +
        "try{localStorage.setItem('theme',t);}catch(_){};" +
        "document.documentElement.setAttribute('data-theme',t);" +
        "}}catch(e){}})();",
    },
  ],

  // Mirror direction (Docusaurus's own theme toggle → cookie) lives
  // in src/themeCookieSync.ts so the choice carries back to the
  // Dashboard and WorkflowBuilder on next navigation.
  clientModules: ['./src/themeCookieSync.ts'],

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. For example, if your site is Chinese, you
  // may want to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          routeBasePath: '/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themes: [
    '@docusaurus/theme-mermaid',
    // Offline local search (no Algolia account needed). Indexes the docs at
    // build time and serves a client-side search box in the navbar.
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        indexDocs: true,
        indexBlog: false,
        docsRouteBasePath: '/',
        highlightSearchTermsOnTargetPage: true,
      },
    ],
  ],

  themeConfig: {
    // Replace with your project's social card
    image: 'img/favicon.svg',
    colorMode: {
      // Land in dark by default. Existing visitors keep whatever
      // they picked because Docusaurus persists the choice to
      // localStorage — only fresh sessions / new browsers see the
      // new default. respectPrefersColorScheme intentionally off:
      // OS preference would otherwise override defaultMode and
      // re-introduce the inconsistency with Dashboard /
      // WorkflowBuilder, which now both default to dark.
      defaultMode: 'dark',
      respectPrefersColorScheme: false,
    },
    navbar: {
      // No title text — the wordmark image already shows "MOD". A separate
      // title here visually duplicates the brand and pushes the nav items
      // further to the right.
      title: '',
      logo: {
        alt: 'MOD',
        // Theme-aware: navy logo on the light navbar, white on dark.
        src: '/branding/logos/mod-logo-navy.png',
        srcDark: '/branding/logos/mod-logo-white.png',
        href: DASHBOARD_URL,
        target: '_self',
        // mod-logo is ~2.14:1 (2176×1015); height 48 → width ~103.
        width: 103,
        height: 48,
      },
      items: [
        {
          to: '/getting-started',
          label: 'Get Started',
          position: 'left',
        },
        {
          type: 'docSidebar',
          sidebarId: 'tutorialSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          href: '/api-reference',
          label: 'API Reference',
          position: 'left',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Documentation',
          items: [
            {
              label: 'How MOD works',
              to: '/how-mod-works',
            },
            {
              label: 'Getting Started',
              to: '/getting-started',
            },
            {
              label: 'API Reference',
              to: '/api-reference',
            },
            {
              label: 'Worker Setup',
              to: '/worker-setup',
            },
          ],
        },
        {
          title: 'Support',
          items: [
            {
              label: 'Troubleshooting',
              to: '/troubleshooting',
            },
          ],
        },
        {
          title: 'Legal',
          items: [
            {
              label: 'Privacy Policy',
              to: '/legal/privacy-policy',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} MOD Tech Labs. All rights reserved.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
