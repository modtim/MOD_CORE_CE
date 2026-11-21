// Dashboard/js/platform-backups.js
//
// FX.84 p2c — Backups card inside System → Settings (platform-admin only).
// Backend: routers/platform_backups.py — GET /platform/backups returns the
// seven backup.* settings plus the readings the backup machinery already
// writes (backup_status.json last-run block, restore-test ledger lines,
// ledger_readable). PUT /platform/backups/settings accepts only those seven
// keys; validators (cron shape, target enum, ranges) answer 422 with the
// validator message, surfaced verbatim. Credentials are never in any
// payload — off-box transport secrets live in the Vault; if a masked value
// ever appears under a secret-ish key it is rendered as the mask, never
// expanded. Scheduler/expiry wiring landed in FX.84 p2b; off-box
// transports (s3/nfs/disk/ssh) are Planned (FX.84 p3), so the card says
// "Planned:" for them.

import { platformBackups } from './api.js';
import { withButtonBusy } from './utils.js';

// FX.84 p2c — Backups card block (the vm test extracts from here down).
const _BACKUP_CRON_RE = /^\S+(\s+\S+){4}$/;
const _BACKUP_TARGETS = ['none', 's3', 'nfs', 'disk', 'ssh'];
const _PB_SECRETISH_RE = /key|secret|token|password|credential/i;

function _pbEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _pbFormatError(err) {
    // Verbatim server detail: string as-is; FastAPI 422 arrays joined as
    // "field: msg" pairs (same shape as account.js _formatScError).
    const detail = err && err.detail !== undefined ? err.detail : (err ? err.message : null);
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(e => {
            if (!e || typeof e !== 'object') return String(e);
            const loc = Array.isArray(e.loc) ? e.loc.filter(p => p !== 'body').join('.') : '';
            return loc ? `${loc}: ${e.msg || 'invalid'}` : (e.msg || JSON.stringify(e));
        }).join('; ');
    }
    if (detail && typeof detail === 'object') { try { return JSON.stringify(detail); } catch { return 'Unknown error'; } }
    return (err && err.message) || 'Unknown error';
}

function _pbSecretText(v) {
    // Never render secret values: a masked credential (asterisks) is shown
    // as the mask; anything else under a secret-ish key is hidden.
    if (v == null) return '';
    const s = String(v);
    return /[*•●]/.test(s) ? s : '[hidden]';
}

function _pbOnOff(v) {
    return v === true ? 'On' : v === false ? 'Off' : '—';
}

export async function initializePlatformBackupsPanel(user) {
    const card = document.getElementById('platform-backups-card');
    if (!card) return;
    // Mirror the backend gate (_require_platform_admin): platform admins
    // only — tenant admins and members never call GET.
    if (!user || user.is_platform_admin !== true) return;

    let data;
    try {
        data = await platformBackups.get();
    } catch {
        return; // 403/404 — keep the card hidden
    }
    data = data || {};
    const s = data.settings || {};

    const readings = document.getElementById('pb-readings');
    if (readings) {
        const rows = [
            ['Schedule', `${_pbOnOff(s.schedule_enabled)} — cron ${s.schedule_cron || '—'}`],
            ['Retention', `${s.retention_days ?? '—'} days · keep last ${s.monthly_keep ?? '—'} month-end sets`],
            ['Off-box destination', `${s.offbox_target || 'none'}${s.offbox_path ? ` · ${s.offbox_path}` : ' · not set'}`],
            ['Restore test monthly', _pbOnOff(s.restore_test_monthly)],
        ];
        // FX.58 s5 — the optional second off-site copy: the API only sends
        // `secondary` when a second target is configured, so this line is
        // rendered only when present (never a placeholder for "none").
        const sec = data.secondary;
        if (sec && typeof sec === 'object') {
            const secStatus = sec.overall || (sec.verified === true ? 'ok'
                : sec.verified === false ? 'not verified' : 'unknown');
            const secVerified = sec.last_success_utc || sec.last_attempt_utc || 'never';
            rows.push(['Second off-site copy', `${secStatus} · last verified ${secVerified}`]);
        }
        readings.innerHTML = rows.map(([k, v]) =>
            `<p class="text-sm"><span class="font-semibold">${_pbEsc(k)}</span> ${_pbEsc(v)}</p>`).join('');
    }

    const bannerEl = document.getElementById('pb-banner');
    if (bannerEl && s.offbox_target && s.offbox_target !== 'none') {
        bannerEl.className = 'text-sm text-orange-800 bg-orange-50 rounded-md px-3 py-2';
        bannerEl.textContent = `Off-box destination set to ${s.offbox_target}. Planned: the s3/nfs/disk/ssh transports ship in FX.84 part 3; transport credentials stay in the Vault and are never stored in these settings.`;
    }

    const lastRunEl = document.getElementById('pb-last-run');
    if (lastRunEl) {
        const lr = data.last_run;
        if (!lr) {
            lastRunEl.textContent = 'No backup run recorded yet (backup_status.json not present).';
        } else {
            const parts = [];
            for (const k of Object.keys(lr)) {
                const v = lr[k];
                if (_PB_SECRETISH_RE.test(k)) { parts.push(`${k}: ${_pbSecretText(v)}`); continue; }
                parts.push(`${k}: ${v === true ? 'yes' : v === false ? 'no' : (v === null || v === '' ? '—' : String(v))}`);
            }
            lastRunEl.textContent = parts.join(' · ');
        }
    }

    const testsEl = document.getElementById('pb-restore-tests');
    if (testsEl) {
        if (data.ledger_readable === false) {
            testsEl.textContent = 'Restore-test ledger not readable on this install.';
        } else {
            const tests = Array.isArray(data.restore_tests) ? data.restore_tests : [];
            if (!tests.length) {
                testsEl.textContent = 'No restore tests recorded yet.';
            } else {
                testsEl.innerHTML = tests.map(t => {
                    const ok = t.ok === true ? 'passed' : t.ok === false ? 'FAILED' : 'unknown';
                    const extra = t.error ? ` — ${t.error}` : '';
                    const times = t.rto_seconds != null || t.rpo_seconds != null
                        ? ` · RTO ${t.rto_seconds ?? '—'}s · RPO ${t.rpo_seconds ?? '—'}s` : '';
                    return `<p class="text-sm">${_pbEsc(t.ts || '—')} · set ${_pbEsc(t.backup_id || '—')} · ${ok}${times}${extra}</p>`;
                }).join('');
            }
        }
    }

    card.classList.remove('hidden');

    const statusEl = document.getElementById('pb-status');
    const saveBtn = document.getElementById('pb-save-btn');
    const form = document.getElementById('pb-form');
    function setStatus(msg, cls) {
        if (statusEl) { statusEl.className = `text-sm ${cls}`; statusEl.textContent = msg || ''; }
    }

    if (form && !form.dataset.wired) {
        form.dataset.wired = '1';
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const fields = {
                schedule_enabled: document.getElementById('pb-f-schedule-enabled')?.value,
                schedule_cron: document.getElementById('pb-f-schedule-cron')?.value,
                retention_days: document.getElementById('pb-f-retention-days')?.value,
                monthly_keep: document.getElementById('pb-f-monthly-keep')?.value,
                offbox_target: document.getElementById('pb-f-offbox-target')?.value,
                offbox_path: document.getElementById('pb-f-offbox-path')?.value,
                restore_test_monthly: document.getElementById('pb-f-restore-test-monthly')?.value,
            };
            const payload = {};
            for (const [k, raw] of Object.entries(fields)) {
                const v = (raw || '').trim();
                if (!v) continue;
                if (k === 'schedule_enabled' || k === 'restore_test_monthly') {
                    if (/^(true|on|yes)$/i.test(v)) payload[k] = true;
                    else if (/^(false|off|no)$/i.test(v)) payload[k] = false;
                    else { setStatus(`${k} must be on/off.`, 'text-red-700'); return; }
                } else if (k === 'retention_days' || k === 'monthly_keep') {
                    const n = Number(v);
                    if (!Number.isInteger(n)) { setStatus(`${k} must be a whole number.`, 'text-red-700'); return; }
                    if (k === 'retention_days' && !(n >= 1 && n <= 3650)) { setStatus('retention_days must be between 1 and 3650 days.', 'text-red-700'); return; }
                    if (k === 'monthly_keep' && !(n >= 0 && n <= 480)) { setStatus('monthly_keep must be between 0 and 480 months.', 'text-red-700'); return; }
                    payload[k] = n;
                } else if (k === 'schedule_cron') {
                    if (!_BACKUP_CRON_RE.test(v)) { setStatus('schedule_cron must be 5 whitespace-separated cron fields.', 'text-red-700'); return; }
                    payload[k] = v;
                } else if (k === 'offbox_target') {
                    // No client-side enum: BACKUP_OFFBOX_TARGETS is server-
                    // owned and its validator message is surfaced verbatim.
                    payload[k] = v;
                } else {
                    payload[k] = v;
                }
            }
            if (!Object.keys(payload).length) { setStatus('Nothing to change — fill the fields you want to save.', 'text-gray-500'); return; }
            setStatus('Saving…', 'text-gray-500');
            await withButtonBusy(saveBtn, async () => {
                try {
                    await platformBackups.update(payload);
                    setStatus('Saved.', 'text-green-700');
                    if (window.showInfo) window.showInfo('Backup settings saved.', 'Backups');
                } catch (err) {
                    // Server 422/404 detail verbatim (validator messages are curated text).
                    setStatus(`Could not save: ${_pbFormatError(err)}`, 'text-red-700');
                }
            });
        });
    }
}
