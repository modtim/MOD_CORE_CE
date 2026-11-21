// account.js - Handles the My Account page

import { me, securityContact, isPaidEdition } from './api.js';
import { getKcActionUrl, getMfaSetupUrl } from './auth.js';
import { withButtonBusy } from './utils.js';

export async function initializeAccountPage() {
    console.log('Initializing Account page');

    try {
        // Fetch user data and permissions in parallel
        const [user, permissions] = await Promise.all([
            me.get(),
            me.getPermissions()
        ]);

        populateAccountInfo(user);
        populatePermissions(permissions);
        populateSessionInfo();
        // 2400LR-dash6f: on CE the sso-profile, me/email-preferences and
        // tenant/* routers are denied (404). Read the edition once (fail-closed,
        // coalesced /platform/features cache) and skip those calls + hide their
        // sections; paid keeps every call unchanged.
        const paid = await isPaidEdition();
        // add-self-service-sso §6.8 — linked sign-in methods card
        loadLinkedIdentities(paid);
        // activation-lifecycle-emails — getting-started email opt-out
        loadEmailPreferences(paid);
        // notification-preferences (WS4.Q22) — in-app per-type toggles
        loadNotificationPreferences();
        // My Account controls
        initProfileEdit(user);
        initPasswordChange();
        // GY.C4.MFA — 2FA status + MOD-branded enrollment buttons
        initSecurity();
        loadSessions();
        initDataPrivacy();
        // Bug #2 — organization settings (owner / tenant-admin only)
        initOrgSettings(user, paid);
        // FX.89 p3 — tenant security contact (tenant-admin / platform-admin only)
        await initSecurityContact(user, permissions, paid);

    } catch (error) {
        console.error('Error loading account page:', error);
        showError('Failed to load account information. Please try refreshing the page.');
    }
}

function populateAccountInfo(user) {
    // Avatar initials
    const avatarEl = document.getElementById('account-avatar');
    if (avatarEl) {
        const displayName = user.first_name && user.last_name
            ? `${user.first_name} ${user.last_name}`
            : user.username || user.email || 'User';
        const initials = getInitials(displayName);
        avatarEl.textContent = initials;
    }

    // Name
    const nameEl = document.getElementById('account-name');
    if (nameEl) {
        if (user.first_name || user.last_name) {
            nameEl.textContent = `${user.first_name || ''} ${user.last_name || ''}`.trim();
        } else {
            nameEl.textContent = user.username || 'Not set';
        }
    }

    // Email
    const emailEl = document.getElementById('account-email');
    if (emailEl) {
        emailEl.textContent = user.email || 'Not set';
    }

    // Username
    const usernameEl = document.getElementById('account-username');
    if (usernameEl) {
        usernameEl.textContent = user.username || 'Not set';
    }

    // Role
    const roleEl = document.getElementById('account-role');
    if (roleEl) {
        const isSuperAdmin = user.parent_user_id === null;
        let roleLabel = user.role?.name || 'User';
        let roleClass = 'bg-gray-100 text-gray-800';

        if (isSuperAdmin) {
            roleLabel = 'Super Admin';
            roleClass = 'bg-purple-100 text-purple-800';
        } else if (roleLabel === 'Admin') {
            roleClass = 'bg-primary/20 text-primary';
        }

        roleEl.innerHTML = `
            <span class="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${roleClass}">
                ${roleLabel}
            </span>
        `;
    }

    // Tenant
    const tenantEl = document.getElementById('account-tenant');
    if (tenantEl) {
        tenantEl.textContent = user.tenant?.name || `Tenant ${user.tenant_id}`;
    }

    // Status
    const statusEl = document.getElementById('account-status');
    if (statusEl) {
        const isActive = user.is_active !== false;
        const statusClass = isActive ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-900';
        const statusText = isActive ? 'Active' : 'Inactive';
        statusEl.innerHTML = `
            <span class="inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${statusClass}">
                ${statusText}
            </span>
        `;
    }

    // User ID
    const userIdEl = document.getElementById('account-user-id');
    if (userIdEl) {
        userIdEl.textContent = user.id || '-';
    }

    // Created date
    const createdEl = document.getElementById('account-created');
    if (createdEl) {
        if (user.created_at) {
            const date = new Date(user.created_at);
            createdEl.textContent = date.toLocaleDateString('en-US', {
                year: 'numeric',
                month: 'long',
                day: 'numeric'
            });
        } else {
            createdEl.textContent = '-';
        }
    }
}

function populatePermissions(permissions) {
    const permissionsEl = document.getElementById('account-permissions');
    if (!permissionsEl) return;

    if (!permissions || permissions.length === 0) {
        permissionsEl.innerHTML = '<span class="text-gray-500">No specific permissions assigned</span>';
        return;
    }

    // Group permissions by resource type
    const grouped = {};
    permissions.forEach(p => {
        const [resourceType, capability] = p.name.includes(':')
            ? p.name.split(':')
            : ['general', p.name];

        if (!grouped[resourceType]) {
            grouped[resourceType] = [];
        }
        grouped[resourceType].push(capability);
    });

    // Render grouped permissions
    let html = '';
    Object.entries(grouped).forEach(([resourceType, capabilities]) => {
        capabilities.forEach(capability => {
            const displayName = `${resourceType}:${capability}`;
            html += `
                <span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-primary/10 text-primary border border-primary">
                    ${displayName}
                </span>
            `;
        });
    });

    permissionsEl.innerHTML = html;
}

function populateSessionInfo() {
    const tokenExpiryEl = document.getElementById('account-token-expiry');
    if (!tokenExpiryEl) return;

    // Try to decode the token to get expiry time
    const token = sessionStorage.getItem('accessToken');
    if (!token) {
        tokenExpiryEl.textContent = 'No active session';
        return;
    }

    try {
        // Decode JWT payload (base64)
        const payload = JSON.parse(atob(token.split('.')[1]));
        if (payload.exp) {
            const expiryDate = new Date(payload.exp * 1000);
            const now = new Date();

            if (expiryDate > now) {
                const diff = expiryDate - now;
                const minutes = Math.floor(diff / 60000);
                const hours = Math.floor(minutes / 60);

                if (hours > 0) {
                    tokenExpiryEl.textContent = `Expires in ${hours}h ${minutes % 60}m`;
                } else {
                    tokenExpiryEl.textContent = `Expires in ${minutes}m`;
                }
            } else {
                tokenExpiryEl.textContent = 'Session expired';
            }
        } else {
            tokenExpiryEl.textContent = '-';
        }
    } catch (e) {
        tokenExpiryEl.textContent = '-';
    }
}

// add-self-service-sso §6.8 + §6.9 — Linked sign-in methods card.
// Reveals the card only when the user has at least one federated
// identity, so non-SSO users aren't burdened with an empty section.
// Each row gets a Disconnect button gated by §6.9 (can_disconnect=
// false → button disabled + tooltip pointing at the password-set
// flow).
async function loadLinkedIdentities(paid = true) {
    const card = document.getElementById('sso-linked-methods-card');
    const list = document.getElementById('sso-linked-methods-list');
    if (!card || !list) return;
    if (!paid) { card.classList.add('hidden'); return; } // CE: sso_* router denied

    const token = sessionStorage.getItem('accessToken');
    if (!token) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

    try {
        const resp = await fetch(`${apiUrl}/sso/profile/linked-identities`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { card.classList.add('hidden'); return; }
        const data = await resp.json();
        const items = (data && data.identities) || [];
        if (items.length === 0) { card.classList.add('hidden'); return; }

        list.innerHTML = items.map(idp => `
            <div class="flex items-center justify-between p-3 border border-border rounded-lg">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 text-primary" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clip-rule="evenodd"/>
                    </svg>
                    <div>
                        <div class="text-sm font-medium text-foreground">${escapeHtml(idp.provider_name)}</div>
                        ${idp.linked_username ? `<div class="text-xs text-foreground/60">${escapeHtml(idp.linked_username)}</div>` : ''}
                    </div>
                </div>
                <button
                    class="text-xs ${idp.can_disconnect ? 'text-foreground hover:underline' : 'text-foreground/40 cursor-not-allowed'}"
                    data-alias="${escapeHtml(idp.idp_alias)}"
                    ${idp.can_disconnect ? '' : 'disabled title="Set a password before disconnecting your only sign-in method."'}>
                    Disconnect
                </button>
            </div>
        `).join('');
        card.classList.remove('hidden');

        list.querySelectorAll('button[data-alias]').forEach(btn => {
            if (btn.disabled) return;
            btn.addEventListener('click', async () => {
                const alias = btn.dataset.alias;
                if (!confirm(`Disconnect ${alias}? You can re-link it later by signing in via this provider.`)) return;
                try {
                    const dresp = await fetch(`${apiUrl}/sso/profile/disconnect-identity`, {
                        method: 'POST',
                        headers: {
                            'Authorization': `Bearer ${token}`,
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({ idp_alias: alias }),
                    });
                    if (!dresp.ok) {
                        const err = await dresp.json().catch(() => ({}));
                        const msg = err?.detail?.message || 'Could not disconnect.';
                        alert(msg);
                        return;
                    }
                    // Refresh list so the disconnected row vanishes
                    // and can_disconnect re-evaluates for the remaining
                    // identities (e.g. now-only identity becomes
                    // non-disconnectable).
                    loadLinkedIdentities();
                } catch (e) {
                    alert('Network error — try again.');
                }
            });
        });
    } catch (e) {
        card.classList.add('hidden');
    }
}

// activation-lifecycle-emails: load + wire the getting-started email toggle.
// The toggle reflects "opted IN" (checked = receive emails), while the API
// stores the inverse (lifecycle_emails_opted_out).
async function loadEmailPreferences(paid = true) {
    const toggle = document.getElementById('lifecycle-emails-toggle');
    const status = document.getElementById('lifecycle-emails-status');
    if (!toggle) return;
    if (!paid) {
        // CE: email_preferences router denied — hide the whole row, no fetch.
        const row = document.getElementById('lifecycle-emails-row');
        if (row) row.classList.add('hidden');
        if (status) status.classList.add('hidden');
        return;
    }
    try {
        const prefs = await me.getEmailPreferences();
        toggle.checked = !prefs.lifecycle_emails_opted_out;
    } catch (e) {
        if (status) status.textContent = 'Could not load email preferences.';
        return;
    }
    toggle.addEventListener('change', async () => {
        toggle.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
            await me.setEmailPreferences(!toggle.checked); // opted_out = NOT receiving
            if (status) status.textContent = toggle.checked
                ? 'Getting-started emails are on.'
                : 'Getting-started emails are off.';
        } catch (e) {
            toggle.checked = !toggle.checked; // revert on failure
            if (status) status.textContent = 'Could not save — try again.';
        } finally {
            toggle.disabled = false;
        }
    });
}

// notification-preferences (WS4.Q22): load + wire the in-app per-category
// toggles (workflow / billing / product). Each checkbox id is
// `notif-pref-<category>-in_app`; only the in_app channel has a UI control
// today (email is governed by the same backend but has no toggle yet).
const NOTIFICATION_PREF_CATEGORIES = ['workflow', 'billing', 'product'];

async function loadNotificationPreferences() {
    const status = document.getElementById('notification-prefs-status');
    const toggles = NOTIFICATION_PREF_CATEGORIES
        .map((category) => [category, document.getElementById(`notif-pref-${category}-in_app`)])
        .filter(([, el]) => !!el);
    if (toggles.length === 0) return;

    try {
        const prefs = await me.getNotificationPreferences();
        toggles.forEach(([category, el]) => {
            el.checked = !!prefs?.[category]?.in_app;
            el.dataset.loaded = '1'; // ready signal: GET applied, change listener wired below
        });
    } catch (e) {
        if (status) status.textContent = 'Could not load notification preferences.';
        return;
    }

    toggles.forEach(([category, el]) => {
        el.addEventListener('change', async () => {
            el.disabled = true;
            if (status) status.textContent = 'Saving…';
            try {
                await me.setNotificationPreferences({ [category]: { in_app: el.checked } });
                if (status) status.textContent = 'Notification preferences saved.';
            } catch (e) {
                el.checked = !el.checked; // revert on failure
                if (status) status.textContent = 'Could not save — try again.';
            } finally {
                el.disabled = false;
            }
        });
    });
}

// --- Edit profile (name fields via PUT /me/profile, username via PATCH /me) ---
function initProfileEdit(user) {
    const form = document.getElementById('profile-edit-form');
    if (!form) return;
    const firstEl = document.getElementById('profile-first-name');
    const lastEl = document.getElementById('profile-last-name');
    const userEl = document.getElementById('profile-username');
    const btn = document.getElementById('profile-save-btn');
    const status = document.getElementById('profile-edit-status');

    if (firstEl) firstEl.value = user.first_name || '';
    if (lastEl) lastEl.value = user.last_name || '';
    if (userEl) userEl.value = user.username || '';

    // Remember what was loaded so we only send fields that actually changed.
    let loaded = {
        first_name: user.first_name || '',
        last_name: user.last_name || '',
        username: user.username || '',
    };

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const first = (firstEl?.value || '').trim();
        const last = (lastEl?.value || '').trim();
        const username = (userEl?.value || '').trim();

        const nameChanged = first !== loaded.first_name || last !== loaded.last_name;
        const usernameChanged = username !== loaded.username;

        if (!nameChanged && !usernameChanged) {
            if (status) { status.className = 'text-sm text-gray-500'; status.textContent = 'Nothing changed yet. Edit a field above, then Save.'; }
            return;
        }
        if (usernameChanged && !username) {
            if (status) { status.className = 'text-sm text-red-700'; status.textContent = 'Username cannot be empty.'; }
            return;
        }

        if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
        if (status) { status.className = 'text-sm text-gray-500'; status.textContent = 'Saving…'; }
        try {
            if (nameChanged) await me.updateProfile(first, last);
            if (usernameChanged) await me.updateUsername(username);
            loaded = { first_name: first, last_name: last, username };
            // The display cards above read from the cached /me — refresh it.
            me.invalidate();
            try {
                const fresh = await me.get();
                populateAccountInfo(fresh);
            } catch (_) { /* display refresh is best-effort */ }
            if (status) { status.className = 'text-sm text-green-700'; status.textContent = 'Profile updated.'; }
        } catch (err) {
            if (status) {
                status.className = 'text-sm text-red-700';
                status.textContent = `Could not save: ${err?.message || 'please try again.'}`;
            }
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Save changes'; }
        }
    });
}

// --- Change password (GY.C4.MFA: MOD-branded action, never the Keycloak
// account console). The Keycloak-branded account console is not linked from
// MOD UI at all — this uses the application-initiated action on the MOD-themed
// sign-in page, which returns to the Dashboard with kc_action_status=success.
function initPasswordChange() {
    const btn = document.getElementById('change-password-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        try {
            const status = await me.getMfa();
            const url = await getKcActionUrl('UPDATE_PASSWORD',
                status?.change_password_url);
            window.open(url, '_blank', 'noopener');
        } catch (err) {
            showError(`Could not open the password change page: ${err?.message || 'please try again.'}`);
        }
    });
}

// --- Two-factor authentication (GY.C4.MFA) ---
// Status comes from GET /me/mfa; the buttons start the Keycloak
// application-initiated action (kc_action=CONFIGURE_TOTP) on the MOD-themed
// sign-in page. "Replace authenticator" is the same action: the themed page
// shows the existing device and asks for a new one, so the old device is
// replaced rather than the user being sent to a Keycloak console.
async function initSecurity() {
    const badge = document.getElementById('mfa-status-badge');
    const detail = document.getElementById('mfa-status-detail');
    const note = document.getElementById('mfa-status-note');
    const setupBtn = document.getElementById('mfa-setup-btn');
    const replaceBtn = document.getElementById('mfa-replace-btn');
    if (!badge || !detail) return;

    let status = null;
    try {
        status = await me.getMfa();
    } catch (err) {
        badge.textContent = 'unavailable';
        detail.textContent = `Could not load 2FA status: ${escapeHtml(err?.message || 'please try again.')}`;
        return;
    }

    if (status.exempt) {
        badge.textContent = 'not required';
        badge.className = 'px-2 py-0.5 text-xs rounded bg-gray-100 text-gray-600';
        detail.textContent = 'This is a machine/service account, so two-factor authentication is not required and no authenticator is queued for it.';
        if (setupBtn) setupBtn.disabled = true;
        if (replaceBtn) replaceBtn.disabled = true;
        return;
    }

    if (status.enrolled) {
        badge.textContent = 'on';
        badge.className = 'px-2 py-0.5 text-xs rounded bg-green-100 text-green-700';
        const label = status.device_label ? `Device: ${escapeHtml(status.device_label)}` : 'Authenticator enrolled';
        const created = status.credential_created_at
            ? ` · set up ${escapeHtml(String(status.credential_created_at).slice(0, 10))}` : '';
        detail.textContent = `Two-factor authentication is on. ${label}${created}.`;
        if (setupBtn) setupBtn.textContent = 'Set up 2FA';
        if (replaceBtn) replaceBtn.disabled = false;
    } else {
        badge.textContent = status.required ? 'required' : 'off';
        badge.className = status.required
            ? 'px-2 py-0.5 text-xs rounded bg-red-100 text-red-700'
            : 'px-2 py-0.5 text-xs rounded bg-yellow-100 text-yellow-700';
        detail.textContent = status.required
            ? 'Two-factor authentication is required for your account but is not enrolled yet. Set it up now, or it will be asked for on your next sign-in.'
            : 'Two-factor authentication is not enabled on your account yet. Set it up to protect sign-ins.';
        if (setupBtn) setupBtn.disabled = false;
        if (replaceBtn) replaceBtn.disabled = true;
    }

    if (note) {
        note.textContent = 'Setup happens on the MOD sign-in page and returns you here. MOD supports TOTP; it does not certify any specific framework compliance.';
    }

    const go = async () => {
        try {
            const url = await getMfaSetupUrl(status.setup_url);
            window.open(url, '_blank', 'noopener');
        } catch (err) {
            showError(`Could not start 2FA setup: ${err?.message || 'please try again.'}`);
        }
    };
    if (setupBtn) setupBtn.addEventListener('click', go);
    if (replaceBtn) replaceBtn.addEventListener('click', go);
    renderSecurityKeys(status);
}

// --- Security keys (GY.C3b — WebAuthn, phishing-resistant) ---
// Registered keys come from GET /me/mfa (security_keys: label + created
// only). "Add security key" starts the MOD-branded WEBAUTHN_REGISTER
// application-initiated action, so the whole registration happens on the
// MOD sign-in page — never a Keycloak-branded page.
async function renderSecurityKeys(status) {
    const badge = document.getElementById('webauthn-status-badge');
    const detail = document.getElementById('webauthn-status-detail');
    const list = document.getElementById('webauthn-keys-list');
    const addBtn = document.getElementById('webauthn-add-btn');
    if (!badge || !detail || !status) return;

    if (status.exempt) {
        badge.textContent = 'not available';
        detail.textContent = 'Security keys are not available on machine/service accounts.';
        if (list) list.innerHTML = '';
        if (addBtn) addBtn.disabled = true;
        return;
    }

    const keys = Array.isArray(status.security_keys) ? status.security_keys : [];
    if (status.webauthn_registered || keys.length > 0) {
        badge.textContent = 'on';
        badge.className = 'px-2 py-0.5 text-xs rounded bg-green-100 text-green-700';
        detail.textContent = `${keys.length} security key${keys.length === 1 ? '' : 's'} registered. Each one can be used to sign in.`;
        if (list) {
            list.innerHTML = keys.map(k => `
                <li class="mb-1">
                    <span class="font-medium text-gray-700">${escapeHtml(k.device_label || 'Security key')}</span>
                    <span class="text-xs text-gray-500">${k.created_at ? ` · added ${escapeHtml(String(k.created_at).slice(0, 10))}` : ''}</span>
                </li>`).join('');
        }
        if (addBtn) addBtn.disabled = false;
    } else {
        badge.textContent = 'off';
        badge.className = 'px-2 py-0.5 text-xs rounded bg-yellow-100 text-yellow-700';
        detail.textContent = 'No security key yet. Adding one gives you a phishing-proof way to sign in.';
        if (list) list.innerHTML = '';
        if (addBtn) addBtn.disabled = false;
    }

    if (addBtn) addBtn.addEventListener('click', async () => {
        try {
            const url = await getKcActionUrl('WEBAUTHN_REGISTER',
                status?.register_security_key_url);
            window.open(url, '_blank', 'noopener');
        } catch (err) {
            showError(`Could not start security key setup: ${err?.message || 'please try again.'}`);
        }
    });
}

// --- Active sessions (GET /me/sessions, DELETE /me/sessions[/{id}]) ---
async function loadSessions() {
    const list = document.getElementById('sessions-list');
    const revokeBtn = document.getElementById('revoke-other-sessions-btn');
    const status = document.getElementById('sessions-status');
    if (!list) return;

    // Which session is "this" one — the sessions endpoint marks the current
    // session with is_current when available; fall back to none.
    try {
        const sessions = await me.getSessions();
        renderSessions(sessions);
    } catch (err) {
        list.innerHTML = `<span class="text-sm text-red-700">Could not load sessions: ${escapeHtml(err?.message || 'please try again.')}</span>`;
        if (revokeBtn) revokeBtn.disabled = true;
        return;
    }

    if (revokeBtn && !revokeBtn.dataset.wired) {
        revokeBtn.dataset.wired = '1';
        revokeBtn.addEventListener('click', async () => {
            if (!confirm('Log out of every other session? Other devices and browsers will need to sign in again.')) return;
            revokeBtn.disabled = true;
            if (status) { status.className = 'text-sm text-gray-500'; status.textContent = 'Logging out other sessions…'; }
            try {
                const res = await me.revokeOtherSessions();
                const n = (res && typeof res.revoked === 'number') ? res.revoked : 0;
                if (status) {
                    status.className = 'text-sm text-green-700';
                    status.textContent = n > 0
                        ? `Logged out ${n} other session${n === 1 ? '' : 's'}.`
                        : 'No other sessions were active.';
                }
                await loadSessions();
            } catch (err) {
                if (status) {
                    status.className = 'text-sm text-red-700';
                    status.textContent = `Could not log out other sessions: ${err?.message || 'please try again.'}`;
                }
            } finally {
                revokeBtn.disabled = false;
            }
        });
    }
}

function renderSessions(sessions) {
    const list = document.getElementById('sessions-list');
    const revokeBtn = document.getElementById('revoke-other-sessions-btn');
    if (!list) return;
    const items = Array.isArray(sessions) ? sessions : [];
    if (items.length === 0) {
        list.innerHTML = '<span class="text-sm text-gray-500">No active sessions found.</span>';
        if (revokeBtn) revokeBtn.disabled = true;
        return;
    }
    if (revokeBtn) revokeBtn.disabled = items.length < 2;

    list.innerHTML = items.map(s => {
        const device = shortenDevice(s.device_info) || 'Unknown device';
        const ip = s.ip_address || '—';
        const last = s.last_active_at ? formatSessionTime(s.last_active_at) : '—';
        return `
            <div class="flex items-center justify-between p-3 border border-gray-200 rounded-lg">
                <div class="min-w-0">
                    <div class="text-sm font-medium text-gray-900 truncate">${escapeHtml(device)}</div>
                    <div class="text-xs text-gray-500">IP ${escapeHtml(ip)} · Last active ${escapeHtml(last)}</div>
                </div>
            </div>
        `;
    }).join('');
}

function shortenDevice(ua) {
    if (!ua) return '';
    let os = '';
    if (/Windows/i.test(ua)) os = 'Windows';
    else if (/Mac OS X|Macintosh/i.test(ua)) os = 'macOS';
    else if (/Android/i.test(ua)) os = 'Android';
    else if (/iPhone|iPad|iOS/i.test(ua)) os = 'iOS';
    else if (/Linux/i.test(ua)) os = 'Linux';
    let browser = '';
    if (/Edg\//i.test(ua)) browser = 'Edge';
    else if (/Chrome\//i.test(ua)) browser = 'Chrome';
    else if (/Firefox\//i.test(ua)) browser = 'Firefox';
    else if (/Safari\//i.test(ua)) browser = 'Safari';
    const parts = [browser, os].filter(Boolean);
    return parts.length ? parts.join(' on ') : ua.slice(0, 60);
}

function formatSessionTime(iso) {
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '—';
        return d.toLocaleString('en-US', {
            month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
        });
    } catch (_) { return '—'; }
}

// --- Data & privacy (GDPR export + account deletion) ---
function initDataPrivacy() {
    const exportBtn = document.getElementById('export-data-btn');
    const exportStatus = document.getElementById('export-data-status');
    if (exportBtn && !exportBtn.dataset.wired) {
        exportBtn.dataset.wired = '1';
        exportBtn.addEventListener('click', () => withButtonBusy(exportBtn, async () => {
            if (exportStatus) { exportStatus.className = 'text-sm text-gray-500'; exportStatus.textContent = 'Preparing your export…'; }
            try {
                const data = await me.exportData();
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `mod-data-export-${new Date().toISOString().slice(0, 10)}.json`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
                if (exportStatus) { exportStatus.className = 'text-sm text-green-700'; exportStatus.textContent = 'Your data export has been downloaded.'; }
            } catch (err) {
                if (exportStatus) {
                    exportStatus.className = 'text-sm text-red-700';
                    exportStatus.textContent = `Could not export your data: ${err?.message || 'please try again.'}`;
                }
            }
        }, { busyLabel: 'Exporting…' }));
    }

    initDeleteAccount();
}

function initDeleteAccount() {
    const openBtn = document.getElementById('delete-account-btn');
    const modal = document.getElementById('delete-account-modal');
    const cancelBtn = document.getElementById('delete-account-cancel-btn');
    const confirmBtn = document.getElementById('delete-account-confirm-btn');
    const input = document.getElementById('delete-account-confirm-input');
    const modalStatus = document.getElementById('delete-account-modal-status');
    const pageStatus = document.getElementById('delete-account-status');
    if (!openBtn || !modal || !confirmBtn || !input) return;

    const close = () => {
        modal.classList.add('hidden');
        input.value = '';
        confirmBtn.disabled = true;
        if (modalStatus) modalStatus.textContent = '';
    };

    openBtn.addEventListener('click', () => {
        modal.classList.remove('hidden');
        input.value = '';
        confirmBtn.disabled = true;
        if (modalStatus) modalStatus.textContent = '';
        setTimeout(() => input.focus(), 50);
    });
    cancelBtn?.addEventListener('click', close);
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    input.addEventListener('input', () => {
        confirmBtn.disabled = input.value.trim().toUpperCase() !== 'DELETE';
    });

    confirmBtn.addEventListener('click', async () => {
        if (input.value.trim().toUpperCase() !== 'DELETE') return;
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Deleting…';
        if (modalStatus) { modalStatus.className = 'text-sm text-gray-500 mb-3'; modalStatus.textContent = 'Requesting account deletion…'; }
        try {
            const res = await me.deleteAccount();
            close();
            if (pageStatus) {
                pageStatus.className = 'text-sm text-green-700';
                pageStatus.textContent = (res && res.message)
                    || 'Your account has been deactivated and your data anonymized.';
            }
            openBtn.disabled = true;
            // The account is now deactivated — sign the user out shortly so
            // they aren't left on a dead session.
            setTimeout(() => {
                try { window.location.hash = '#login'; } catch (_) {}
                try { sessionStorage.clear(); } catch (_) {}
                window.location.reload();
            }, 4000);
        } catch (err) {
            if (modalStatus) {
                modalStatus.className = 'text-sm text-red-700 mb-3';
                modalStatus.textContent = `Could not delete account: ${err?.message || 'please try again.'}`;
            }
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'Delete account';
        }
    });
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
}

// --- Organization settings (Bug #2) — owner / tenant-admin only ---
// The card is hidden by default. We only attempt to load it for tenant admins
// (super admins / platform admins). The backend gates on TENANT:view_settings /
// TENANT:manage_settings, so a 403 simply leaves the card hidden.
async function initOrgSettings(user, paid = true) {
    const card = document.getElementById('org-settings-card');
    if (!card) return;
    if (!paid) { card.classList.add('hidden'); return; } // CE: tenant/* denied

    const isAdmin = user.parent_user_id === null || user.is_platform_admin === true;
    if (!isAdmin) return;

    const token = sessionStorage.getItem('accessToken');
    if (!token) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

    const nameInput = document.getElementById('org-name');
    const emailInput = document.getElementById('org-billing-email');
    const storageEl = document.getElementById('org-default-storage');
    const form = document.getElementById('org-settings-form');
    const saveBtn = document.getElementById('org-settings-save-btn');
    const statusEl = document.getElementById('org-settings-status');

    function renderStorage(ds) {
        if (!storageEl) return;
        if (!ds) { storageEl.textContent = '—'; return; }
        if (ds.type === 'byo') {
            const ok = ds.last_test_ok ? '' : ' (connection test not passing)';
            storageEl.textContent = `${ds.name} — ${ds.bucket} @ ${ds.endpoint}${ok}`;
        } else {
            storageEl.textContent = `MOD default storage (${ds.bucket})`;
        }
    }

    try {
        const resp = await fetch(`${apiUrl}/tenant/settings`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { card.classList.add('hidden'); return; }
        const data = await resp.json();
        if (nameInput) nameInput.value = data.name || '';
        if (emailInput) emailInput.value = data.billing_email || '';
        renderStorage(data.default_storage_connection);
        card.classList.remove('hidden');
    } catch (e) {
        card.classList.add('hidden');
        return;
    }

    if (form && !form.dataset.wired) {
        form.dataset.wired = '1';
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const setStatus = (msg, cls) => {
                if (statusEl) { statusEl.className = `text-sm ${cls}`; statusEl.textContent = msg; }
            };
            const body = {
                name: (nameInput?.value || '').trim(),
                billing_email: (emailInput?.value || '').trim(),
            };
            if (!body.name) { setStatus('Organization name cannot be empty.', 'text-red-700'); return; }
            await withButtonBusy(saveBtn, async () => {
                setStatus('Saving…', 'text-gray-500');
                try {
                    const r = await fetch(`${apiUrl}/tenant/settings`, {
                        method: 'PUT',
                        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    if (!r.ok) {
                        let detail = `HTTP ${r.status}`;
                        try { const b = await r.json(); detail = b.detail || detail; } catch {}
                        setStatus(`Could not save: ${detail}`, 'text-red-700');
                        return;
                    }
                    const updated = await r.json();
                    if (nameInput) nameInput.value = updated.name || '';
                    if (emailInput) emailInput.value = updated.billing_email || '';
                    renderStorage(updated.default_storage_connection);
                    setStatus('Saved.', 'text-green-700');
                } catch (err) {
                    setStatus(`Could not save: ${err?.message || 'please try again.'}`, 'text-red-700');
                }
            });
        });
    }
}

// FX.89 p3 — tenant security contact card (My Account page).
// Backend: routers/tenant/security_contact.py — GET /tenant/security-contact
// gates on TENANT:view_settings, PUT on TENANT:manage_settings; the DTO is
// {email, phone, secondary_email, confirm} and the response block is
// {security_contact_email, security_contact_phone, security_contact_secondary_email,
//  security_contact_confirmed_at}. Non-admins never see the card (hidden),
// mirroring the backend authz; tenant admins without manage_settings get a
// read-only card (Save disabled).
// Client-side validation runs BEFORE the PUT (email shape, phone shape,
// primary required); when the server still rejects (422 Pydantic detail
// array, 400 ServiceError string), the server's own detail is surfaced
// verbatim in the status line. The missing-contact banner is worded "Planned:"
// — licence-activation gating is not enforced server-side yet, so we do not
// claim it is.
const _SC_EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[A-Za-z][A-Za-z0-9.-]*$/;
const _SC_PHONE_RE = /^\+?[0-9][0-9 ()\-.]{4,31}$/;
const SC_RECONFIRM_DAYS = 182; // mirrors services.tenant_security_contact_service.RECONFIRM_INTERVAL_DAYS

function _formatScError(err) {
    // Verbatim server detail: string as-is; FastAPI 422 arrays joined as
    // "field: msg" pairs (same shape as users.js _formatApiError).
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

async function initSecurityContact(user, permissions, paid = true) {
    const card = document.getElementById('security-contact-card');
    if (!card) return;
    if (!paid) { card.classList.add('hidden'); return; } // CE: tenant/* denied
    // Mirror the backend gate: tenant admins (super admins) and platform
    // admins can view; everyone else gets nothing (the card stays hidden).
    const isAdmin = user.parent_user_id === null || user.is_platform_admin === true;
    const caps = permissions || [];
    const hasCap = (name) => caps.some(p => p && p.name === name);
    const canView = isAdmin || hasCap('tenant:view_settings');
    const canManage = isAdmin || hasCap('tenant:manage_settings');
    if (!canView) return;

    let data;
    try {
        data = await securityContact.get();
    } catch (err) {
        return; // 403/404 — leave the card hidden, same as the org-settings card
    }

    const primaryInput = document.getElementById('sc-primary-email');
    const phoneInput = document.getElementById('sc-phone');
    const secondaryInput = document.getElementById('sc-secondary-email');
    const bannerEl = document.getElementById('security-contact-banner');
    const confirmedEl = document.getElementById('security-contact-confirmed');
    const statusEl = document.getElementById('security-contact-status');
    const saveBtn = document.getElementById('security-contact-save-btn');
    const confirmBtn = document.getElementById('security-contact-confirm-btn');
    const form = document.getElementById('security-contact-form');

    function setStatus(msg, cls) {
        if (statusEl) { statusEl.className = `text-sm ${cls}`; statusEl.textContent = msg || ''; }
    }

    function renderContact(d) {
        if (primaryInput) primaryInput.value = d.security_contact_email || '';
        if (phoneInput) phoneInput.value = d.security_contact_phone || '';
        if (secondaryInput) secondaryInput.value = d.security_contact_secondary_email || '';
        if (bannerEl) {
            if (!d.security_contact_email) {
                bannerEl.className = 'text-sm text-orange-800 bg-orange-50 rounded-md px-3 py-2';
                bannerEl.textContent = 'No security contact set. Until one is set, MOD falls back to notifying all tenant admins for breach, subprocessor and SIEM security alerts.';
            } else {
                bannerEl.className = 'text-sm text-gray-600';
                bannerEl.textContent = '';
            }
        }
        if (confirmedEl) {
            const stamp = d.security_contact_confirmed_at;
            const stale = !stamp || (Date.now() - Date.parse(stamp)) / 86400000 > SC_RECONFIRM_DAYS;
            if (d.security_contact_email && stale) {
                confirmedEl.textContent = `Not re-confirmed in the last 6 months (last: ${stamp ? stamp.slice(0, 10) : 'never'}) — please use Re-confirm.`;
            } else if (d.security_contact_email && stamp) {
                confirmedEl.textContent = `Confirmed ${stamp.slice(0, 10)}.`;
            } else {
                confirmedEl.textContent = '';
            }
        }
    }

    renderContact(data || {});
    card.classList.remove('hidden');
    if (!canManage && saveBtn) saveBtn.disabled = true;
    if (!canManage && confirmBtn) confirmBtn.disabled = true;

    if (form && !form.dataset.wired) {
        form.dataset.wired = '1';
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const email = (primaryInput?.value || '').trim();
            const phone = (phoneInput?.value || '').trim();
            const secondary = (secondaryInput?.value || '').trim();
            // Client-side validation first: primary required, emails shaped,
            // phone shaped (empty phone clears the field, empty secondary = no change).
            if (!email) { setStatus('Primary email is required before saving.', 'text-red-700'); return; }
            if (!_SC_EMAIL_RE.test(email)) { setStatus('Primary email is not a valid email address.', 'text-red-700'); return; }
            if (secondary && !_SC_EMAIL_RE.test(secondary)) { setStatus('Secondary email is not a valid email address.', 'text-red-700'); return; }
            if (phone && !_SC_PHONE_RE.test(phone)) { setStatus('Phone must be digits (spaces, (), - or . allowed, optional leading +).', 'text-red-700'); return; }
            setStatus('Saving…', 'text-gray-500');
            await withButtonBusy(saveBtn, async () => {
                try {
                    const updated = await securityContact.update({
                        email,
                        phone,
                        secondary_email: secondary || null,
                    });
                    renderContact(updated || data || {});
                    setStatus('Saved.', 'text-green-700');
                    if (window.showInfo) window.showInfo('Security contact saved.', 'Security contact');
                } catch (err) {
                    // Server 422/400 detail verbatim.
                    setStatus(`Could not save: ${_formatScError(err)}`, 'text-red-700');
                }
            });
        });
    }

    if (confirmBtn && !confirmBtn.dataset.wired) {
        confirmBtn.dataset.wired = '1';
        confirmBtn.addEventListener('click', async () => {
            await withButtonBusy(confirmBtn, async () => {
                try {
                    const updated = await securityContact.update({ confirm: true });
                    renderContact(updated || data || {});
                    setStatus('Re-confirmed.', 'text-green-700');
                    if (window.showInfo) window.showInfo('Security contact re-confirmed.', 'Security contact');
                } catch (err) {
                    setStatus(`Could not re-confirm: ${_formatScError(err)}`, 'text-red-700');
                }
            });
        });
    }
}

function getInitials(name) {
    if (!name) return '?';
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
        return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return name.substring(0, 2).toUpperCase();
}

function showError(message) {
    const pageContent = document.getElementById('page-content');
    if (pageContent) {
        pageContent.innerHTML = `
            <div class="flex items-center justify-center min-h-[400px]">
                <div class="bg-red-50 border border-red-200 rounded-lg p-8 max-w-md">
                    <div class="flex items-center mb-4">
                        <svg class="w-8 h-8 text-red-600 mr-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
                        </svg>
                        <h2 class="text-xl font-bold text-red-800">Error</h2>
                    </div>
                    <p class="text-red-700">${message}</p>
                </div>
            </div>
        `;
    }
}
