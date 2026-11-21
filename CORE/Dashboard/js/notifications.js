// Dashboard/js/notifications.js

let notificationRefreshInterval = null;
let isNotificationPanelOpen = false;

/**
 * Initialize the notification system
 */
export function initializeNotifications() {
    // Set up event listeners
    const bellButton = document.getElementById('notification-bell');
    const panel = document.getElementById('notification-panel');
    const markAllReadBtn = document.getElementById('mark-all-read-btn');

    if (!bellButton || !panel) {
        console.error('Notification elements not found in DOM');
        return;
    }

    // Toggle notification panel
    bellButton.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleNotificationPanel();
    });

    // Mark all as read
    markAllReadBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await markAllNotificationsRead();
    });

    // Bulk archive — archives everything matching the current filter.
    const archiveAllBtn = document.getElementById('archive-all-btn');
    if (archiveAllBtn) {
        archiveAllBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await archiveAllNotifications();
        });
    }

    // Bulk delete — destructive; type "delete" to confirm.
    const deleteAllBtn = document.getElementById('delete-all-btn');
    if (deleteAllBtn) {
        deleteAllBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await deleteAllNotifications();
        });
    }

    // Close panel when clicking outside
    document.addEventListener('click', (e) => {
        if (isNotificationPanelOpen &&
            !panel.contains(e.target) &&
            !bellButton.contains(e.target)) {
            closeNotificationPanel();
        }
    });

    // Filter chips (P8f) — persist selection, reload list on change
    document.querySelectorAll('.notif-chip').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            setFilter(btn.dataset.filter);
        });
    });
    updateFilterChipState();

    // Initial load
    loadNotifications();
    updateUnreadCount();

    // Auto-refresh every 30 seconds (safety net; SSE below pushes
    // instant updates on top).
    notificationRefreshInterval = setInterval(() => {
        updateUnreadCount();
        if (isNotificationPanelOpen) {
            loadNotifications();
        }
    }, 30000);

    // SSE push (P8e). Layer realtime on top of the 30s poll. WS5.34b:
    // auth is a short-lived, single-use `?ticket=` (minted via
    // POST /stream-tickets) instead of the full access token — EventSource
    // still can't set headers, but a ticket means nothing outside this
    // process, so nothing sensitive lands in proxy access logs.
    notificationStreamClosed = false;
    connectNotificationStream();
}

let notificationEventSource = null;
let notificationStreamRetryTimer = null;
let notificationStreamBackoffMs = 2000;
const NOTIFICATION_STREAM_MAX_BACKOFF_MS = 30000;
let notificationStreamClosed = false;

/** POST /stream-tickets {scope: "notifications"} -> raw ticket, or null. */
async function mintNotificationStreamTicket() {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        if (!token) return null;
        const resp = await fetch(`${apiUrl}/stream-tickets`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ scope: 'notifications' }),
        });
        if (!resp.ok) return null;
        const data = await resp.json();
        return typeof data.ticket === 'string' ? data.ticket : null;
    } catch (e) {
        console.debug('Notification stream ticket mint failed (non-fatal):', e);
        return null;
    }
}

function scheduleNotificationStreamReconnect() {
    if (notificationStreamClosed) return;
    if (notificationStreamRetryTimer) clearTimeout(notificationStreamRetryTimer);
    notificationStreamRetryTimer = setTimeout(connectNotificationStream, notificationStreamBackoffMs);
    notificationStreamBackoffMs = Math.min(notificationStreamBackoffMs * 2, NOTIFICATION_STREAM_MAX_BACKOFF_MS);
}

/**
 * Mint a fresh ticket and open the SSE connection. Reconnects are done
 * manually here (never via the browser's native EventSource auto-retry —
 * a ticket is single-use, so replaying the same URL would just 401 in a
 * tight loop) on an exponential backoff capped at 30s. The 30s poll
 * (notificationRefreshInterval) is the floor regardless of SSE state.
 */
async function connectNotificationStream() {
    if (notificationStreamClosed || typeof EventSource === 'undefined') return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const ticket = await mintNotificationStreamTicket();
    if (notificationStreamClosed) return;
    if (!ticket) {
        scheduleNotificationStreamReconnect();
        return;
    }
    try {
        const es = new EventSource(`${apiUrl}/notifications/stream?ticket=${encodeURIComponent(ticket)}`);
        es.addEventListener('open', () => { notificationStreamBackoffMs = 2000; });
        es.addEventListener('message', () => {
            updateUnreadCount();
            if (isNotificationPanelOpen) loadNotifications();
        });
        es.addEventListener('error', () => {
            try { es.close(); } catch (e) { /* ignore */ }
            if (notificationEventSource === es) notificationEventSource = null;
            scheduleNotificationStreamReconnect();
        });
        notificationEventSource = es;
    } catch (e) {
        console.debug('Notification SSE init failed (non-fatal):', e);
        scheduleNotificationStreamReconnect();
    }
}

/**
 * Toggle the notification panel open/closed
 */
function toggleNotificationPanel() {
    const panel = document.getElementById('notification-panel');

    if (isNotificationPanelOpen) {
        closeNotificationPanel();
    } else {
        panel.classList.remove('hidden');
        isNotificationPanelOpen = true;
        loadNotifications(); // Refresh when opening
    }
}

/**
 * Close the notification panel
 */
function closeNotificationPanel() {
    const panel = document.getElementById('notification-panel');
    panel.classList.add('hidden');
    isNotificationPanelOpen = false;
}

/**
 * Update the unread notification count badge
 */
async function updateUnreadCount() {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');

        const response = await fetch(`${apiUrl}/notifications/unread-count`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            console.error('Failed to fetch unread count:', response.status);
            return;
        }

        const data = await response.json();
        const badge = document.getElementById('notification-badge');

        if (data.unread_count > 0) {
            badge.textContent = data.unread_count > 99 ? '99+' : data.unread_count;
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    } catch (error) {
        console.error('Error updating unread count:', error);
    }
}

/**
 * Load and display notifications
 */
const FILTER_STORAGE_KEY = 'mod.dashNotifFilter.v1';

function currentFilter() {
    let stored;
    try {
        stored = localStorage.getItem(FILTER_STORAGE_KEY) || 'all';
    } catch {
        return 'all';
    }
    if (stored === 'all') return 'all';
    const chips = Array.from(document.querySelectorAll('.notif-chip'));
    // No chips rendered yet: cannot judge the stored value, keep it as-is.
    if (chips.length === 0) return stored;
    // Edition guard: a stored value with no rendered chip (CE has no
    // 'billing' chip) would hide every notification with no chip to clear it.
    if (chips.some(btn => btn.dataset.filter === stored)) return stored;
    try { localStorage.removeItem(FILTER_STORAGE_KEY); } catch { /* ignore */ }
    return 'all';
}

function setFilter(filter) {
    try { localStorage.setItem(FILTER_STORAGE_KEY, filter); } catch { /* ignore */ }
    updateFilterChipState();
    loadNotifications();
}

function updateFilterChipState() {
    const active = currentFilter();
    document.querySelectorAll('.notif-chip').forEach(btn => {
        const on = btn.dataset.filter === active;
        btn.classList.toggle('bg-primary', on);
        btn.classList.toggle('text-white', on);
        btn.classList.toggle('border-primary', on);
        btn.classList.toggle('bg-white', !on);
        btn.classList.toggle('text-gray-600', !on);
        btn.classList.toggle('border-gray-200', !on);
    });
}

async function loadNotifications() {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const filter = currentFilter();

        const params = new URLSearchParams({ limit: '30' });
        if (filter === 'unread')    params.set('unread_only', 'true');
        if (filter === 'workflows') params.set('category', 'workflows');
        if (filter === 'nodes')     params.set('category', 'nodes');
        if (filter === 'billing')   params.set('category', 'billing');
        if (filter === 'archived')  params.set('include_archived', 'true');

        const response = await fetch(`${apiUrl}/notifications/list?${params}`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            console.error('Failed to fetch notifications:', response.status);
            return;
        }

        const data = await response.json();
        let list = data.notifications || [];
        // Archived chip means archived-only; server returns both when
        // include_archived=true so we filter client-side.
        if (filter === 'archived') list = list.filter(n => n.is_archived);
        displayNotifications(list);
    } catch (error) {
        console.error('Error loading notifications:', error);
        showNotificationError();
    }
}

/**
 * Cross-app notification routing (issue #22). Compute the target URL
 * for a clicked notification. Returns null when the notification
 * has no actionable target — caller should NOT make the row
 * clickable in that case.
 *
 * Mirror of the routing logic in
 * CORE/WorkflowBuilder/components/notifications-bell.tsx so the two
 * surfaces don't diverge on what `notification_type` means. Adding a
 * new type means updating both files.
 */
function routeNotification(notification) {
    if (!notification) return null;
    const type = (notification.notification_type || '').toLowerCase();
    const linkUrl = notification.link_url || '';
    const wfUrl = window.APP_CONFIG?.WORKFLOWS_URL || '';

    // Node-build events route to the WorkflowBuilder. Even though
    // link_url today already encodes a resume= query, we re-anchor
    // to WORKFLOWS_URL here so a stage Dashboard pointing at a
    // tenant-scoped link doesn't follow link_url to a wrong domain.
    if (type.startsWith('node_generation_complete') ||
        type.startsWith('node_artifacts_ready') ||
        type.startsWith('node_build_complete') ||
        type.startsWith('node_')) {
        if (!wfUrl) return null;
        // Extract the resume taskId from link_url if present;
        // otherwise route to the WorkflowBuilder root and let the
        // app's bell + queue surfaces pick up from notifications.
        const m = linkUrl.match(/resume=(\d+)/);
        if (m) {
            return { url: `${wfUrl}/?resume=${m[1]}` };
        }
        return { url: wfUrl };
    }

    // Workflow-* events stay in the Dashboard (the workflows page).
    if (type.startsWith('workflow_')) {
        // Hash-routed Dashboard nav — caller will set window.location.hash.
        return { url: linkUrl && linkUrl.startsWith('#') ? linkUrl : '#workflows' };
    }

    // Billing / payment events route to the billing page.
    if (type.startsWith('payment_') || type.startsWith('invoice_')) {
        return { url: '#billing' };
    }

    // conversational-project-builder §6.1 — a project checkpoint waiting for
    // the user carries link_url `#project/<session id>`; follow it to the
    // conversational project view (hash nav on whichever surface hosts it).
    if (type.startsWith('project_')) {
        return { url: linkUrl && linkUrl.startsWith('#') ? linkUrl : '#projects' };
    }

    // Last-ditch: if link_url is set, follow it directly.
    if (linkUrl) {
        return { url: linkUrl };
    }
    return null;
}

/**
 * Click handler for a notification's message body. Marks the row read
 * (if unread), then navigates to the target the routing function
 * computes. No-ops with a console warn for un-routable notifications.
 */
async function handleNotificationClick(notification) {
    const target = routeNotification(notification);
    // Mark-read fires regardless — the user clicked, they've "seen" it.
    if (!notification.is_read) {
        await markNotificationRead(notification.id);
    }
    if (!target) {
        console.warn('Dashboard notification: no route for', notification.notification_type, notification.link_url);
        return;
    }
    if (target.url.startsWith('#')) {
        window.location.hash = target.url.slice(1);
    } else {
        // Cross-app navigation. Use full nav (not window.open) so
        // sessionStorage carries through and the user lands in the
        // same browsing context they were in.
        window.location.href = target.url;
    }
}

// Type → icon SVG + color class for left-edge visual. P8f.
function typeIconSvg(notificationType) {
    const t = (notificationType || '').toLowerCase();
    // Each SVG is a lucide-style 16px icon. Color via stroke.
    if (t.startsWith('workflow_complete')) {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#059669" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`;
    }
    if (t.startsWith('workflow_failed')) {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" x2="12" y1="9" y2="13"/><line x1="12" x2="12.01" y1="17" y2="17"/></svg>`;
    }
    if (t.startsWith('node_generation_failed') || t.startsWith('node_') && t.includes('fail')) {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>`;
    }
    if (t.startsWith('node_')) {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4f46e5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>`;
    }
    if (t.startsWith('payment_') || t.startsWith('invoice_')) {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#d97706" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><line x1="12" x2="12" y1="2" y2="22"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>`;
    }
    // conversational-project-builder §6.1 — project checkpoint "test this"
    // hand-off: a clipboard-check glyph (sky), distinct from the generic bell.
    if (t.startsWith('project_')) {
        return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#0284c7" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m9 4 1 2h4l1-2"/></svg>`;
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#6b7280" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="flex-shrink-0 mt-0.5"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>`;
}

/**
 * Display notifications in the panel
 */
function displayNotifications(notifications) {
    const listContainer = document.getElementById('notifications-list');
    const visibleCount = notifications ? notifications.length : 0;
    updateBulkActionButtonsState(visibleCount);

    if (!notifications || notifications.length === 0) {
        listContainer.innerHTML = `
            <div class="p-8 text-center text-gray-500">
                <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-12 h-12 mx-auto mb-2 text-gray-400">
                    <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/>
                    <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>
                </svg>
                <p class="text-sm">No notifications</p>
            </div>
        `;
        return;
    }

    // Build notification HTML — P8f: type icon at left edge, archive
    // action (soft hide) alongside delete (hard remove). Archive
    // sits left of delete so the destructive one stays at the edge.
    // Issue #22: the message body is now a clickable button that
    // routes to the appropriate target (WorkflowBuilder for node
    // events, hash-routed pages for workflows / billing). Rows with
    // no routable target render the message as plain text.
    const notificationHTML = notifications.map(notification => {
        const isUnread = !notification.is_read;
        const bgClass = isUnread ? 'bg-primary/10' : 'bg-white';
        const timeAgo = formatTimeAgo(notification.created_at);
        const iconSvg = typeIconSvg(notification.notification_type);
        const route = routeNotification(notification);
        const messageClickableClass = route ? 'cursor-pointer hover:underline underline-offset-2' : '';
        const messageRole = route ? 'role="button" tabindex="0"' : '';
        const messageDataAttr = route ? `data-notif-click="${notification.id}"` : '';
        return `
            <div class="notification-item ${bgClass} border-b border-gray-200 p-4 hover:bg-gray-50 transition-colors" data-notification-id="${notification.id}">
                <div class="flex items-start gap-3">
                    ${iconSvg}
                    <div class="flex-1 min-w-0">
                        <p class="text-sm text-gray-800 ${isUnread ? 'font-medium' : ''} ${messageClickableClass}" ${messageRole} ${messageDataAttr}>${escapeHtml(notification.message)}</p>
                        <p class="text-xs text-gray-500 mt-1" title="${escapeHtml(notification.created_at || '')}">${timeAgo}</p>
                    </div>
                    <div class="flex items-center gap-1 flex-shrink-0">
                        ${isUnread ? `
                            <button class="mark-read-btn p-1 text-gray-400 hover:text-primary/80 transition-colors"
                                    data-notification-id="${notification.id}"
                                    title="Mark as read">
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <polyline points="20 6 9 17 4 12"></polyline>
                                </svg>
                            </button>
                        ` : ''}
                        ${!notification.is_archived ? `
                            <button class="archive-notification-btn p-1 text-gray-400 hover:text-gray-700 transition-colors"
                                    data-notification-id="${notification.id}"
                                    title="Archive (soft-hide)">
                                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>
                                </svg>
                            </button>
                        ` : ''}
                        <button class="delete-notification-btn p-1 text-gray-400 hover:text-red-600 transition-colors"
                                data-notification-id="${notification.id}"
                                title="Delete">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="18" y1="6" x2="6" y2="18"></line>
                                <line x1="6" y1="6" x2="18" y2="18"></line>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    listContainer.innerHTML = notificationHTML;

    // Attach event listeners to action buttons
    attachNotificationEventListeners();

    // Issue #22 — clickable message body. Closure over the rendered
    // notifications list so each click handler has the right row.
    const byId = new Map(notifications.map(n => [String(n.id), n]));
    document.querySelectorAll('[data-notif-click]').forEach(el => {
        el.addEventListener('click', async (e) => {
            e.stopPropagation();
            const id = el.getAttribute('data-notif-click');
            const n = byId.get(String(id));
            if (n) await handleNotificationClick(n);
        });
        el.addEventListener('keydown', async (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                e.stopPropagation();
                const id = el.getAttribute('data-notif-click');
                const n = byId.get(String(id));
                if (n) await handleNotificationClick(n);
            }
        });
    });
}

/**
 * Attach event listeners to notification action buttons
 */
function attachNotificationEventListeners() {
    // Mark as read buttons
    document.querySelectorAll('.mark-read-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const notificationId = btn.dataset.notificationId;
            await markNotificationRead(notificationId);
        });
    });

    // Archive buttons (P8f — soft hide)
    document.querySelectorAll('.archive-notification-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const notificationId = btn.dataset.notificationId;
            await archiveNotification(notificationId);
        });
    });

    // Delete buttons
    document.querySelectorAll('.delete-notification-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const notificationId = btn.dataset.notificationId;
            await deleteNotification(notificationId);
        });
    });
}

async function archiveNotification(notificationId) {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const response = await fetch(`${apiUrl}/notifications/${notificationId}/archive`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
        });
        if (!response.ok) {
            console.error('Archive notification failed:', response.status);
            return;
        }
        await loadNotifications();
        await updateUnreadCount();
    } catch (e) {
        console.error('Error archiving notification:', e);
    }
}

/**
 * Mark a single notification as read
 */
async function markNotificationRead(notificationId) {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');

        const response = await fetch(`${apiUrl}/notifications/${notificationId}/mark-read`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (response.ok) {
            // Refresh notifications and unread count
            await loadNotifications();
            await updateUnreadCount();
        } else {
            console.error('Failed to mark notification as read:', response.status);
        }
    } catch (error) {
        console.error('Error marking notification as read:', error);
    }
}

/**
 * Build URLSearchParams matching the current filter chip — same shape
 * the loadNotifications() request uses, so bulk endpoints scope to
 * exactly what the user is looking at.
 */
function bulkFilterParams() {
    const filter = currentFilter();
    const params = new URLSearchParams();
    if (filter === 'unread')    params.set('unread_only', 'true');
    if (filter === 'workflows') params.set('category', 'workflows');
    if (filter === 'nodes')     params.set('category', 'nodes');
    if (filter === 'billing')   params.set('category', 'billing');
    if (filter === 'archived')  params.set('include_archived', 'true');
    return params;
}

/**
 * Archive every notification matching the current filter.
 */
async function archiveAllNotifications() {
    const filter = currentFilter();
    const label = filter === 'all' ? 'all notifications' : `the ${filter} notifications`;
    let confirmed;
    try {
        const { showConfirm } = await import('./ui.js');
        confirmed = await showConfirm(
            `Archive ${label}? They'll move to the Archived filter and you can still find them later.`,
            'Archive all notifications'
        );
    } catch {
        confirmed = window.confirm(`Archive ${label}?`);
    }
    if (!confirmed) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const params = bulkFilterParams();
        const url = `${apiUrl}/notifications/archive-all${params.toString() ? '?' + params : ''}`;
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (response.ok) {
            await loadNotifications();
            await updateUnreadCount();
        } else {
            console.error('archive-all failed:', response.status);
        }
    } catch (e) {
        console.error('archive-all error:', e);
    }
}

/**
 * Hard-delete every notification matching the current filter. Type
 * "delete" in the prompt to confirm — extra-destructive, no undo.
 */
async function deleteAllNotifications() {
    const filter = currentFilter();
    const label = filter === 'all' ? 'ALL notifications' : `all ${filter} notifications`;
    const typed = window.prompt(
        `This will permanently delete ${label}. This cannot be undone.\n\nType "delete" to confirm.`,
        ''
    );
    if (typed === null) return;
    if (typed.trim().toLowerCase() !== 'delete') {
        window.alert('Cancelled — you didn\'t type "delete".');
        return;
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const params = bulkFilterParams();
        const url = `${apiUrl}/notifications/delete-all${params.toString() ? '?' + params : ''}`;
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (response.ok) {
            await loadNotifications();
            await updateUnreadCount();
        } else {
            console.error('delete-all failed:', response.status);
        }
    } catch (e) {
        console.error('delete-all error:', e);
    }
}

/**
 * Enable or disable the bulk action buttons based on whether the
 * currently displayed filter has any matching items.
 */
function updateBulkActionButtonsState(visibleCount) {
    const archiveBtn = document.getElementById('archive-all-btn');
    const deleteBtn = document.getElementById('delete-all-btn');
    const empty = !visibleCount || visibleCount <= 0;
    if (archiveBtn) archiveBtn.disabled = empty;
    if (deleteBtn) deleteBtn.disabled = empty;
}

/**
 * Mark all notifications as read
 */
async function markAllNotificationsRead() {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');

        const response = await fetch(`${apiUrl}/notifications/mark-all-read`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (response.ok) {
            // Refresh notifications and unread count
            await loadNotifications();
            await updateUnreadCount();
        } else {
            console.error('Failed to mark all notifications as read:', response.status);
        }
    } catch (error) {
        console.error('Error marking all notifications as read:', error);
    }
}

/**
 * Delete a notification
 */
async function deleteNotification(notificationId) {
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');

        const response = await fetch(`${apiUrl}/notifications/${notificationId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (response.ok) {
            // Refresh notifications and unread count
            await loadNotifications();
            await updateUnreadCount();
        } else {
            console.error('Failed to delete notification:', response.status);
        }
    } catch (error) {
        console.error('Error deleting notification:', error);
    }
}

/**
 * Show error message in notification panel
 */
function showNotificationError() {
    const listContainer = document.getElementById('notifications-list');
    listContainer.innerHTML = `
        <div class="p-8 text-center text-red-500">
            <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-12 h-12 mx-auto mb-2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <p class="text-sm">Failed to load notifications</p>
        </div>
    `;
}

/**
 * Format timestamp as relative time (e.g., "2 hours ago")
 */
function formatTimeAgo(timestamp) {
    const now = new Date();
    const then = new Date(timestamp);
    const seconds = Math.floor((now - then) / 1000);

    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)} days ago`;

    // For older notifications, show the date
    return then.toLocaleDateString();
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/**
 * Clean up notification system (call when user logs out)
 */
export function cleanupNotifications() {
    if (notificationRefreshInterval) {
        clearInterval(notificationRefreshInterval);
        notificationRefreshInterval = null;
    }
    if (notificationEventSource) {
        try { notificationEventSource.close(); } catch { /* ignore */ }
        notificationEventSource = null;
    }
    isNotificationPanelOpen = false;
}
