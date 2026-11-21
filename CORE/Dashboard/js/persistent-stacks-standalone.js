
    const API = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
    function token() { return sessionStorage.getItem("accessToken"); }
    function setMsg(t) { document.getElementById("msg").textContent = t || ""; }
    function _esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    async function api(path, opts = {}) {
      const r = await fetch(`${API}${path}`, {
        ...opts,
        headers: { "Authorization": `Bearer ${token()}`, "Content-Type": "application/json", ...(opts.headers || {}) },
      });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 140)}`);
      return r.status === 204 ? null : r.json();
    }

    let TEMPLATES = [];
    const REFS = {};
    async function loadTemplates() {
      TEMPLATES = await api("/persistent-stacks/templates");
      const sel = document.getElementById("tpl");
      sel.innerHTML = TEMPLATES.map(t => `<option value="${t.id}">${t.name}</option>`).join("");
      sel.onchange = renderTplParams; renderTplParams();
    }
    function curTpl() { return TEMPLATES.find(t => t.id === document.getElementById("tpl").value); }
    function renderTplParams() {
      const t = curTpl(); if (!t) return;
      document.getElementById("tplDesc").textContent = t.description || "";
      document.getElementById("tplParams").innerHTML = (t.params || [])
        .map(p => `<input data-key="${p.key}" aria-label="${p.label}" placeholder="${p.label}" value="${p.default || ""}" />`).join("");
    }
    async function createFromTemplate() {
      try {
        const t = curTpl();
        const params = {};
        document.querySelectorAll("#tplParams input").forEach(i => params[i.dataset.key] = i.value);
        const name = document.getElementById("tplName").value || `${t.id}-stack`;
        await api("/persistent-stacks/from-template", { method: "POST",
          body: JSON.stringify({ template_id: t.id, name, params }) });
        setMsg(`Created "${name}".`); load();
      } catch (e) { setMsg("Create failed: " + e.message); }
    }

    async function act(id, verb, q = "") {
      try { await api(`/persistent-stacks/${id}/${verb}${q}`, { method: "POST" }); setMsg(`${verb} dispatched for stack ${id}.`); load(); }
      catch (e) { setMsg(`${verb} failed: ` + e.message); }
    }
    async function backup(id) {
      try { const r = await api(`/persistent-stacks/${id}/backup`, { method: "POST" }); setMsg(`Backup ${r.snapshot_id} dispatched for stack ${id}.`); load(); }
      catch (e) { setMsg("Backup failed: " + e.message); }
    }
    async function restore(id, refs) {
      const snaps = Object.keys((refs && refs.snapshots) || {});
      if (!snaps.length) return setMsg("No snapshots for this stack — back it up first.");
      act(id, "restore", `?snapshot_id=${encodeURIComponent(snaps[snaps.length - 1])}`);
    }

    async function load() {
      try {
        const stacks = await api("/persistent-stacks");
        document.getElementById("rows").innerHTML = stacks.length ? stacks.map(s => { REFS[s.id] = s.storage_refs || {}; return `
          <tr>
            <td>${s.id}</td><td>${s.name}</td>
            <td><span class="status ${s.status}">${s.status}</span></td>
            <td>${s.target_worker || "—"}</td><td>${s.storage_class}</td>
            <td>
              <button data-ps-act="deploy" data-ps-id="${s.id}">Deploy</button>
              <button class="secondary" data-ps-act="backup" data-ps-id="${s.id}">Back up</button>
              <button class="secondary" data-ps-act="restore" data-ps-id="${s.id}">Restore</button>
              <button class="secondary" data-ps-act="migrate" data-ps-id="${s.id}">Migrate</button>
              <button class="danger" data-ps-act="teardown" data-ps-id="${s.id}">Tear down</button>
            </td>
          </tr>`; }).join("") : `<tr><td colspan="7" class="muted">No stacks yet. A stack runs a service such as an AI model on one of your workers: add a worker first (Dashboard > Workers > Add Worker), then click New AI stack.</td></tr>`;
      } catch (e) {
        document.getElementById("rows").innerHTML = `<tr><td colspan="7" class="muted">Failed to load (${_esc(e.message)}). Log into the dashboard first.</td></tr>`;
      }
    }

    if (!token()) setMsg("Not logged in — open the dashboard and sign in, then reload this page.");
    async function loadThreshold() {
      try {
        const r = await api("/persistent-stacks/rebalancer-config");
        document.getElementById("rbThreshold").value = r.threshold;
        document.getElementById("rbEnabled").checked = r.enabled === true;
        document.getElementById("rbSource").textContent = `source: ${r.source}`;
      } catch (e) { /* non-admin or not migrated — leave blank */ }
    }
    async function saveThreshold() {
      try {
        const v = parseFloat(document.getElementById("rbThreshold").value);
        const enabled = document.getElementById("rbEnabled").checked;
        const r = await api("/persistent-stacks/rebalancer-config",
          { method: "PUT", body: JSON.stringify({ threshold: v, enabled }) });
        document.getElementById("rbSource").textContent = `source: ${r.source}`;
        setMsg(`Rebalancer ${r.enabled ? "enabled" : "disabled"}, threshold ${r.threshold}`);
      } catch (e) { setMsg(e.message); }
    }


    // CE (D76): automatic idle reclaim is not part of this edition.
    function idleCell() { return ""; }
    async function loadIdle() {}

    // R-167 — inline on* handlers removed; wired here instead.
    document.getElementById("btn-create").addEventListener("click", createFromTemplate);
    document.getElementById("btn-save-threshold").addEventListener("click", saveThreshold);
    const rowsEl = document.getElementById("rows");
    rowsEl.addEventListener("click", (e) => {
      const b = e.target.closest("[data-ps-act]");
      if (!b) return;
      const id = Number(b.dataset.psId);
      const actv = b.dataset.psAct;
      if (actv === "backup") backup(id);
      else if (actv === "restore") restore(id, REFS[id]);
      else act(id, actv);
    });
    rowsEl.addEventListener("change", (e) => {
      return;
    });

    loadTemplates().catch(e => setMsg("Templates: " + e.message));
    loadIdle().then(load);
    loadThreshold();
