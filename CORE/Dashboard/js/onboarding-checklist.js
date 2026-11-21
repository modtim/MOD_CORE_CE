// Dashboard/js/onboarding-checklist.js
// Floating onboarding checklist with slide-out panel

// FIX-CE-ONBOARDING-BONUS-CREDITS (owner 11-11, answer B): CE has NO credits economy.
// api.js imports nothing, so pulling getPlatformFeatures in here adds no import cycle
// (same reason settings.js/api.js read it: js/api.js:436).
import { getPlatformFeatures } from './api.js';

const apiUrl = () => window.APP_CONFIG?.API_URL || 'http://api.localhost';
const authHeaders = () => ({
    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
    'Content-Type': 'application/json',
});

let checklistOpen = false;

const CELEBRATION_MESSAGES = [
    { title: "Amazing!", subtitle: "You're making great progress!" },
    { title: "Awesome!", subtitle: "Keep up the great work!" },
    { title: "Fantastic!", subtitle: "You're on a roll!" },
    { title: "Well Done!", subtitle: "Another step conquered!" },
    { title: "Brilliant!", subtitle: "You're almost there!" },
];

// FIX-CE-ONBOARDING-BONUS-CREDITS: positive-paid gate, the SAME shape as the OAuth
// panel gate in js/settings.js (~:5396, commits c92305253 / 650a03c68). ONLY a POSITIVE
// saas/pro edition is paid; CE, an unknown edition, an entitlements error and a rejected
// /platform/features call all resolve TRUE (= CE = hide the credits economy). Fail closed.
function _isCeEdition() {
    return getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !(ent && !ent.error && (ent.edition === 'saas' || ent.edition === 'pro'));
    }).catch(() => true);
}

export async function initOnboardingChecklist() {
    const isSaaS = window.APP_CONFIG?.DEPLOYMENT_MODE === 'saas';

    try {
        const resp = await fetch(`${apiUrl()}/onboarding/checklist`, { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();

        // FIX-CE-ONBOARDING-BONUS-CREDITS: resolve the edition ONCE per init (api.js
        // coalesces + caches /platform/features for 30s) BEFORE anything renders, so
        // credit text can never paint first. Fail closed: a throw/unknown = CE.
        let isCe = true;
        try { isCe = await _isCeEdition(); } catch (e) { isCe = true; }

        // In SaaS: hide after all done + reward claimed. On-prem (and CE, which has no
        // credits economy): hide after all done.
        // FIX-ONBOARDING-CHECKLIST-JS-CE-ALL-DONE-REMOVE-FAB (AUDIT-CE-JOURNEY-ONBOARDING-CHECKLIST
        // F2): the early return below used to leave #checklist-fab and #checklist-panel
        // on screen with stale numbers until a reload. Remove both when the return is taken because of
        // the fail-closed CE gate (isCe from _isCeEdition, ~:29). Guarded on isCe ONLY: paid (saas/pro,
        // isCe false) keeps today's behaviour byte-for-byte - the claim handler (~:260) already removes
        // both 2 s after a successful claim, and the guard line itself is unchanged.
        if (data.all_done && isCe) {
            document.getElementById('checklist-fab')?.remove();
            document.getElementById('checklist-panel')?.remove();
        }
        if (data.all_done && (data.reward_claimed || isCe || !isSaaS)) return;

        // Check for newly completed steps → celebrate!
        _checkForNewCompletions(data);

        renderFloatingButton(data);
        renderSlideOutPanel(data, isSaaS, isCe);
    } catch (e) {
        // Silently fail — checklist is non-critical
    }
}

function _checkForNewCompletions(data) {
    // MONOTONIC celebrated-set: a step is celebrated at most once, ever. The
    // set only grows. The old code overwrote a "completed steps" list with the
    // current set each call, so ANY fluctuation in the API's completed-set
    // (or rapid re-inits from page polling) re-fired the popup on a loop. A
    // never-shrinking set makes re-celebration impossible.
    const key = 'checklist_celebrated_steps';
    let celebrated;
    try {
        celebrated = new Set(JSON.parse(localStorage.getItem(key) || '[]'));
    } catch (e) {
        celebrated = new Set();
    }
    const hadSeen = celebrated.size > 0;
    const current = data.steps.filter(s => s.completed).map(s => s.title);
    const newlyDone = current.filter(t => !celebrated.has(t));
    if (newlyDone.length === 0) return;

    // Record everything currently complete so none of it can ever re-fire.
    current.forEach(t => celebrated.add(t));
    localStorage.setItem(key, JSON.stringify([...celebrated]));

    // Don't celebrate on the first observation (fresh browser / pre-existing
    // completions) — only genuine new progress. This also immediately
    // silences the prior bug's loop: the first post-fix call records the set
    // without firing.
    if (!hadSeen) return;

    const stepName = newlyDone[0];
    const msg = CELEBRATION_MESSAGES[Math.min(current.length - 1, CELEBRATION_MESSAGES.length - 1)];
    _showCelebration(msg.title, msg.subtitle, stepName);
}

function _showCelebration(title, subtitle, stepName) {
    // Never stack overlays — if one is already on screen, skip.
    if (document.getElementById('celebration-overlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'celebration-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.5);animation:fadeIn 0.3s ease;';
    overlay.innerHTML = `
        <div style="background:white;border-radius:24px;padding:48px 64px;text-align:center;max-width:500px;animation:bounceIn 0.5s cubic-bezier(0.34,1.56,0.64,1);box-shadow:0 25px 50px rgba(0,0,0,0.25);">
            <div style="font-size:72px;margin-bottom:16px;">🎉</div>
            <h2 style="font-size:36px;font-weight:800;color:#1a1a2e;margin:0 0 8px 0;">${title}</h2>
            <p style="font-size:18px;color:#6b7280;margin:0 0 12px 0;">${subtitle}</p>
            <div style="display:inline-block;padding:8px 20px;background:#e0e7ff;border-radius:999px;margin-top:8px;">
                <span style="font-size:14px;font-weight:600;color:#4338ca;">✓ ${stepName}</span>
            </div>
            <div style="margin-top:24px;">
                <button onclick="this.closest('#celebration-overlay').remove()" style="padding:12px 32px;background:#4f46e5;color:white;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;">
                    Keep Going!
                </button>
            </div>
        </div>
    `;

    // Add animations
    const style = document.createElement('style');
    style.textContent = `
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes bounceIn { from { transform: scale(0.3); opacity: 0; } to { transform: scale(1); opacity: 1; } }
    `;
    document.head.appendChild(style);
    document.body.appendChild(overlay);

    // Auto-dismiss after 5 seconds
    setTimeout(() => overlay.remove(), 5000);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

function renderFloatingButton(data) {
    const existing = document.getElementById('checklist-fab');
    if (existing) existing.remove();

    const pct = Math.round((data.completed / data.total) * 100);

    const btn = document.createElement('button');
    btn.id = 'checklist-fab';
    btn.className = 'fixed bottom-5 right-24 z-40 w-14 h-14 bg-primary text-white rounded-full shadow-lg hover:bg-primary/90 transition-all flex items-center justify-center group';
    btn.title = 'Get Started Checklist';
    btn.innerHTML = `
        <div class="relative w-10 h-10">
            <svg class="w-10 h-10 -rotate-90" viewBox="0 0 36 36">
                <circle cx="18" cy="18" r="15" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="3"/>
                <circle cx="18" cy="18" r="15" fill="none" stroke="white" stroke-width="3"
                        stroke-dasharray="${pct} ${100 - pct}" stroke-linecap="round"/>
            </svg>
            <span class="absolute inset-0 flex items-center justify-center text-xs font-bold">${data.completed}/${data.total}</span>
        </div>
    `;

    btn.addEventListener('click', togglePanel);
    document.body.appendChild(btn);
}

function renderSlideOutPanel(data, isSaaS = false, isCe = true) {
    const existing = document.getElementById('checklist-panel');
    if (existing) existing.remove();

    const stepsHtml = data.steps.map(step => {
        const done = step.completed;
        const icon = done
            ? '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-green-500 flex-shrink-0"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
            : '<div class="w-5 h-5 rounded-full border-2 border-gray-300 flex-shrink-0"></div>';
        const textClass = done ? 'text-gray-400 line-through' : 'text-gray-800';
        let actionLink = '';
        if (!done && step.action) {
            if (step.action === '#workflows') {
                // Workflow Builder is a separate app — navigate with token like the sidebar does
                actionLink = `<a href="#" class="text-xs text-primary hover:underline mt-0.5 block" onclick="event.preventDefault(); document.getElementById('checklist-panel').classList.add('translate-x-full'); document.getElementById('workflow-builder-link')?.click();">Do this now</a>`;
            } else {
                actionLink = `<a href="${step.action}" class="text-xs text-primary hover:underline mt-0.5 block" onclick="document.getElementById('checklist-panel').classList.add('translate-x-full')">Do this now</a>`;
            }
        }

        return `
            <div class="flex items-start space-x-3 py-3 border-b border-gray-100 last:border-0">
                ${icon}
                <div>
                    <div class="text-sm font-medium ${textClass}">${step.title}</div>
                    <div class="text-xs text-gray-500">${step.description}</div>
                    ${actionLink}
                </div>
            </div>
        `;
    }).join('');

    // FIX-CE-ONBOARDING-BONUS-CREDITS (owner 11-11, answer B): CE has NO credits economy.
    // isCe is the positive-paid gate resolved ONCE in initOnboardingChecklist (fail
    // closed: CE / unknown edition / entitlements error / rejected features call = true).
    // On CE the whole credits branch is gone: no 'Claim your N bonus credits' line, no
    // 'Claim N Credits' button, no 'You earned N bonus credits!' line, no 'to earn N
    // bonus credits' sentence. The checklist steps + progress bar above are untouched,
    // and the reward sentence is replaced with NOTHING (CE already rendered nothing here
    // today via DEPLOYMENT_MODE=onprem; the gate makes that authoritative instead of
    // config-dependent — a CE install with DEPLOYMENT_MODE=saas can no longer paint it).
    // saas/pro (positive paid) keep the exact branches below, unchanged.
    const rewardSection = (isCe || !isSaaS)
        ? (data.all_done ? '<div class="mt-4 p-3 bg-green-50 border border-green-200 rounded-lg text-center"><p class="text-sm text-green-600 font-medium">All done! You\'re all set.</p></div>' : '')
        : data.all_done && !data.reward_claimed
        ? `<div class="mt-4 p-4 bg-green-50 border border-green-200 rounded-lg text-center">
               <div class="text-lg font-bold text-green-700">All done!</div>
               <p class="text-sm text-green-600 mt-1">Claim your ${data.reward_credits} bonus credits</p>
               <button id="claim-reward-btn" class="mt-3 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 text-sm font-medium">
                   Claim ${data.reward_credits} Credits
               </button>
           </div>`
        : data.reward_claimed
            ? `<div class="mt-4 p-4 bg-green-50 border border-green-200 rounded-lg text-center">
                   <div class="text-sm text-green-600">You earned ${data.reward_credits} bonus credits!</div>
               </div>`
            : `<div class="mt-4 p-3 bg-primary/10 border border-primary rounded-lg text-center">
                   <p class="text-xs text-primary">Complete all ${data.total} steps to earn <strong>${data.reward_credits} bonus credits</strong></p>
               </div>`;

    const panel = document.createElement('div');
    panel.id = 'checklist-panel';
    panel.className = 'fixed top-0 right-0 h-full w-80 bg-white shadow-2xl z-50 transform translate-x-full transition-transform duration-300 flex flex-col';
    panel.innerHTML = `
        <div class="p-4 border-b border-gray-200 flex items-center justify-between">
            <div>
                <h3 class="text-lg font-semibold text-gray-900">Get Started</h3>
                <p class="text-xs text-gray-500">${data.completed}/${data.total} complete</p>
            </div>
            <button id="close-checklist" class="p-1 rounded hover:bg-gray-100" aria-label="Close">
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-gray-400"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
        </div>
        <div class="w-full bg-gray-200 h-1">
            <div class="bg-primary h-1 transition-all" style="width: ${(data.completed / data.total) * 100}%"></div>
        </div>
        <div class="flex-1 overflow-y-auto p-4">
            ${stepsHtml}
            ${rewardSection}
        </div>
    `;

    document.body.appendChild(panel);

    document.getElementById('close-checklist')?.addEventListener('click', togglePanel);

    document.getElementById('claim-reward-btn')?.addEventListener('click', async () => {
        const btn = document.getElementById('claim-reward-btn');
        btn.disabled = true;
        btn.textContent = 'Claiming...';

        try {
            const resp = await fetch(`${apiUrl()}/onboarding/checklist/claim-reward`, {
                method: 'POST',
                headers: authHeaders(),
            });
            if (resp.ok) {
                btn.textContent = 'Claimed!';
                btn.className = 'mt-3 px-4 py-2 bg-gray-400 text-white rounded-lg text-sm font-medium cursor-default';

                // Update the floating button
                setTimeout(() => {
                    document.getElementById('checklist-fab')?.remove();
                    document.getElementById('checklist-panel')?.remove();
                }, 2000);
            } else {
                const err = await resp.json();
                showInfo(err.detail || 'Failed to claim');
                btn.disabled = false;
                btn.textContent = `Claim ${data.reward_credits} Credits`;
            }
        } catch (e) {
            showError('Error: ' + e.message);
            btn.disabled = false;
            btn.textContent = `Claim ${data.reward_credits} Credits`;
        }
    });
}

function togglePanel() {
    const panel = document.getElementById('checklist-panel');
    if (!panel) return;
    checklistOpen = !checklistOpen;
    if (checklistOpen) {
        panel.classList.remove('translate-x-full');
    } else {
        panel.classList.add('translate-x-full');
    }
}

// Expose globally so other pages (files.js, workflows.js) can trigger re-check
// after actions that may complete checklist steps (upload, create workflow, run workflow)
window.refreshOnboardingChecklist = initOnboardingChecklist;

// Also poll every 30 seconds to catch completions from other tabs/apps (WorkflowBuilder)
setInterval(() => {
    if (document.getElementById('checklist-fab')) {
        initOnboardingChecklist();
    }
}, 30000);
