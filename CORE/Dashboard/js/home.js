// Dashboard/js/home.js

import { renderUsageWidget } from './usage-widget.js';
import { renderQuickStatsWidget } from './quick-stats-widget.js';
import { renderRecentActivityWidget } from './recent-activity-widget.js';
import { renderSystemStatusWidget } from './system-status-widget.js';
import { renderLicenseWidget } from './license-widget.js';

export async function initializeHomePage() {
    // Order: existing widgets first (Usage Overview) so the visible
    // page paints fast, then the home-page-finalize widgets in parallel.
    await renderUsageWidget('home-usage-overview');

    // License overview (Q1-B). Guarded: a license-widget failure must never
    // break the home page. On CE the widget renders the edition badge.
    // Note: try/catch around `await` also catches async rejections, not
    // only synchronous throws.
    try {
        await renderLicenseWidget('home-license-overview');
    } catch (err) {
        console.warn('home license overview failed to render', err);
    }

    // The three new widgets are independent — fire them in parallel; each
    // owns its own loading skeleton and error handling.
    await Promise.all([
        renderQuickStatsWidget('home-quick-stats'),
        renderRecentActivityWidget('home-recent-activity'),
        renderSystemStatusWidget('home-system-status'),
    ]);
}
