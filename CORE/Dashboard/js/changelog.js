// What's New — public list + per-note deep link + admin authoring
// (#5 / capability `whats-new-publishing`). Renders hero cards
// using the design tokens from #6, supports tag filtering, and
// gives platform admins inline authoring without leaving the page.

import { fetchApi as apiCall, me } from './api.js';

let _entries = [];
let _activeTag = null;
let _currentUser = null;

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function renderBody(body) {
    return escapeHtml(body)
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/`(.*?)`/g, '<code class="bg-card px-1 rounded text-sm">$1</code>')
        .replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>')
        .replace(/^### (.+)$/gm, '<h4 class="font-semibold text-gray-700 mt-3">$1</h4>')
        .replace(/\n/g, '<br>');
}

function fmtDate(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleDateString('en-US', {
        year: 'numeric', month: 'long', day: 'numeric',
    });
}

function tagChips(tags, opts) {
    if (!tags || !tags.length) return '';
    const interactive = opts && opts.interactive;
    return tags.map(t => {
        const cls = `tag-chip inline-block px-2 py-0.5 mr-1 mb-1 rounded text-[11px] bg-primary/10 text-primary ${interactive ? 'cursor-pointer hover:bg-primary/25' : ''}`;
        return `<span class="${cls}" data-tag="${escapeHtml(t)}">${escapeHtml(t)}</span>`;
    }).join('');
}

function renderTagBar() {
    const bar = document.getElementById('wn-tag-bar');
    if (!bar) return;
    const all = new Set();
    _entries.forEach(e => (e.tags || []).forEach(t => all.add(t)));
    const tags = Array.from(all).sort();
    bar.innerHTML = '';
    if (!tags.length) return;
    const allChip = document.createElement('button');
    allChip.className = `px-2.5 py-1 rounded-full border text-xs ${_activeTag == null ? 'bg-primary text-white border-primary' : 'bg-white text-gray-600 border-gray-200 hover:bg-card'}`;
    allChip.textContent = 'All';
    allChip.onclick = () => { _activeTag = null; renderEntries(); renderTagBar(); };
    bar.appendChild(allChip);
    tags.forEach(t => {
        const c = document.createElement('button');
        c.className = `px-2.5 py-1 rounded-full border text-xs ${_activeTag === t ? 'bg-primary text-white border-primary' : 'bg-white text-gray-600 border-gray-200 hover:bg-card'}`;
        c.textContent = t;
        c.onclick = () => { _activeTag = t; renderEntries(); renderTagBar(); };
        bar.appendChild(c);
    });
}

function renderEntries() {
    const container = document.getElementById('changelog-entries');
    if (!container) return;
    const visible = _activeTag
        ? _entries.filter(e => (e.tags || []).includes(_activeTag))
        : _entries;
    if (!visible.length) {
        container.innerHTML = `<p class="text-gray-500 col-span-full">${_activeTag ? `No releases tagged "${escapeHtml(_activeTag)}".` : 'No updates yet.'}</p>`;
        return;
    }
    container.innerHTML = visible.map(entry => {
        const date = fmtDate(entry.published_at);
        const versionPill = entry.version
            ? `<span class="px-2 py-0.5 bg-primary/20 text-primary rounded-full text-xs font-medium">${escapeHtml(entry.version)}</span>`
            : '';
        const hero = entry.hero_image_url
            ? `<img src="${escapeHtml(entry.hero_image_url)}" alt="" class="w-full h-40 object-cover"/>`
            : '<div class="h-2 bg-primary/80"></div>';
        const summary = entry.summary
            ? `<p class="text-gray-600 mb-3 text-sm">${escapeHtml(entry.summary)}</p>`
            : '';
        const tagsHtml = tagChips(entry.tags, {interactive: false});
        return `
            <a href="#changelog?slug=${encodeURIComponent(entry.slug || '')}" class="bg-white rounded-xl border border-gray-200 shadow-soft overflow-hidden hover:shadow-raised transition-shadow block">
                ${hero}
                <div class="p-5">
                    <div class="flex items-center gap-2 mb-2 flex-wrap">
                        ${versionPill}
                        <span class="text-xs text-gray-500">${escapeHtml(date)}</span>
                    </div>
                    <h2 class="text-lg font-semibold text-gray-800 mb-1">${escapeHtml(entry.title)}</h2>
                    ${summary}
                    <div class="text-xs">${tagsHtml}</div>
                </div>
            </a>
        `;
    }).join('');
}

async function showDetailFromSlug(slug) {
    const panel = document.getElementById('wn-detail-panel');
    if (!panel) return;
    try {
        const entry = await apiCall(`/changelog/by-slug/${encodeURIComponent(slug)}`);
        document.getElementById('wn-detail-title').textContent = entry.title;
        document.getElementById('wn-detail-summary').textContent = entry.summary || '';
        const meta = `${entry.version ? `${entry.version} · ` : ''}${fmtDate(entry.published_at)}`;
        document.getElementById('wn-detail-meta').textContent = meta;
        const hero = document.getElementById('wn-detail-hero');
        if (entry.hero_image_url) {
            hero.src = entry.hero_image_url;
            hero.classList.remove('hidden');
        } else {
            hero.classList.add('hidden');
        }
        document.getElementById('wn-detail-body').innerHTML = renderBody(entry.body || '');
        panel.classList.remove('hidden');
        panel.scrollIntoView({behavior: 'smooth', block: 'start'});
    } catch (e) {
        console.error('Failed to load release note', e);
    }
}

function bindAdminFormHandlers() {
    document.getElementById('wn-author-btn')?.addEventListener('click', () => {
        document.getElementById('wn-author-form')?.classList.remove('hidden');
    });
    document.getElementById('wn-author-cancel')?.addEventListener('click', () => {
        document.getElementById('wn-author-form')?.classList.add('hidden');
    });
    document.getElementById('wn-author-discard')?.addEventListener('click', () => {
        document.getElementById('wn-author-form-inner')?.reset();
        document.getElementById('wn-author-form')?.classList.add('hidden');
    });
    document.getElementById('wn-f-publish-mode')?.addEventListener('change', (e) => {
        document.getElementById('wn-f-scheduled').disabled = e.target.value !== 'schedule';
    });
    document.getElementById('wn-author-form-inner')?.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const status = document.getElementById('wn-author-status');
        status.textContent = 'Saving…';
        status.className = 'text-xs text-gray-500';
        const mode = document.getElementById('wn-f-publish-mode').value;
        const tagsRaw = document.getElementById('wn-f-tags').value || '';
        const audience = document.getElementById('wn-f-audience')?.value || '';
        // Publish-time guard: API rejects publish without an explicit
        // audience. Surface that to the user before posting so they
        // see a friendly inline message instead of a 400 toast.
        if ((mode === 'now' || mode === 'schedule') && audience !== 'user_facing' && audience !== 'internal') {
            status.textContent = 'Pick an audience before publishing — User-facing or Internal.';
            status.className = 'text-xs text-destructive';
            return;
        }
        const body = {
            title: document.getElementById('wn-f-title').value.trim(),
            summary: document.getElementById('wn-f-summary').value.trim() || null,
            version: document.getElementById('wn-f-version').value.trim() || null,
            tags: tagsRaw.split(',').map(s => s.trim()).filter(Boolean),
            hero_image_url: document.getElementById('wn-f-hero').value.trim() || null,
            body: document.getElementById('wn-f-body').value,
            is_published: mode === 'now',
            scheduled_for: null,
            audience: audience || null,
        };
        if (mode === 'schedule') {
            const v = document.getElementById('wn-f-scheduled').value;
            if (!v) { status.textContent = 'Pick a scheduled time.'; status.className = 'text-xs text-destructive'; return; }
            body.scheduled_for = new Date(v).toISOString();
        }
        try {
            await apiCall('/changelog', {method: 'POST', body: JSON.stringify(body)});
            status.textContent = mode === 'now' ? 'Published.' : (mode === 'schedule' ? 'Scheduled.' : 'Saved as draft.');
            status.className = 'text-xs text-success';
            document.getElementById('wn-author-form-inner').reset();
            await loadEntries();
            setTimeout(() => {
                document.getElementById('wn-author-form')?.classList.add('hidden');
            }, 800);
        } catch (e) {
            console.error('Failed to save release note', e);
            status.textContent = `Failed: ${e.message || e}`;
            status.className = 'text-xs text-destructive';
        }
    });
    document.getElementById('wn-detail-close')?.addEventListener('click', () => {
        document.getElementById('wn-detail-panel')?.classList.add('hidden');
        if (window.location.hash.includes('?slug=')) {
            window.location.hash = '#changelog';
        }
    });
    document.getElementById('wn-drafts-btn')?.addEventListener('click', () => {
        const panel = document.getElementById('wn-drafts-panel');
        if (!panel) return;
        panel.classList.toggle('hidden');
        if (!panel.classList.contains('hidden')) {
            loadDrafts();
        }
    });
    document.getElementById('wn-drafts-close')?.addEventListener('click', () => {
        document.getElementById('wn-drafts-panel')?.classList.add('hidden');
    });
}

function renderDrafts(drafts) {
    const list = document.getElementById('wn-drafts-list');
    if (!list) return;
    if (!drafts.length) {
        list.innerHTML = '<p class="text-gray-500 text-sm">No drafts or scheduled entries.</p>';
        return;
    }
    list.innerHTML = drafts.map(entry => {
        const statusLabel = entry.scheduled_for
            ? `Scheduled for ${fmtDate(entry.scheduled_for)}`
            : 'Draft';
        return `
            <div class="flex items-center justify-between border border-gray-200 rounded-lg px-3 py-2">
                <div>
                    <span class="font-medium text-gray-800">${escapeHtml(entry.title)}</span>
                    ${entry.version ? `<span class="ml-2 text-xs text-gray-500">${escapeHtml(entry.version)}</span>` : ''}
                    <div class="text-xs text-gray-500">${escapeHtml(statusLabel)}</div>
                </div>
            </div>
        `;
    }).join('');
}

async function loadDrafts() {
    const list = document.getElementById('wn-drafts-list');
    if (!list) return;
    list.innerHTML = '<p class="text-gray-500 text-sm">Loading…</p>';
    try {
        const drafts = await apiCall('/changelog/drafts') || [];
        renderDrafts(drafts);
    } catch (e) {
        console.error('Failed to load drafts:', e);
        list.innerHTML = '<p class="text-destructive text-sm">Failed to load drafts.</p>';
    }
}

async function loadEntries() {
    const container = document.getElementById('changelog-entries');
    if (!container) return;
    try {
        _entries = await apiCall('/changelog?limit=50') || [];
        localStorage.setItem('changelog_last_seen', new Date().toISOString());
        renderTagBar();
        renderEntries();
    } catch (e) {
        console.error('Failed to load changelog:', e);
        container.innerHTML = '<p class="text-destructive col-span-full">Failed to load What\'s New.</p>';
    }
}

export async function initializeChangelogPage() {
    bindAdminFormHandlers();

    // Reveal admin controls if the current user is a platform admin.
    try {
        _currentUser = await me.get();
        if (_currentUser && _currentUser.is_platform_admin) {
            document.getElementById('wn-author-btn')?.classList.remove('hidden');
            document.getElementById('wn-drafts-btn')?.classList.remove('hidden');
        }
    } catch { /* ignore — list still works for non-admins */ }

    await loadEntries();

    // If the URL is `#changelog?slug=<slug>`, focus that release.
    const hashSlug = window.location.hash.match(/[?&]slug=([^&]+)/);
    if (hashSlug && hashSlug[1]) {
        showDetailFromSlug(decodeURIComponent(hashSlug[1]));
    }
}
