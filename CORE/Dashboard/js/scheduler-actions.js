// scheduler-actions.js
//
// Shared helpers for the scheduler-runtime-config-panel feature:
//   * loadSchedulerRuntimeConfig() — GET the current snapshot.
//   * saveSchedulerRuntimeConfig({...}) — POST + return persisted row.
//   * generateTrainingData()         — relocated from workers.js.
//
// Imported by settings.js for the new Scheduler sub-tab. Backend
// endpoints + payload shapes are unchanged from the originals.

const PRESETS = {
    aggressive: { capacity_floor_pct: 10, packer_full_above_util_pct: 85, variance_penalty_s: 5, background_max_fleet_pct: 60 },
    normal:     { capacity_floor_pct: 30, packer_full_above_util_pct: 70, variance_penalty_s: 15, background_max_fleet_pct: 40 },
    light:      { capacity_floor_pct: 50, packer_full_above_util_pct: 50, variance_penalty_s: 30, background_max_fleet_pct: 20 },
};

function _apiUrl() {
    return window.APP_CONFIG?.API_URL || '';
}

function _authHeaders() {
    const token = sessionStorage.getItem('accessToken');
    return token ? { Authorization: `Bearer ${token}` } : {};
}

export function detectScheduleRuntimePreset({ capacity_floor_pct, packer_full_above_util_pct, variance_penalty_s, background_max_fleet_pct }) {
    for (const [name, p] of Object.entries(PRESETS)) {
        if (p.capacity_floor_pct === capacity_floor_pct
            && p.packer_full_above_util_pct === packer_full_above_util_pct
            && p.variance_penalty_s === variance_penalty_s
            && p.background_max_fleet_pct === background_max_fleet_pct) {
            return name;
        }
    }
    return 'custom';
}

export function getSchedulerPresetValues(name) {
    return PRESETS[name] || null;
}

export async function loadSchedulerRuntimeConfig() {
    const resp = await fetch(`${_apiUrl()}/platform/scheduler-runtime-config`, {
        headers: _authHeaders(),
    });
    if (!resp.ok) {
        const body = await resp.text().catch(() => '');
        throw new Error(`Failed to load scheduler config (HTTP ${resp.status}): ${body}`);
    }
    return resp.json();
}

export async function saveSchedulerRuntimeConfig({ capacity_floor_pct, packer_full_above_util_pct, variance_penalty_s, background_max_fleet_pct, preset_name }) {
    const resp = await fetch(`${_apiUrl()}/platform/scheduler-runtime-config`, {
        method: 'POST',
        headers: { ..._authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ capacity_floor_pct, packer_full_above_util_pct, variance_penalty_s, background_max_fleet_pct, preset_name }),
    });
    if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
            const body = await resp.json();
            detail = body.detail || JSON.stringify(body);
        } catch { /* keep status */ }
        throw new Error(detail);
    }
    return resp.json();
}

export async function fetchTrainingStatus() {
    const resp = await fetch(`${_apiUrl()}/platform/training/status`, {
        headers: _authHeaders(),
    });
    if (!resp.ok) {
        throw new Error(`training status: HTTP ${resp.status}`);
    }
    return resp.json();
}

// Relocated from workers.js. Returns the dispatch response augmented
// with a `baseline` snapshot (status fields captured BEFORE the POST)
// so the caller can compute progress as a delta.
export async function generateTrainingData() {
    let status = {};
    try {
        status = await fetchTrainingStatus();
    } catch (e) {
        console.warn('training status check failed', e);
    }

    const currentSamples = status.resource_samples || 0;
    const runsPerWorker = prompt(
        `Generate Training Data for TFT Model\n\n` +
        `Current data: ${currentSamples.toLocaleString()} resource samples\n` +
        `${status.completed_workflow_tasks || 0} completed tasks\n` +
        `${status.profiled_workers || 0} profiled workers\n` +
        `${status.unique_dag_types_with_patterns || 0} DAG types with patterns\n` +
        `${status.ready_for_training ? '✅ Ready for training!' : '⚠️ ' + (status.recommendation || 'Need more data')}\n\n` +
        `Enter runs per worker (default 500).\n` +
        `With 20 workers × 500 = 10,000 runs ≈ 30,000 tasks ≈ ~12 hours.`,
        '500'
    );

    if (!runsPerWorker) return null;

    const baseline = {
        training_runs_dispatched: status.training_runs_dispatched || 0,
        training_runs_completed: status.training_runs_completed || 0,
        completed_workflow_tasks: status.completed_workflow_tasks || 0,
    };

    const resp = await fetch(`${_apiUrl()}/platform/training/generate`, {
        method: 'POST',
        headers: { ..._authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ runs_per_worker: parseInt(runsPerWorker) || 500 }),
    });
    if (!resp.ok) {
        const error = await resp.json().catch(() => ({}));
        throw new Error(error.detail || 'Failed to generate training data');
    }
    const result = await resp.json();
    return { ...result, baseline, started_at: Date.now() };
}

// Poll /platform/training/status until `dispatched` workflow runs complete
// (relative to `baseline.training_runs_completed` captured before the POST).
//
// `onTick({ completedDelta, dispatched, pct, etaSeconds, status })` fires
// after every successful poll. `onDone(status)` fires once when the
// progress hits 100% (or when stopped externally). `onError(err)` fires
// on a poll failure but does NOT stop the loop — transient network
// errors are common during long runs.
//
// Returns a `{ stop }` handle so the caller can cancel polling
// (the underlying training run keeps running on the workers).
export function pollTrainingProgress({
    baseline, dispatched,
    intervalMsVisible = 2000,
    intervalMsHidden = 10000,
    rollingWindowSamples = 30,
    onTick, onDone, onError,
}) {
    let stopped = false;
    let timer = null;
    // Rolling window of recent observations, used for a smoother ETA
    // than "cumulative throughput since dispatch start". Each entry is
    // { tsMs, completedDelta }.
    const samples = [];

    function currentInterval() {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
            return intervalMsHidden;
        }
        return intervalMsVisible;
    }

    function rollingEtaSeconds(currentDelta) {
        if (samples.length < 2) return null;
        const oldest = samples[0];
        const latest = samples[samples.length - 1];
        const dtMs = latest.tsMs - oldest.tsMs;
        const dn = latest.completedDelta - oldest.completedDelta;
        if (dtMs <= 0 || dn <= 0) return null;
        const ratePerMs = dn / dtMs;
        const remaining = Math.max(0, dispatched - currentDelta);
        return Math.round(remaining / ratePerMs / 1000);
    }

    async function tick() {
        if (stopped) return;
        try {
            const status = await fetchTrainingStatus();
            const completedDelta = Math.max(
                0,
                (status.training_runs_completed || 0) - baseline.training_runs_completed,
            );

            samples.push({ tsMs: Date.now(), completedDelta });
            while (samples.length > rollingWindowSamples) samples.shift();

            const pct = dispatched > 0
                ? Math.min(100, Math.round((completedDelta / dispatched) * 1000) / 10)
                : 0;
            const etaSeconds = rollingEtaSeconds(completedDelta);
            const elapsedSeconds = Math.max(
                0,
                Math.round((Date.now() - (baseline.started_at_ms || Date.now())) / 1000),
            );

            if (typeof onTick === 'function') {
                onTick({
                    completedDelta, dispatched, pct, etaSeconds,
                    elapsedSeconds, status, sampleCount: samples.length,
                });
            }

            if (completedDelta >= dispatched) {
                stopped = true;
                if (typeof onDone === 'function') onDone(status);
                cleanup();
                return;
            }
        } catch (e) {
            if (typeof onError === 'function') onError(e);
            // continue — don't stop on transient errors
        }
        timer = setTimeout(tick, currentInterval());
    }

    // Visibility-aware: when the tab becomes visible after being hidden,
    // immediately fire a poll instead of waiting for the next 10s tick.
    let visibilityHandler = null;
    if (typeof document !== 'undefined') {
        visibilityHandler = () => {
            if (document.visibilityState === 'visible' && !stopped) {
                if (timer) {
                    clearTimeout(timer);
                    timer = null;
                }
                tick();
            }
        };
        document.addEventListener('visibilitychange', visibilityHandler);
    }

    function cleanup() {
        if (timer) {
            clearTimeout(timer);
            timer = null;
        }
        if (visibilityHandler && typeof document !== 'undefined') {
            document.removeEventListener('visibilitychange', visibilityHandler);
            visibilityHandler = null;
        }
    }

    tick();

    return {
        stop() {
            stopped = true;
            cleanup();
        },
    };
}
