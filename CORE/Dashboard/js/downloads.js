        // Fetch download manifest and populate download buttons
        async function loadDownloads() {
            try {
                const response = await fetch('/downloads/manifest.json');
                const manifest = await response.json();

                // WIN.1.s16: a manifest may exist without any artifact keys (or with
                // no `files` dict at all). Never invent a filename: a missing key
                // renders "Not yet available", never a dead link.
                // The Windows keys are fed from the release bundle's `windows_artifacts`
                // entries (CORE/release/build-bundle.sh:502 dashboard_key
                // "windows" / "windows_app"); these two keys are the Dashboard's own
                // names and stay stable.
                const files = manifest.files || {};

                const platforms = [
                    {
                        icon: '🪟',
                        name: 'Windows',
                        description: 'Windows 10 and later<br>x64 installer (MOD-Setup MSI)',
                        file: files.windows,
                        altFile: files.windows_app,
                        altLabel: 'MODTray app',
                        type: 'windows'
                    },
                    {
                        icon: '🍎',
                        name: 'macOS',
                        description: 'macOS 10.13 and later<br>Intel and Apple Silicon',
                        file: files.macos,
                        type: 'macos'
                    },
                    {
                        icon: '🐧',
                        name: 'Linux',
                        description: 'Ubuntu, Debian, and more<br>Universal AppImage',
                        file: files.linux_appimage,
                        altFile: files.linux_deb,
                        altLabel: '.deb package',
                        type: 'linux'
                    }
                ];

                const platformsContainer = document.getElementById('platforms');
                platformsContainer.innerHTML = '';

                platforms.forEach(platform => {
                    const card = document.createElement('div');
                    card.className = 'platform-card';

                    const downloadButton = platform.file
                        ? `<a href="/downloads/${platform.file}" class="download-button">Download for ${platform.name}</a>`
                        : `<span class="download-button disabled">Not yet available</span>`;

                    const altDownload = platform.altFile
                        ? `<br><small style="display: block; margin-top: 12px;"><a href="/downloads/${platform.altFile}" style="color: #667eea;">Download ${platform.altLabel} →</a></small>`
                        : '';

                    card.innerHTML = `
                        <div class="platform-icon">${platform.icon}</div>
                        <h2>${platform.name}</h2>
                        <p>${platform.description}</p>
                        ${downloadButton}
                        ${altDownload}
                    `;

                    platformsContainer.appendChild(card);
                });

                // Update version if available
                if (manifest.version) {
                    document.getElementById('version').textContent = `v${manifest.version}`;
                }

            } catch (error) {
                console.error('Failed to load downloads:', error);
                document.getElementById('platforms').innerHTML = `
                    <div class="platform-card" style="grid-column: 1 / -1; text-align: center;">
                        <h2>Downloads Not Yet Available</h2>
                        <p>No installer artifact has been published for this release yet.<br>
                        The worker enrollment one-liner on the Workers page still works
                        (it installs the worker service directly, no installer needed).</p>
                    </div>
                `;
            }
        }

        loadDownloads();
