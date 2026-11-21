        // BUG.B29c — delegate HTTP to the shared fetchApi so auth, token refresh,
// error parsing and response deserialization are handled in one place.
import { fetchApi, isPaidEdition } from './api.js';
import { withButtonBusy } from './utils.js';

// API utility function — thin wrapper around the shared fetchApi. fetchApi's
// API_BASE_URL already carries the /v1 prefix (see CORE/Dashboard/inject-config.sh),
// so paths are passed through unchanged — only a leading '/' is guaranteed.
        async function apiCall(endpoint, method = 'GET', body = null) {
            const path = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
            return fetchApi(path, {
                method,
                body: body ? JSON.stringify(body) : undefined,
            });
        }

        // --- Plan-gate notice (402 paid-plan gate) ------------------------
        function _showPlanGate(msg) {
            const el = document.getElementById('audit-plan-gate');
            const msgEl = document.getElementById('audit-plan-gate-message');
            if (!el) return;
            if (msgEl) msgEl.textContent = msg
                || 'The tamper-evident (HMAC hash-chained) permission audit trail is not part of this edition.';
            el.classList.remove('hidden');
            // Disable filter controls so no further gated calls fire
            document.getElementById('apply-filters')?.setAttribute('disabled', 'disabled');
            document.getElementById('reset-filters')?.setAttribute('disabled', 'disabled');
            document.getElementById('filter-search')?.setAttribute('disabled', 'disabled');
            document.getElementById('audit-page-download-csv')?.setAttribute('disabled', 'disabled');
            document.getElementById('audit-page-download-json')?.setAttribute('disabled', 'disabled');
        }

        function _hidePlanGate() {
            const el = document.getElementById('audit-plan-gate');
            if (!el) return;
            el.classList.add('hidden');
            ['apply-filters', 'reset-filters', 'filter-search',
             'audit-page-download-csv', 'audit-page-download-json'].forEach(id => {
                document.getElementById(id)?.removeAttribute('disabled');
            });
        }

        let currentPage = 0;
        const pageSize = 50;
        let currentFilters = {};

        // BUG.B30b — free-text search bar (id="filter-search"). The server matches the
        // `q` param against action, resource type and actor/target email.
        function getSearchValue() {
            const el = document.getElementById('filter-search');
            return el ? String(el.value || '').trim() : '';
        }

        let searchDebounceTimer = null;

        // BUG.B29c Part C — the active filters + search as query params. Shared by the
        // page load and the export fallback so both honour exactly the same filters.
        function buildFilterParams(limit, offset) {
            const params = new URLSearchParams({ limit, offset });
            if (currentFilters.action) params.append('action', currentFilters.action);
            if (currentFilters.resource_type) params.append('resource_type', currentFilters.resource_type);
            if (currentFilters.start_date) params.append('start_date', currentFilters.start_date);
            const searchValue = getSearchValue();
            if (searchValue) params.append('q', searchValue);
            return params;
        }

        // Format timestamp
        function formatTimestamp(timestamp) {
            const date = new Date(timestamp);
            return date.toLocaleString();
        }

        // Format action for display
        function formatAction(action) {
            const actionMap = {
                'permission_granted': { label: 'Granted', color: 'green' },
                'permission_revoked': { label: 'Revoked', color: 'red' },
                'permission_updated': { label: 'Updated', color: 'yellow' },
                'permission_requested': { label: 'Requested', color: 'blue' },
                'permission_request_approved': { label: 'Approved', color: 'green' },
                'permission_request_denied': { label: 'Denied', color: 'red' },
                'permission_request_cancelled': { label: 'Cancelled', color: 'gray' },
            };

            const info = actionMap[action]
                || roadActionLabel(action)
                || { label: action, color: 'gray' };
            return `<span class="px-2 py-1 rounded-full text-xs font-medium bg-${info.color}-100 text-${info.color}-800">${info.label}</span>`;
        }

        // Friendly labels for METHOD:/path audit keys (road router). Never blank.
        function roadActionLabel(action) {
            const m = /^(GET|POST|PUT|PATCH|DELETE):\/(road|workflows)(\/.*)?$/.exec(action);
            if (!m) return null;
            const method = m[1];
            const p = (m[3] || '').replace(/^\/+|\/+$/g, '');
            if (action === 'POST:/road' || action === 'POST:/workflows') return { label: 'Workflow created', color: 'blue' };
            if (/\/execute$/.test(p)) return { label: 'Workflow run', color: 'green' };
            if (p.startsWith('runs')) return { label: 'Run files updated', color: 'yellow' };
            if (p.startsWith('plan-')) return { label: 'Plan generated', color: 'blue' };
            if (method === 'PUT' || method === 'PATCH') return { label: 'Workflow updated', color: 'yellow' };
            if (method === 'DELETE') return { label: 'Workflow removed', color: 'red' };
            if (method === 'GET') return { label: 'Workflow viewed', color: 'gray' };
            return { label: action, color: 'gray' };
        }

        // Load audit logs
        // BUG.B70 — the events table ran off the screen: long values (emails,
        // resource refs, metadata JSON) forced the table wider than the page.
        // Cells now wrap (break-words) or truncate (max-w-xs truncate) and the
        // full value is kept in a title attribute.
        function escapeAttr(value) {
            return String(value == null ? '' : String(value))
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        function cellText(value) {
            const s = value == null ? '' : String(value);
            return s.length > 48 ? `${s.slice(0, 45)}…` : s;
        }

        function titleAttr(value) {
            return ` title="${escapeAttr(value)}"`;
        }

        async function loadAuditLogs() {
            const loadingState = document.getElementById('loading-state');
            const emptyState = document.getElementById('empty-state');
            const tbody = document.getElementById('logs-tbody');

            loadingState.classList.remove('hidden');
            emptyState.classList.add('hidden');
            tbody.innerHTML = '';

            try {
                // Build query parameters
                const params = buildFilterParams(pageSize, currentPage * pageSize);

                const response = await apiCall(`/audit-logs/permissions?${params.toString()}`);

                loadingState.classList.add('hidden');

                if (!response || response.length === 0) {
                    emptyState.classList.remove('hidden');
                    document.getElementById('results-info').textContent = 'No results';
                    document.getElementById('next-page').disabled = true;
                    return;
                }

                // Render logs
                response.forEach(log => {
                    const row = document.createElement('tr');
                    row.className = 'hover:bg-gray-50 cursor-pointer';
                    row.onclick = () => showLogDetail(log);

                    row.innerHTML = `
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                            ${formatTimestamp(log.timestamp)}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm">
                            ${formatAction(log.action)}
                        </td>
                        <td class="px-6 py-4 max-w-xs break-words text-sm text-gray-900"${titleAttr(log.actor_email || 'Unknown')}>
                            ${cellText(log.actor_email || 'Unknown')}
                        </td>
                        <td class="px-6 py-4 max-w-xs break-words text-sm text-gray-900"${titleAttr(log.target_email || '-')}>
                            ${cellText(log.target_email || '-')}
                        </td>
                        <td class="px-6 py-4 max-w-xs truncate text-sm text-gray-900"${titleAttr(`${log.resource_type} #${log.resource_id}`)}>
                            ${cellText(`${log.resource_type} #${log.resource_id}`)}
                        </td>
                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                            ${log.permission_level || '-'}
                        </td>
                        <td class="px-6 py-4 max-w-xs truncate text-sm text-primary"${titleAttr(log.metadata ? JSON.stringify(log.metadata) : '')}>
                            View →
                        </td>
                    `;

                    tbody.appendChild(row);
                });

                // Update pagination
                const start = currentPage * pageSize + 1;
                const end = start + response.length - 1;
                document.getElementById('results-info').textContent = `Showing ${start}-${end}`;

                document.getElementById('prev-page').disabled = currentPage === 0;
                document.getElementById('next-page').disabled = response.length < pageSize;

            } catch (error) {
                loadingState.classList.add('hidden');
                // fetchApi THROWS ApiError(message, status, headers, detail) (api.js:221),
                // so the 402 lands here, not on the returned value.
                if (error.status === 402) {
                    _showPlanGate(error.detail?.detail);
                    return;
                }
                _hidePlanGate();
                tbody.innerHTML = `
                    <tr>
                        <td colspan="7" class="px-6 py-4 text-center text-red-600">
                            Error loading audit logs: ${error.message}
                        </td>
                    </tr>
                `;
            }
        }

        // Copy the audit metadata JSON block to the clipboard. Async Clipboard
        // API with an execCommand fallback for insecure-origin / older browsers;
        // briefly flips the button label to "Copied" as confirmation.
        async function copyAuditMeta(btn) {
            const block = btn.closest('[data-log-block]');
            const pre = block && block.querySelector('pre');
            const text = (pre && pre.textContent) || '';
            if (!text.trim()) return;
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(text);
                } else {
                    const ta = document.createElement('textarea');
                    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
                    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
                }
                const o = btn.textContent; btn.textContent = 'Copied';
                setTimeout(() => { btn.textContent = o; }, 1500);
            } catch (e) { /* clipboard blocked — the block is selectable */ }
        }
        window.copyAuditMeta = copyAuditMeta;

        // Show log detail modal
        function showLogDetail(log) {
            const modal = document.getElementById('log-detail-modal');
            const content = document.getElementById('log-detail-content');

            content.innerHTML = `
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="text-sm font-medium text-gray-600">ID</label>
                        <p class="text-gray-900">${log.id}</p>
                    </div>
                    <div>
                        <label class="text-sm font-medium text-gray-600">Action</label>
                        <p class="text-gray-900">${formatAction(log.action)}</p>
                    </div>
                    <div>
                        <label class="text-sm font-medium text-gray-600">Timestamp</label>
                        <p class="text-gray-900">${formatTimestamp(log.timestamp)}</p>
                    </div>
                    <div>
                        <label class="text-sm font-medium text-gray-600">Actor</label>
                        <p class="text-gray-900">${log.actor_email || 'Unknown'}</p>
                    </div>
                    ${log.target_email ? `
                        <div>
                            <label class="text-sm font-medium text-gray-600">Target User</label>
                            <p class="text-gray-900">${log.target_email}</p>
                        </div>
                    ` : ''}
                    <div>
                        <label class="text-sm font-medium text-gray-600">Resource</label>
                        <p class="text-gray-900">${log.resource_type} #${log.resource_id}</p>
                    </div>
                    ${log.permission_level ? `
                        <div>
                            <label class="text-sm font-medium text-gray-600">Permission Level</label>
                            <p class="text-gray-900">${log.permission_level}</p>
                        </div>
                    ` : ''}
                    ${log.ip_address ? `
                        <div>
                            <label class="text-sm font-medium text-gray-600">IP Address</label>
                            <p class="text-gray-900">${log.ip_address}</p>
                        </div>
                    ` : ''}
                </div>

                ${log.metadata ? `
                    <div class="mt-4" data-log-block>
                        <div class="flex items-center justify-between">
                            <label class="text-sm font-medium text-gray-600">Additional Details</label>
                            <button class="copy-audit-meta-btn px-3 py-1.5 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300">Copy</button>
                        </div>
                        <pre class="mt-2 bg-gray-50 p-4 rounded-lg text-sm overflow-x-auto">${JSON.stringify(log.metadata, null, 2)}</pre>
                    </div>
                ` : ''}
            `;

            modal.classList.remove('hidden');
        }

        // BUG.B55 — the prev/next listeners and the first load are NOT wired here.
        // They used to be: module-level, unguarded, and they ran at IMPORT time.
        // hub-compliance.js now statically imports this module (BUG.B29a Part A), so
        // that import-time DOM work threw before any markup existed (getElementById
        // returns null for #prev-page / #next-page until the hub injects pages/audit-
        // logs.html into #main-panel-audit-logs at runtime), which failed the module
        // evaluation, which failed hub-compliance.js's evaluation, which made ui.js's
        // optionalPageModule('./hub-compliance.js', ...) resolve to null — so the
        // whole Compliance hub rendered nothing. The same wiring now lives inside the
        // guarded _wire() below (btnPrev / btnNext + the first loadAuditLogs()), which
        // initializeAuditLogsPage() calls on tab activation.
        // --- Audit export (BUG.B29c Part C) ---------------------------------
        // Ported from the old Logs-page Audit tab (#13). Primary path is the
        // server-side /audit-logs/export endpoint (permission_audit_logs +
        // audit_events, with HMAC integrity status) — platform-admin only. When
        // that is not available to the current user (403 / entitlement) we build
        // the file client-side from the rows this page can already read, honouring
        // the active filters and the search bar via buildFilterParams().

        function _setExportStatus(msg) {
            const el = document.getElementById('audit-page-export-status');
            if (el) el.textContent = msg || '';
        }

        function _downloadBlob(content, filename, mime) {
            const blob = new Blob([content], { type: mime });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }

        function _csvCell(v) {
            if (v === null || v === undefined) return '';
            let s = typeof v === 'object' ? JSON.stringify(v) : String(v);
            if (/[",\r\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
            return s;
        }

        // Flatten either export shape into one flat row list with a `source` column.
        function _rowsToRecords(exportData) {
            const records = [];
            (exportData.permission_audit_logs || []).forEach(l => {
                records.push({
                    source: 'permission_audit_log',
                    id: l.id,
                    timestamp: l.timestamp,
                    action: l.action,
                    actor: l.actor_email || l.actor_user_id || '',
                    target: l.target_email || l.target_user_id || '',
                    resource_type: l.resource_type || '',
                    resource_id: l.resource_id || '',
                    tenant_id: l.tenant_id || '',
                    integrity_valid: l.integrity_valid,
                    details: l.metadata,
                });
            });
            (exportData.audit_events || []).forEach(e => {
                records.push({
                    source: 'audit_event',
                    id: e.id,
                    timestamp: e.timestamp,
                    action: e.event_name,
                    actor: e.user_id || '',
                    target: '',
                    resource_type: '',
                    resource_id: '',
                    tenant_id: e.tenant_id || '',
                    integrity_valid: undefined,
                    details: e.details,
                });
            });
            return records;
        }

        function _recordsToCsv(records) {
            const allCols = ['source', 'id', 'timestamp', 'action', 'actor', 'target',
                'resource_type', 'resource_id', 'tenant_id', 'integrity_valid', 'details'];
            // CE exports carry no integrity result, so CE has no integrity_valid column.
            const cols = records.omitIntegrity ? allCols.filter(c => c !== 'integrity_valid') : allCols;
            const lines = [cols.join(',')];
            records.forEach(r => lines.push(cols.map(c => _csvCell(r[c])).join(',')));
            return lines.join('\r\n');
        }

        async function downloadExport(format) {
            const stamp = new Date().toISOString().replace(/[:.]/g, '-');
            _setExportStatus('Preparing export…');

            let exportData = null;
            let scope = 'consolidated';

            // 1. Try the server-side consolidated export (platform admins).
            try {
                exportData = await apiCall('/audit-logs/export');
            } catch (err) {
                // 402 — plan gate; show same notice, no "Export failed"
                if (err.status === 402) {
                    _showPlanGate(err.detail?.detail);
                    _setExportStatus('Export unavailable — plan gate.');
                    return;
                }
                // 2. Fall back to the rows this page can read, honouring the active
                //    filters and the search bar.
                console.warn('Server audit export unavailable, building client-side:', err?.status || err);
                scope = 'permission-audit';
                try {
                    const params = buildFilterParams(10000, 0);
                    const rows = await apiCall(`/audit-logs/permissions?${params.toString()}`);
                    exportData = {
                        export_metadata: {
                            exported_at: new Date().toISOString(),
                            source: 'client-side (permission audit)',
                        },
                        permission_audit_logs: rows || [],
                        audit_events: [],
                    };
                } catch (err2) {
                    // 402 — plan gate
                    if (err2.status === 402) {
                        _showPlanGate(err2.detail?.detail);
                        _setExportStatus('Export unavailable — plan gate.');
                        return;
                    }
                    console.error('Audit export fallback failed:', err2);
                    _setExportStatus('Export failed — could not load audit logs.');
                    return;
                }
            }

            const records = _rowsToRecords(exportData);
            if (records.length === 0) {
                _setExportStatus('No audit records to export for the current filters.');
                return;
            }

            if (format === 'csv') {
                records.omitIntegrity = !(await isPaidEdition());
                _downloadBlob(_recordsToCsv(records), `audit-log-${stamp}.csv`, 'text/csv;charset=utf-8');
            } else {
                _downloadBlob(JSON.stringify(exportData, null, 2), `audit-log-${stamp}.json`, 'application/json');
            }
            _setExportStatus(`Exported ${records.length} record${records.length === 1 ? '' : 's'} (${scope}).`);
        }

    // BUG.B29a Part A — extract all DOM-dependent initialisation (event
    // listeners, banner mount, first load) into a single function so the
    // module can be imported by hub-compliance.js without executing on import.
    // The DOM elements are queried inside _wire so they resolve correctly
    // whether the tab is first-time loaded or re-entered later.
    // Null-safe: every getElementById is guarded. Idempotent: re-entering the
    // tab never double-binds listeners (dataset.wired guard), but still
    // reloads the logs on each entry.
    function _wire() {
        const root = document.getElementById('audit-logs-container');
        if (!root) return;
        if (root.dataset.wired === '1') return;
        root.dataset.wired = '1';

        // Event listeners (all getElementById calls are null-guarded below).
        const btnClose = document.getElementById('close-detail-modal');
        if (btnClose) btnClose.addEventListener('click', () => {
            const modal = document.getElementById('log-detail-modal');
            if (modal) modal.classList.add('hidden');
        });

        const btnApply = document.getElementById('apply-filters');
        if (btnApply) btnApply.addEventListener('click', () => {
            if (document.getElementById('audit-plan-gate') && !document.getElementById('audit-plan-gate').classList.contains('hidden')) return;
            currentFilters = {
                action: (document.getElementById('filter-action') || {}).value || '',
                resource_type: (document.getElementById('filter-resource-type') || {}).value || '',
            };

            const dateRangeEl = document.getElementById('filter-date-range');
            const dateRange = dateRangeEl ? dateRangeEl.value : '7';
            if (dateRange !== 'all') {
                const daysAgo = parseInt(dateRange);
                const startDate = new Date();
                startDate.setDate(startDate.getDate() - daysAgo);
                currentFilters.start_date = startDate.toISOString();
            }

            currentPage = 0;
            loadAuditLogs();
        });

        const btnReset = document.getElementById('reset-filters');
        if (btnReset) btnReset.addEventListener('click', () => {
            const filterAction = document.getElementById('filter-action');
            if (filterAction) filterAction.value = '';
            const filterResource = document.getElementById('filter-resource-type');
            if (filterResource) filterResource.value = '';
            const filterDateRange = document.getElementById('filter-date-range');
            if (filterDateRange) filterDateRange.value = '7';
            // BUG.B30b — Reset clears the search bar too.
            const searchElReset = document.getElementById('filter-search');
            if (searchElReset) searchElReset.value = '';
            currentFilters = {};
            currentPage = 0;
            loadAuditLogs();
        });

        // BUG.B30b — the search bar applies the filters on its own:
        //  * Enter in the box = the same as clicking #apply-filters,
        //  * typing debounces 400 ms then applies (so a burst of keystrokes is one
        //    request, not one per character),
        //  * changing the search always returns to the first page.
        const searchInput = document.getElementById('filter-search');
        if (searchInput) {
            let lastAppliedSearch = null;

            const applySearch = () => {
                if (document.getElementById('audit-plan-gate') && !document.getElementById('audit-plan-gate').classList.contains('hidden')) return;
                const q = getSearchValue();
                if (q === lastAppliedSearch) return;
                lastAppliedSearch = q;
                currentPage = 0;
                loadAuditLogs();
            };

            const scheduleSearchApply = () => {
                if (searchDebounceTimer !== null) {
                    clearTimeout(searchDebounceTimer);
                }
                searchDebounceTimer = setTimeout(() => {
                    searchDebounceTimer = null;
                    applySearch();
                }, 400);
            };

            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    // Enter applies immediately — cancel any pending debounce first.
                    if (searchDebounceTimer !== null) {
                        clearTimeout(searchDebounceTimer);
                        searchDebounceTimer = null;
                    }
                    applySearch();
                } else if (!e.key || e.key.length === 1) {
                    scheduleSearchApply();
                }
            });
            searchInput.addEventListener('input', scheduleSearchApply);
            searchInput.addEventListener('paste', scheduleSearchApply);
        }

        // BUG.B29c Part C — CSV / JSON export buttons (ported from the old Logs-page
        // Audit tab). Guarded so the hub tab renders fine without them.
        const csvBtn = document.getElementById('audit-page-download-csv');
        if (csvBtn) csvBtn.addEventListener('click', (e) => {
            if (document.getElementById('audit-plan-gate') && !document.getElementById('audit-plan-gate').classList.contains('hidden')) return;
            withButtonBusy(e.currentTarget, () => downloadExport('csv'), { busyLabel: 'Exporting…' });
        });

        const jsonBtn = document.getElementById('audit-page-download-json');
        if (jsonBtn) jsonBtn.addEventListener('click', (e) => {
            if (document.getElementById('audit-plan-gate') && !document.getElementById('audit-plan-gate').classList.contains('hidden')) return;
            withButtonBusy(e.currentTarget, () => downloadExport('json'), { busyLabel: 'Exporting…' });
        });

        // R-167 — Copy button in the detail modal is rendered via innerHTML,
        // so the handler is wired by delegation instead of an on* attribute.
        const modalEl = document.getElementById('log-detail-modal');
        if (modalEl) modalEl.addEventListener('click', (e) => {
            const b = e.target.closest('.copy-audit-meta-btn');
            if (b) copyAuditMeta(b);
        });

        // GY.C0 — declared compliance environment admin banner (hook).
        // Silent no-op for non-admins / on API errors.
        if (window.MOD_COMPLIANCE_ENV_BANNER) {
            window.MOD_COMPLIANCE_ENV_BANNER.mount(
                document.getElementById('compliance-env-banner'));
        }

        // Pagination listeners
        const btnPrev = document.getElementById('prev-page');
        if (btnPrev) btnPrev.addEventListener('click', () => {
            if (currentPage > 0) {
                currentPage--;
                loadAuditLogs();
            }
        });

        const btnNext = document.getElementById('next-page');
        if (btnNext) btnNext.addEventListener('click', () => {
            currentPage++;
            loadAuditLogs();
        });

        // Initialize default filter (last 7 days) and load logs on first entry.
        const defaultStartDate = new Date();
        defaultStartDate.setDate(defaultStartDate.getDate() - 7);
        currentFilters = { start_date: defaultStartDate.toISOString() };
        loadAuditLogs();
    }

    // Exported by name for hub-compliance.js to call on tab activation.
    export function initializeAuditLogsPage() {
        // rc20e: audit-logs is the CE-CORE tab (manifest.yaml:2182 deny_exception re-ships
        // the hub shell "hosting the CE-core audit-logs tab"; hub-compliance.js _TAB_GATE
        // ['audit-logs'] = 'tenant' "visible on CE and paid"; router api_allow manifest.yaml:165
        // + main.py:2120). Hiding it left the Compliance Center with ZERO tabs (TAB_KEYS[0]).
        // Paid-only is the tamper-evident read/export: audit-integrity capability (entitlements.py:173)
        // 402s /audit-logs/permissions + /export, which loadAuditLogs() :179 and downloadExport()
        // :376 render as #audit-plan-gate and disable the filter + export controls. Wire always.
        _wire();
    }
