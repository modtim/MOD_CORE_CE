
    const API = (window.APP_CONFIG && window.APP_CONFIG.API_URL) || "";
    const token = () => sessionStorage.getItem("accessToken");
    const setMsg = (t, ok) => { const m = document.getElementById("msg"); m.textContent = t || ""; m.style.color = ok ? "#1b7a35" : "#b1303a"; };

    const TYPES = ["schedule","interval","one_shot","workflow_completion","node_completion","fan_in",
      "webhook","api_call","email","chatops","sms","git","file_event","db_row_event","system_threshold",
      "log_pattern_alert","queue_empty","manual","approval","form","composite","message_bus","poll"];
    const HINTS = {
      schedule: '{"cron":"0 9 * * 1","timezone":"UTC"}',
      interval: '{"interval_seconds":900}',
      one_shot: '{"run_at":"2026-07-01T09:00:00Z"}',
      workflow_completion: '{"source_workflow_id":1,"on_status":"succeeded","forward_outputs":true}',
      node_completion: '{"source_workflow_id":1,"workflow_node_id":5,"on_status":"succeeded"}',
      fan_in: '{"sources":[1,2],"window_seconds":3600}',
      webhook: '{}', api_call: '{}', email: '{"address":"intake@flows.example.com"}',
      chatops: '{"provider":"slack","signing_secret":"…","command":"/run"}',
      sms: '{"keyword":"deploy"}', git: '{"provider":"github","signing_secret":"…","branch":"main"}',
      file_event: '{"source":"s3","bucket":"tenant-1-files","suffix":".psd","event_type":"created"}',
      db_row_event: '{"table":"orders","op":"insert","filter":{"status":"paid"}}',
      system_threshold: '{"metric":"queue_depth","op":"gt","value":50,"rearm_value":20}',
      log_pattern_alert: '{"pattern":"OutOfMemory","level":"ERROR"}',
      queue_empty: '{"scope":"tenant"}', manual: '{}',
      approval: '{"approvers":[1,2]}', form: '{"fields":["name","email"]}',
      composite: '{"op":"and","constituents":[10,11],"window_seconds":3600}',
      message_bus: '{"provider":"redis","topic":"events"}',
      poll: '{"kind":"http","url":"https://example.com/feed.json","interval_seconds":300}',
    };

    async function api(path, opts = {}) {
      const r = await fetch(`${API}${path}`, { ...opts,
        headers: { "Authorization": `Bearer ${token()}`, "Content-Type": "application/json", ...(opts.headers || {}) } });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
      return r.status === 204 ? null : r.json();
    }

    const sel = document.getElementById("ttype");
    TYPES.forEach(t => { const o = document.createElement("option"); o.value = o.textContent = t; sel.appendChild(o); });
    document.getElementById("hint").onclick = () => { document.getElementById("config").value = HINTS[sel.value] || "{}"; };

    function fmt(ts) { return ts ? new Date(ts).toLocaleString() : "—"; }

    async function load() {
      try {
        const list = await api("/road/triggers/");
        const rows = document.getElementById("rows");
        rows.innerHTML = "";
        for (const t of list) {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td>${t.id}</td><td><code>${t.trigger_type}</code></td><td>${t.workflow_id}</td>
            <td><span class="pill ${t.is_active ? "on" : "off"}">${t.is_active ? "active" : "inactive"}</span></td>
            <td>${fmt(t.last_triggered_at)}</td><td>${t.trigger_count || 0}</td>
            <td class="row" style="min-width:220px;">
              <button class="ghost" data-test="${t.id}">Test</button>
              <button class="danger" data-del="${t.id}">Delete</button></td>`;
          rows.appendChild(tr);
        }
        rows.querySelectorAll("[data-test]").forEach(b => b.onclick = () => testTrigger(b.dataset.test));
        rows.querySelectorAll("[data-del]").forEach(b => b.onclick = () => delTrigger(b.dataset.del));
      } catch (e) { setMsg(e.message, false); }
    }

    document.getElementById("create").onclick = async () => {
      try {
        const cfg = JSON.parse(document.getElementById("config").value || "{}");
        const body = { workflow_id: Number(document.getElementById("wfid").value),
          trigger_type: sel.value, configuration: cfg,
          is_active: document.getElementById("active").value === "true" };
        const r = await api("/road/triggers/", { method: "POST", body: JSON.stringify(body) });
        let extra = r.webhook_url ? ` — URL: ${r.webhook_url}` : "";
        if (r.webhook_secret) extra += ` (secret shown once: ${r.webhook_secret})`;
        setMsg(`Created trigger #${r.id}${extra}`, true);
        load();
      } catch (e) { setMsg(e.message, false); }
    };

    async function testTrigger(id) {
      try {
        const r = await api(`/road/triggers/${id}/test`, { method: "POST", body: JSON.stringify({ mode: "dry_run" }) });
        setMsg(`Test #${id}: would_dispatch=${r.would_dispatch}${r.reason ? " (blocked: " + r.reason + ")" : ""}`, r.would_dispatch);
      } catch (e) { setMsg(e.message, false); }
    }

    async function delTrigger(id) {
      if (!confirm(`Delete trigger #${id}?`)) return;
      try { await api(`/road/triggers/${id}`, { method: "DELETE" }); setMsg(`Deleted #${id}`, true); load(); }
      catch (e) { setMsg(e.message, false); }
    }

    load();
