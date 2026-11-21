import { invitations, cogs, rbac, externalInvites, isPaidEdition } from './api.js';
import { showCopyableModal } from './modal.js';

export async function loadInvitations() {
    try {
        // Check if platform admin has selected a tenant
        const tenantSelect = document.getElementById('invitation-tenant');
        const selectedTenantId = tenantSelect && tenantSelect.value ? parseInt(tenantSelect.value, 10) : null;

        const invs = await invitations.get(selectedTenantId);
        renderInvitations(invs);
    } catch (error) {
        console.error("Error loading invitations:", error);
    }
}

function renderInvitations(invs) {
    const invitationsTableBody = document.querySelector("#invitations-table tbody");
    if (!invitationsTableBody) return;

    invitationsTableBody.innerHTML = '';
    if (invs.length === 0) {
        invitationsTableBody.innerHTML = `<tr><td colspan="3" class="text-center text-gray-500 py-4">No pending invitations.</td></tr>`;
        return;
    }
    invs.forEach(invitation => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td class="border-t px-4 py-2">${invitation.email}</td>
            <td class="border-t px-4 py-2">${new Date(invitation.expires_at).toLocaleString()}</td>
            <td class="border-t px-4 py-2 text-right">
                <button class="cancel-invitation-button text-red-600 hover:text-red-800" data-id="${invitation.id}">Cancel</button>
            </td>
        `;
        invitationsTableBody.appendChild(row);
    });
}

async function handleCancelInvitation(event) {
    const invitationId = event.target.getAttribute('data-id');
    if (await showConfirm('Are you sure you want to cancel this invitation?')) {
        try {
            await invitations.cancel(invitationId);
            loadInvitations(); // Refresh the list
        } catch (error) {
            console.error("Error canceling invitation:", error);
            showError("Failed to cancel invitation.");
        }
    }
}

async function handleSendInvitation(event) {
    event.preventDefault();
    const email = document.getElementById('invitation-email').value;
    const templateId = document.getElementById('invitation-template').value;
    const customRoleId = document.getElementById('invitation-role').value;
    const tenantSelect = document.getElementById('invitation-tenant');
    // LDF-3 §11.3 fix — the tenant container is `display:none`'d for
    // tenant admins, but the <select> element itself stays in the DOM,
    // so a naive `if (tenantSelect && !tenantId)` would block tenant
    // admins from ever submitting (Tim's #19 regression). Treat the
    // picker as "not in play" when its container is hidden, and only
    // require a selection when the picker is actually visible.
    const tenantContainer = document.getElementById('invitation-tenant-container');
    const isPickerVisible = tenantContainer && tenantContainer.style.display !== 'none' &&
                            !tenantContainer.classList.contains('hidden');
    const tenantId = (isPickerVisible && tenantSelect) ? tenantSelect.value : null;

    const selectors = document.querySelectorAll('.cog-dropdown');
    const lastSelector = selectors[selectors.length - 1];
    const cogId = lastSelector ? lastSelector.value : "";

    if (!customRoleId) {
        showInfo("Please select a role to assign to the user.");
        return;
    }

    // Only enforce tenant selection when the picker is visible (i.e.
    // caller is a platform admin). Tenant admins fall through with
    // tenantId=null; the API uses their own tenant_id.
    if (isPickerVisible && !tenantId) {
        showInfo("Please select a tenant for this invitation.");
        return;
    }

    try {
        // Build the request based on whether a specific folder/file was chosen.
        const payload = {
            email: email,
            custom_role_id: parseInt(customRoleId, 10)
        };

        // Add tenant_id for platform admins
        if (tenantId) {
            payload.tenant_id = parseInt(tenantId, 10);
        }

        // Only add folder/file permissions if both cog AND template are selected
        if (cogId && templateId) {
            payload.cog_id = parseInt(cogId, 10);
            payload.template_id = parseInt(templateId, 10);
        }

        const created = await invitations.create(payload);

        // 2400LR-BUG-1027b — CE team invites: SMTP is optional/off, so the
        // invitee needs the accept link handed to the admin directly. Older
        // API responses have no accept_link -> keep today's toast only.
        if (created && created.accept_link) {
            showTeamInviteAcceptLink(created.email, created.accept_link, created.email_delivered);
        }

        // FIX-INVITATIONS-EMAIL-DELIVERED-FIELD (REV 7ffe9dff26 option 1): the
        // FIX-DASH-INVITATIONS-NO-EMAIL-TOAST gate above (`created.accept_link &&
        // !await isPaidEdition()`) LIED on two paths — (F-A) CE WITH mail configured:
        // an email WAS queued (invitations_service.py:436 `if not ce_no_mail`) yet the
        // toast claimed none was sent; (F-B) paid with an unreadable /platform/features
        // payload: api.js isPaidEdition() returns false for unknown entitlements too,
        // so the new toast fired on paid where ce_no_mail is always False (email always
        // sent). The create response now carries the honest signal: schemas.py
        // `InvitationCreated.email_delivered` (True = queued, False = skipped, None =
        // older API). Gate on STRICT false only, so None/undefined/true keep the
        // original toast byte-for-byte and the isPaidEdition() dependency is dropped
        // from the toast entirely (the external-card guard at :490 still uses it).
        if (created && created.email_delivered === false && created.accept_link) {
            showError('Invitation created. No email was sent: copy the link below and send it yourself.', 'E-mail sending is not available');
        } else {
            showInfo('Invitation sent successfully!');
        }
        loadInvitations();
        document.getElementById('invitation-form').reset();
        initializeCogSelector();
    } catch (error) {
        console.error("Error sending invitation:", error);
        // Use copyable modal for configuration errors
        showCopyableModal(
            'Failed to Send Invitation',
            error.message || 'An unexpected error occurred'
        );
    }
}

export async function initializeCogSelector() {
    const cogSelectorContainer = document.getElementById("cog-selector-container");
    if (!cogSelectorContainer) return;

    cogSelectorContainer.innerHTML = '<label class="block text-sm font-medium text-gray-700">Grant Initial Access To (Optional)</label>'; // Reset but keep label
    await createCogDropdown(null, 0);
}

async function createCogDropdown(parentId, level) {
    const cogSelectorContainer = document.getElementById("cog-selector-container");
    if (!cogSelectorContainer) return;

    try {
        const cogsResult = await cogs.get(parentId);
        const select = document.createElement('select');
        select.className = 'cog-dropdown mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm';
        select.dataset.level = level;

        // This change turns the default option into "All"
        const placeholder = document.createElement('option');
        placeholder.value = ''; // An empty value represents "All"
        placeholder.textContent = 'All';
        select.appendChild(placeholder);

        cogsResult.forEach(cog => {
            const option = document.createElement('option');
            option.value = cog.id;
            option.textContent = cog.name;
            option.dataset.isFolder = cog.cog_type.includes('folder');
            select.appendChild(option);
        });

        select.addEventListener('change', (e) => {
            const selectedOption = e.target.options[e.target.selectedIndex];
            const nextLevel = level + 1;
            document.querySelectorAll(`.cog-dropdown`).forEach(dropdown => {
                if (parseInt(dropdown.dataset.level) >= nextLevel) dropdown.remove();
            });
            if (selectedOption.dataset.isFolder === 'true' && selectedOption.value) {
                createCogDropdown(selectedOption.value, nextLevel);
            }
        });
        cogSelectorContainer.appendChild(select);
    } catch (error) {
        console.error("Error creating cog dropdown:", error);
    }
}

async function loadRolesForInvitation(tenantId = null) {
    const roleSelector = document.getElementById('invitation-role');
    if (!roleSelector) return;
    try {
        // For platform admins, pass tenant_id query parameter
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const url = tenantId
            ? `${apiUrl}/rbac/roles?tenant_id=${tenantId}`
            : `${apiUrl}/rbac/roles`;

        console.log("Loading roles from:", url);

        const response = await fetch(url, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) throw new Error(`Failed to load roles: ${response.status}`);

        const roles = await response.json();
        console.log("Loaded roles:", roles);

        if (roles.length === 0) {
            roleSelector.innerHTML = '<option value="">No roles are available yet. Reload the page; if this persists, contact your administrator.</option>';
            console.warn("No roles found for tenant; the built-in roles are assigned by the administrator.");
            return;
        }

        roleSelector.innerHTML = '<option value="">Select a role...</option>'; // Placeholder
        roles.forEach(role => {
            const option = document.createElement('option');
            option.value = role.id;
            option.textContent = role.name;
            roleSelector.appendChild(option);
        });
    } catch (error) {
        console.error("Error loading roles for invitation:", error);
        roleSelector.innerHTML = '<option value="">Failed to load roles</option>';
    }
}

async function loadPermissionTemplates() {
    const templateSelector = document.getElementById('invitation-template');
    if (!templateSelector) return;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/permissions/templates`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });
        if (!response.ok) throw new Error('Failed to load permission templates');

        const templates = await response.json();
        templateSelector.innerHTML = '<option value="">None - No initial folder/file access</option>';
        templates.forEach(template => {
            const option = document.createElement('option');
            option.value = template.id;
            option.textContent = `${template.name} (${template.permission_level}${template.duration_days ? ', ' + template.duration_days + ' days' : ''})`;
            option.dataset.description = template.description;
            templateSelector.appendChild(option);
        });
    } catch (error) {
        console.error("Error loading permission templates:", error);
    }
}

async function loadTenantsForPlatformAdmin() {
    // Check if user is platform admin by attempting to fetch tenants
    const tenantSelectorContainer = document.getElementById('invitation-tenant-container');
    if (!tenantSelectorContainer) {
        console.log("Tenant selector container not found in DOM");
        return;
    }

    // CE hide: platform-admin tenant select is PAID-only (mirrors external-invites pattern at :484)
    if (!await isPaidEdition()) {
        tenantSelectorContainer.style.display = 'none';
        return;
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        console.log("Attempting to fetch tenants from:", `${apiUrl}/tenants`);

        const response = await fetch(`${apiUrl}/tenants`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        console.log("Tenants API response status:", response.status);

        if (!response.ok) {
            // Not a platform admin, hide the tenant selector
            console.log("User is not platform admin (403) or API error - hiding tenant selector");
            tenantSelectorContainer.style.display = 'none';
            return;
        }

        const tenants = await response.json();
        console.log("Loaded tenants:", tenants);

        tenantSelectorContainer.style.display = 'block';
        console.log("Showing tenant selector for platform admin");

        // BUG.4: re-lookup after the await; the route may have changed.
        const tenantSelector = document.getElementById('invitation-tenant');
        if (!tenantSelector) return;
        tenantSelector.innerHTML = '<option value="">Select a tenant...</option>';
        tenants.forEach(tenant => {
            const option = document.createElement('option');
            option.value = tenant.id;
            option.textContent = `${tenant.name} (ID: ${tenant.id})`;
            tenantSelector.appendChild(option);
        });

        // Add event listener to reload roles and invitations when tenant changes
        tenantSelector.addEventListener('change', (e) => {
            const selectedTenantId = e.target.value;
            console.log("Tenant selected:", selectedTenantId);
            if (selectedTenantId) {
                loadRolesForInvitation(parseInt(selectedTenantId, 10));
                loadInvitations(); // Reload invitations for selected tenant
            } else {
                // Clear roles if no tenant selected
                const roleSelector = document.getElementById('invitation-role');
                if (roleSelector) {
                    roleSelector.innerHTML = '<option value="">Select a tenant first</option>';
                }
                loadInvitations(); // Reload invitations (will show platform admin's own tenant)
            }
        });
    } catch (error) {
        console.error("Error loading tenants:", error);
        tenantSelectorContainer.style.display = 'none';
    }
}

// ============================================================================
// External Invitations (external-invite-accept-ui)
// ----------------------------------------------------------------------------
// Invite someone outside the tenant to a single folder. On create the API
// returns an `invitation_token`; SMTP is off, so we surface a copyable accept
// link (/accept-invite?token=<token>) — that link is how the invitee gets in.
// ============================================================================

async function loadExternalCogOptions() {
    const select = document.getElementById('external-invite-cog');
    if (!select) return;
    try {
        const items = await cogs.get();
        const folders = (Array.isArray(items) ? items : []).filter(
            c => c.cog_type && c.cog_type.includes('folder'));
        if (!folders.length) {
            select.innerHTML = '<option value="">No folders available — create a folder first</option>';
            return;
        }
        select.innerHTML = '<option value="">Select a folder…</option>';
        folders.forEach(folder => {
            const opt = document.createElement('option');
            opt.value = folder.id;
            opt.textContent = folder.name;
            select.appendChild(opt);
        });
    } catch (error) {
        console.error('Error loading folders for external invite:', error);
        select.innerHTML = '<option value="">Failed to load folders</option>';
    }
}

export const NO_EMAIL_NOTE_PREFIX = 'Invitation created for ';
export const NO_EMAIL_NOTE_SUFFIX = '. E-mail sending is not available on this install, so no message was sent: copy this link and send it yourself.';

export function showTeamInviteAcceptLink(email, acceptLink, emailDelivered) {
    const result = document.getElementById('team-invite-result');
    const emailEl = document.getElementById('team-invite-result-email');
    const linkEl = document.getElementById('team-invite-link');
    if (!result || !linkEl) return;
    // M2: server/user strings reach the DOM as text only (textContent / .value).
    if (emailEl) emailEl.textContent = email;
    // Strict false only (API field email_delivered): no e-mail system / nothing sent.
    // true / undefined keep the original callout wording unchanged.
    const noEmailNote = document.getElementById('team-invite-no-email-note');
    const sentLine = emailEl ? emailEl.parentElement : null;
    if (noEmailNote) {
        const noEmail = emailDelivered === false;
        noEmailNote.textContent = noEmail ? `${NO_EMAIL_NOTE_PREFIX}${email}${NO_EMAIL_NOTE_SUFFIX}` : '';
        noEmailNote.classList.toggle('hidden', !noEmail);
        if (sentLine) sentLine.classList.toggle('hidden', noEmail);
    }
    linkEl.value = acceptLink;
    result.classList.remove('hidden');
    result.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function showExternalAcceptLink(email, token) {
    const result = document.getElementById('external-invite-result');
    const emailEl = document.getElementById('external-invite-result-email');
    const linkEl = document.getElementById('external-invite-link');
    if (!result || !linkEl) return;
    const link = externalInvites.acceptLink(token);
    if (emailEl) emailEl.textContent = email;
    linkEl.value = link;
    result.classList.remove('hidden');
    result.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export async function copyToClipboard(text) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (_) { /* fall through to legacy path */ }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch (_) {
        return false;
    }
}

async function handleCreateExternalInvite(event) {
    event.preventDefault();
    const email = document.getElementById('external-invite-email').value.trim();
    const cogId = document.getElementById('external-invite-cog').value;
    const level = document.getElementById('external-invite-level').value;
    const expiryRaw = document.getElementById('external-invite-expiry').value;

    if (!email) { showInfo('Please enter an email address.'); return; }
    if (!cogId) { showInfo('Please select a folder to grant access to.'); return; }

    const payload = {
        email,
        cog_id: parseInt(cogId, 10),
        permission_level: level || 'viewer',
    };
    const expiresInDays = parseInt(expiryRaw, 10);
    if (Number.isFinite(expiresInDays) && expiresInDays > 0) {
        payload.expires_in_days = expiresInDays;
    }

    try {
        const invite = await externalInvites.create(payload);
        showExternalAcceptLink(invite.email, invite.invitation_token);
        document.getElementById('external-invite-form').reset();
        document.getElementById('external-invite-expiry').value = 30;
        loadExternalInvitations();
    } catch (error) {
        console.error('Error creating external invitation:', error);
        showCopyableModal('Failed to Create External Invite', error.message || 'An unexpected error occurred');
    }
}

async function loadExternalInvitations() {
    const tbody = document.querySelector('#external-invitations-table tbody');
    if (!tbody) return;
    try {
        const invs = await externalInvites.listPending();
        renderExternalInvitations(Array.isArray(invs) ? invs : []);
    } catch (error) {
        console.error('Error loading external invitations:', error);
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-gray-500 py-4">Failed to load external invitations.</td></tr>';
    }
}

function renderExternalInvitations(invs) {
    const tbody = document.querySelector('#external-invitations-table tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!invs.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-gray-500 py-4">No pending external invitations.</td></tr>';
        return;
    }
    invs.forEach(inv => {
        const row = document.createElement('tr');
        const expires = inv.expires_at ? new Date(inv.expires_at).toLocaleDateString() : '—';
        row.innerHTML = `
            <td class="border-t px-4 py-2">${inv.email}</td>
            <td class="border-t px-4 py-2">${inv.cog_name || '—'}</td>
            <td class="border-t px-4 py-2 capitalize">${inv.permission_level || '—'}</td>
            <td class="border-t px-4 py-2">${expires}</td>
            <td class="border-t px-4 py-2 text-right whitespace-nowrap">
                <button class="external-copy-link-button text-primary hover:underline mr-3" data-token="${inv.invitation_token || ''}" data-email="${inv.email}">Copy link</button>
                <button class="external-cancel-button text-red-600 hover:text-red-800" data-id="${inv.id}">Cancel</button>
            </td>
        `;
        tbody.appendChild(row);
    });
}

async function handleCancelExternalInvite(invitationId) {
    if (!(await showConfirm('Cancel this external invitation? The accept link will stop working.'))) return;
    try {
        await externalInvites.cancel(invitationId);
        loadExternalInvitations();
    } catch (error) {
        console.error('Error canceling external invitation:', error);
        showError('Failed to cancel external invitation.');
    }
}

export async function initializeExternalInvites() {
    const form = document.getElementById('external-invite-form');
    if (!form) return; // page variant without the external-invite card

    // rc20-CE-404 — external invitations are PAID-only: routers/external_invitations*.py
    // is manifest-denied (open-core/manifest.yaml:1496) and mounts only in main.py's
    // optional _GATE4B_ROUTER_MOUNTS block (CORE/API/main.py:2389-2390), so on CE every
    // /v1/external-invitations/* is a dead 404. loadExternalInvitations() below fires
    // GET /v1/external-invitations/pending on EVERY page load, which the shared net-guard
    // collects (CORE/e2e/fixtures/net.ts:71) and asserts absent (:100) — that 404 is what
    // failed the admin-identity "Invitations create/cancel round-trip" E2E. Hide the whole
    // card and fire nothing, using the same fail-open predicate the shell already uses
    // (api.js isPaidEdition, api.js:345 — false ONLY when /platform/features positively
    // reports edition "ce"; an unreadable payload keeps the paid behaviour unchanged).
    if (!await isPaidEdition()) {
        const card = form.parentElement;
        if (card) {
            card.style.display = 'none';
            // FIX-DASH-INVITATIONS-...-EMPTY-EXTERNAL-INVITES-CARD (audit F6):
            // display:none alone left the bordered wrapper (the card container in
            // pages/invitations.html) in the DOM as an empty bordered gap. Drop the
            // container node itself, not only its content. Paid editions never get
            // here (isPaidEdition() true), so the card stays byte-identical.
            card.remove();
        }
        return;
    }

    form.addEventListener('submit', handleCreateExternalInvite);

    const copyBtn = document.getElementById('external-invite-copy');
    if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
            const link = document.getElementById('external-invite-link')?.value || '';
            const ok = await copyToClipboard(link);
            copyBtn.textContent = ok ? 'Copied!' : 'Copy failed';
            setTimeout(() => { copyBtn.textContent = 'Copy link'; }, 2000);
        });
    }

    const table = document.getElementById('external-invitations-table');
    if (table) {
        table.addEventListener('click', async (e) => {
            if (e.target.classList.contains('external-cancel-button')) {
                handleCancelExternalInvite(e.target.getAttribute('data-id'));
            } else if (e.target.classList.contains('external-copy-link-button')) {
                const token = e.target.getAttribute('data-token');
                if (!token) { showInfo('This invite has no token to copy.'); return; }
                const ok = await copyToClipboard(externalInvites.acceptLink(token));
                const original = e.target.textContent;
                e.target.textContent = ok ? 'Copied!' : 'Copy failed';
                setTimeout(() => { e.target.textContent = original; }, 2000);
            }
        });
    }

    loadExternalCogOptions();
    loadExternalInvitations();
}

export function initializeInvitationsPageListeners() {
    const invitationForm = document.getElementById('invitation-form');
    if (invitationForm) {
        invitationForm.addEventListener('submit', handleSendInvitation);
    }

    // 2400LR-BUG-1027b — Copy button for the team-invite accept-link callout.
    const teamCopyBtn = document.getElementById('team-invite-copy');
    if (teamCopyBtn) {
        teamCopyBtn.addEventListener('click', async () => {
            const link = document.getElementById('team-invite-link')?.value || '';
            const ok = await copyToClipboard(link);
            teamCopyBtn.textContent = ok ? 'Copied!' : 'Copy failed';
            setTimeout(() => { teamCopyBtn.textContent = 'Copy link'; }, 2000);
        });
    }

    // external-invite-accept-ui — wire the external-invitation card.
    initializeExternalInvites();
    const invitationsTable = document.getElementById("invitations-table");
    if (invitationsTable) {
        invitationsTable.addEventListener('click', (e) => {
            if (e.target.classList.contains('cancel-invitation-button')) {
                handleCancelInvitation(e);
            }
        });
    }

    // Load tenants for platform admin (will hide selector if not platform admin)
    loadTenantsForPlatformAdmin().then(() => {
        // After tenant loading completes, check if we should load roles
        const tenantSelectorContainer = document.getElementById('invitation-tenant-container');

        if (tenantSelectorContainer && tenantSelectorContainer.style.display !== 'none') {
            // Platform admin - don't load roles yet, show instruction
            const roleSelector = document.getElementById('invitation-role');
            if (roleSelector) {
                roleSelector.innerHTML = '<option value="">Select a tenant first...</option>';
            }
        } else {
            // Regular user - load their tenant's roles
            loadRolesForInvitation();
        }
    });

    loadPermissionTemplates();
}