        const API_BASE_URL = window.APP_CONFIG?.API_URL || "http://api.localhost";

        const COMPONENT_LABELS = {
            api: 'API',
            database: 'Database',
            keycloak: 'Authentication',
            minio: 'Object Storage',
            redis: 'Cache',
            harbor: 'Container Registry',
            workers: 'Workers'
        };

        const STATUS_LABELS = { up: 'Operational', degraded: 'Degraded', down: 'Outage' };

        async function fetchStatus() {
            try {
                const resp = await fetch(`${API_BASE_URL}/health`);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            } catch (e) {
                return null;
            }
        }

        function renderBanner(status) {
            const el = document.getElementById('banner');
            const bannerText = {
                operational: 'All Systems Operational',
                degraded: 'Partial System Degradation',
                outage: 'System Outage Detected'
            };
            el.className = `banner ${status}`;
            el.innerHTML = `<span class="dot ${status === 'operational' ? 'up' : status === 'degraded' ? 'degraded' : 'down'}"></span>${bannerText[status] || 'Unknown'}`;
        }

        function renderComponents(components) {
            const el = document.getElementById('components');
            el.innerHTML = Object.entries(components).map(([key, status]) => {
                const label = COMPONENT_LABELS[key] || key;
                const statusLabel = STATUS_LABELS[status] || status;
                return `<div class="component">
                    <span class="component-name">${label}</span>
                    <span class="component-status ${status}"><span class="dot ${status}"></span>${statusLabel}</span>
                </div>`;
            }).join('');
        }

        async function refresh() {
            const data = await fetchStatus();
            if (!data) {
                renderBanner('outage');
                return;
            }
            renderBanner(data.status);
            renderComponents(data.components);
            document.getElementById('last-checked').textContent = `Last checked: ${new Date(data.checked_at).toLocaleTimeString()}`;
        }

        refresh();
        setInterval(refresh, 30000);
