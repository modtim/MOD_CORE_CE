import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

// Manually-grouped sidebar so newcomers aren't faced with a flat list of 20+
// pages. Order within each category is the order below (not sidebar_position).

const sidebars: SidebarsConfig = {
  tutorialSidebar: [
    {
      type: 'category',
      label: 'Front Porch',
      items: ['how-mod-works', 'getting-started', 'first-workflow', 'bind-a-model-to-the-wizard', 'lan-access'],
    },

    {
      type: 'category',
      label: 'Travel Guide',
      items: [
          'workflow-builder',
          'node-library',
          'triggers',
          'account-and-teams',
          'analytics',
          'byo-storage-move-out',
      ]
    },

    {
      type: 'category',
      label: 'Dinner Bell',
      items: [
          'recipe-batch',
          'recipe-video',
      ]
    },

    {
      type: 'category',
      label: 'Royal Archives',
      items: ['api-reference', 'features', 'glossary', 'product-names', 'troubleshooting'],
    },

    {
      type: 'category',
      label: 'Homestead',
      items: ['worker-setup', 'windows-worker', 'macos-worker', 'ce-k3s-setup', 'email-smtp-providers'],
    },

    {
      type: 'category',
      label: 'Engine Room',
      collapsed: true,
      items: [
          'runbooks/api-restart-and-drain',
          'runbooks/worker-enrol-and-retire',
          'runbooks/stack-deploy-and-recover',
          'runbooks/certificate-renewal',
          'runbooks/ce-backup-and-restore',
          'runbooks/database-pooler-dns-recovery',
          'runbooks/container-log-rotation',
          'runbooks/monthly-patch-release',
          'runbooks/incident-first-steps',
          'runbooks/build-your-own-node',
          'runbooks/uninstall-ce',
      ]
    },

    {
      type: 'category',
      label: 'Security setup',
      collapsed: true,
      items: [
          'security/mfa-reset-and-security-contact',
          'security/identity-and-sessions',
          'security/tls-at-install',
          'security/incident-response',
          'security/accessibility-report',
          'security/upgrade-and-rollback',
          'security/default-install-privileges',
      ]
    },

    {
      type: 'category',
      label: 'Book of Laws',
      collapsed: true,
      items: [
          'legal/privacy-policy',
          'legal/cookies-and-storage',
      ]
    },
  ],
};

export default sidebars;