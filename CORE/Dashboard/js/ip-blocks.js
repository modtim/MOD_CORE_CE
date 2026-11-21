// GSEC FX.106 p2b — auto-blocked client IPs (platform admin only).
//
// Backend: CORE/API/routers/platform_ip_blocks.py (mounted at /v1 in
// CORE/API/main.py), logic in services/ip_block_admin.py:
//   GET  /platform/security/ip-blocks?limit=  -> {scope:'this_process',
//        blocks:[{ip,reason,tripped_at,expires_at,remaining_seconds,
//        hit_count_429}], recent_hits:[{kind,timestamp,ip_address,tenant_id,
//        detail}], settings:{auto_block_enabled,block_after_429s,
//        block_after_login_failures,window_min,duration_min,exempt_cidrs},
//        counts:{active_blocks,recent_hits}}
//   POST /platform/security/ip-blocks/{ip}/unblock  {reason REQUIRED}
//        -> {ip,unblocked,scope,reason,prior_block_events} + one
//        API_AUTO_UNBLOCK audit row; 404 when nothing is blocked for that IP
//        on THIS process; 400 when {ip} is not an IP address.
//
// SCOPE honesty: the block store is IN-MEMORY PER API PROCESS
// (services/api_rate_limit_guard.py). The payload's `scope` field is printed
// verbatim in ib-scope — the page never presents this as a cluster-wide block.
// The audit trail (block/unblock rows) IS process-independent.
//
// The Unblock button is behind confirm() AND requires a reason: the reason
// text field (ib-unblock-reason) is read before the POST, and an empty reason
// is refused client-side with an explanation (the server also refuses — its
// body schema marks `reason` required, min 3 chars). The server's 400/404
// detail is surfaced verbatim in ib-error, never swallowed.
import { ipBlocks, me } from './api.js';
import { withButtonBusy } from './utils.js';

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 19).replace('T', ' ');
}

function _fmtEpoch(ts) {
    if (ts == null) return '—';
    const d = new Date(Number(ts) * 1000);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 19).replace('T', ' ');
}

function _showError(msg) {
    const box = document.getElementById('ib-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('ib-error');
    if (box) box.classList.add('hidden');
}

function _errText(e) {
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(d => `${d.loc || d.field || ''}: ${d.msg || d.detail || ''}`)
            .join('; ');
    }
    return String(detail);
}

function _field(id) {
    const el = document.getElementById(id);
    if (!el) return '';
    const v = el.value != null ? el.value : el.textContent;
    return String(v == null ? '' : v).trim();
}

// ---------------------------------------------------------------------------
// init + controls
// ---------------------------------------------------------------------------

export async function initializeIpBlocksPage() {
    const ub = document.getElementById('ib-unblock-btn');
    if (ub && !ub.dataset.wired) {
        ub.dataset.wired = '1';
        ub.addEventListener('click', () => { handleFormUnblock(); });
    }
    renderSettings();
    await loadTable();
    await loadHits();
    // FX.145.p3c — wire the admin ingress allowlist card (try/catch so a
    // failure never breaks the Blocked IPs tab).  Non-platform-admins get
    // the card hidden rather than seeing errors.
    try {
        const { me, getPlatformFeatures } = await import('./api.js');
        // 2400LR-pg5: admin_ingress_allowlist.py :968 + platform_settings.py :775 are denied
        // on CE, so the card could only error; hide it (fail open on unknown edition).
        const _ent = await getPlatformFeatures().then((f) => f && f.entitlements).catch(() => null);
        if (_ent && _ent.edition === 'ce' && !_ent.error) {
            document.getElementById('ai-card-container')?.classList.add('hidden');
            return;
        }
        const { initializeAdminIngressAllowlist } = await import('./admin-ingress-allowlist.js');
        const user = await me.get();
        const cardEl = document.getElementById('ai-card-container');
        if (user && !user.is_platform_admin && cardEl) {
            cardEl.classList.add('hidden');
        } else {
            initializeAdminIngressAllowlist();
        }
    } catch (_) { /* card init failure must never break the tab */ }
}

function renderSettings() {
    const box = document.getElementById('ib-settings');
    if (!box) return;
    box.textContent = _settingsText || 'Settings unread (they live on Platform Settings, group api.rate_limit).';
}

let _settingsText = null;

// ---------------------------------------------------------------------------
// The block table
// ---------------------------------------------------------------------------

async function loadTable() {
    _clearError();
    const tbody = document.getElementById('ib-table-body');
    const empty = document.getElementById('ib-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    tbody.innerHTML = '<tr><td colspan="7" class="px-4 py-6 text-gray-500">Loading…</td></tr>';
    try {
        const [list, user] = await Promise.all([
            ipBlocks.list({ limit: 200 }),
            me.get(),
        ]);
        if (user && user.is_platform_admin === false) {
            // The nav hides the page for non-admins; a deep link still must
            // not render the queue (the API 403s anyway — belt and braces).
            tbody.innerHTML = '';
            _showError('Blocked IPs are a platform-admin surface.');
            return;
        }
        const scope = (list && list.scope) || 'this_process';
        const scopeBox = document.getElementById('ib-scope');
        if (scopeBox) {
            scopeBox.textContent = scope === 'this_process'
                ? 'Scope: THIS API process only — the block store is in-memory per process, so an unblock clears this replica. The block/unblock audit rows are the process-independent record.'
                : `Scope: ${scope}`;
        }
        const items = (list && list.blocks) || [];
        _lastHits = (list && list.recent_hits) || [];
        _settingsText = _settingsLine(list && list.settings);
        renderSettings();
        const count = document.getElementById('ib-count');
        if (count) {
            const c = (list && list.counts) || {};
            const bits = [`${c.active_blocks != null ? c.active_blocks : items.length} blocked IP(s)`];
            if (c.recent_hits != null) bits.push(`${c.recent_hits} recent hit(s)`);
            count.textContent = bits.join(' · ');
        }
        if (!items.length) {
            tbody.innerHTML = '';
            if (empty) empty.classList.remove('hidden');
            return;
        }
        tbody.innerHTML = items.map(rowHtml).join('');
        tbody.querySelectorAll('[data-ib-action]').forEach(btn => {
            btn.addEventListener('click', () =>
                handleUnblock(btn.dataset.ibIp, btn));
        });
    } catch (e) {
        tbody.innerHTML = '';
        _showError(_errText(e) || 'Failed to load the block list');
    }
}

function _settingsLine(st) {
    if (!st) return null;
    const bits = [
        `auto-block ${st.auto_block_enabled ? 'ON' : 'OFF'}`,
        `block after ${st.block_after_429s} x 429 or ${st.block_after_login_failures} failed sign-ins`,
        `window ${st.window_min} min`,
        `block lasts ${st.duration_min} min`,
    ];
    if (st.exempt_cidrs && st.exempt_cidrs.length) {
        bits.push(`exempt ${st.exempt_cidrs.join(', ')}`);
    }
    return bits.join(' · ');
}

function rowHtml(r) {
    const left = r.remaining_seconds != null ? `${Math.max(0, r.remaining_seconds)}s` : '—';
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${esc(r.ip || '—')}</td>
        <td class="px-4 py-3 text-xs">${esc(r.reason || 'auto_block')}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${_fmt(r.tripped_at)}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${_fmt(r.expires_at)}</td>
        <td class="px-4 py-3 text-right whitespace-nowrap">${esc(left)}</td>
        <td class="px-4 py-3 text-right whitespace-nowrap">${esc(r.hit_count_429 != null ? r.hit_count_429 : '—')}</td>
        <td class="px-4 py-3 text-right whitespace-nowrap">
            <button type="button" data-ib-action="unblock" data-ib-ip="${esc(r.ip || '')}" class="text-red-700 hover:underline text-sm">Unblock</button>
        </td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Unblock (row button and the standalone form share one path). The reason is
// REQUIRED: read off ib-unblock-reason BEFORE the POST, so an unblock without
// a stated reason never reaches the server and never writes an audit row.
// ---------------------------------------------------------------------------

async function postUnblock(ip, btn) {
    _clearError();
    const reason = _field('ib-unblock-reason');
    if (!reason || reason.length < 3) {
        _showError('An unblock needs a reason (at least a few words) — it becomes the audit row\'s "why". Type it in the Reason field, then click again.');
        return;
    }
    const okMsg = `Unblock ${ip} now? This clears the auto-block EARLY on THIS process only and writes one audited row (who, when, why). The IP starts being served again immediately.`;
    if (!confirm(okMsg)) return;
    try {
        if (btn) {
            await withButtonBusy(btn, async () => {
                await ipBlocks.unblock(ip, reason);
            });
        } else {
            await ipBlocks.unblock(ip, reason);
        }
        if (window.showInfo) {
            window.showInfo(`Auto-block for ${ip} cleared — the unblock is recorded in the audit log.`, 'Blocked IPs');
        }
        await loadTable();
        await loadHits();
    } catch (e) {
        if (btn) btn.disabled = false;
        // 404 = nothing blocked for that IP on this process; 400 = the
        // address is not a valid IP. Both details are the server's words.
        _showError(_errText(e) || `Could not unblock ${ip}`);
    }
}

function handleUnblock(ip, btn) {
    postUnblock(ip, btn);
}

async function handleFormUnblock() {
    const ip = _field('ib-unblock-ip');
    if (!ip) {
        _showError('Type the IP to unblock (or click a row\'s Unblock button).');
        return;
    }
    await postUnblock(ip, null);
}

// ---------------------------------------------------------------------------
// Recent limit hits (429 ring rows + the block/unblock audit rows)
// ---------------------------------------------------------------------------

async function loadHits() {
    const tbody = document.getElementById('ib-hits-body');
    const empty = document.getElementById('ib-hits-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    const hits = _lastHits || [];
    const hc = document.getElementById('ib-hits-count');
    if (hc) hc.textContent = `${hits.length} recent limit event(s)`;
    if (!hits.length) {
        tbody.innerHTML = '';
        if (empty) empty.classList.remove('hidden');
        return;
    }
    tbody.innerHTML = hits.map(hitRowHtml).join('');
}

let _lastHits = [];

function hitRowHtml(h) {
    const kind = h.kind || '—';
    const cls = kind === 'auto_block' ? 'bg-red-100 text-red-700'
        : kind === 'auto_unblock' ? 'bg-green-100 text-green-700'
        : 'bg-amber-100 text-amber-800';
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${cls}">${esc(kind)}</span></td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${_fmtEpoch(h.timestamp)}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${esc(h.ip_address || '—')}</td>
        <td class="px-4 py-3 whitespace-nowrap font-mono text-xs">${h.tenant_id != null ? esc(h.tenant_id) : 'platform'}</td>
        <td class="px-4 py-3 text-xs">${esc(h.detail || '')}</td>
    </tr>`;
}
