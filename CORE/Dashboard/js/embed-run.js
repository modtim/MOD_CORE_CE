
(function () {
  const params = new URLSearchParams(location.search);
  const token = params.get("t") || params.get("token");
  // ?theme=dark|light forces the palette; otherwise it follows the viewer's OS.
  const _theme = (params.get("theme") || "").toLowerCase();
  if (_theme === "dark" || _theme === "light") document.documentElement.dataset.theme = _theme;
  // Derive the API host from the dashboard host by swapping the "dashboard"
  // subdomain label for "api", preserving ANY prefix (dashboard / dev-dashboard
  // / mod-dashboard / …). The old regex only allowed an optional "dev-" prefix,
  // so on a "mod-dashboard" production host apiBase wrongly stayed on the
  // dashboard origin → /embed/run/<token> hit the static dashboard server →
  // HTML → "Unexpected token '<' … is not valid JSON". Works on dev-dashboard,
  // broke on mod-dashboard.
  const apiBase = location.origin.replace(/(\/\/[^.]*?)dashboard/, "$1api");
  const app = document.getElementById("app");
  let desc = null, uploadedCogId = null, fileName = null, inputFile = null, runStartMs = 0, runPassword = "";

  function api(path, opts) {
    opts = opts || {};
    if (runPassword) opts.headers = Object.assign({}, opts.headers || {}, { "X-Run-Password": runPassword });
    return fetch(apiBase + "/embed/run/" + token + path, opts);
  }
  function el(html) { const d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstChild; }
  function blocked(msg) { app.innerHTML = '<div class="blocked">' + msg + '</div>'; }
  const esc = function (v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  async function init() {
    if (!token) return blocked("Missing run link token.");
    let r;
    try { r = await api(""); } catch (e) { return blocked("Could not reach the service."); }
    if (r.status === 404) return blocked("This run link is no longer available.");
    if (!r.ok) return blocked("This run link is unavailable.");
    desc = await r.json();
    if (desc.requires_password && !desc.unlocked) return showGate();
    render();
  }

  // Password wall for protected apps. Re-checks via describe (which reports
  // `unlocked` when the X-Run-Password header matches); the password is then
  // sent on every upload/run call.
  function showGate(errMsg) {
    app.innerHTML = "";
    app.appendChild(el("<h1>" + esc(desc.label || "Protected app") + "</h1>"));
    app.appendChild(el('<p class="status" style="margin:6px 0 12px;text-align:left">🔒 This app is password protected.</p>'));
    app.appendChild(el('<input id="pw" type="password" aria-label="Password" placeholder="Enter password" style="width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)">'));
    if (errMsg) app.appendChild(el('<p style="color:#dc2626;font-size:13px;margin-top:6px">' + errMsg + "</p>"));
    const btn = el('<button class="run" id="unlock">Unlock</button>');
    app.appendChild(btn);
    app.appendChild(el('<div class="note">Powered by MOD</div>'));
    const submit = async function () {
      const pw = document.getElementById("pw").value;
      if (!pw) return;
      runPassword = pw;
      btn.disabled = true; btn.textContent = "Checking…";
      let r; try { r = await api(""); } catch (e) { runPassword = ""; return showGate("Could not reach the service."); }
      const j = await r.json().catch(() => ({}));
      if (j && j.unlocked) { desc = j; render(); }
      else { runPassword = ""; showGate("Incorrect password — try again."); }
    };
    btn.onclick = submit;
    document.getElementById("pw").addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
  }

  function render() {
    const isText = desc.input_kind === "text";
    const settings = (desc.exposed_inputs || []).map(function (s) {
      if (typeof s === "string") {
        return '<div class="field"><label>' + esc(s) + '</label>' +
               '<input data-k="' + esc(s) + '" aria-label="' + esc(s) + '" value=""></div>';
      }
      const name = s.name;
      const label = s.label || name;
      const def = (s.default != null) ? s.default : "";
      const hint = s.description
        ? '<div class="hint" style="font-size:11px;color:var(--muted);margin-top:2px">' + esc(s.description) + '</div>'
        : "";
      let control;
      if (Array.isArray(s.enum) && s.enum.length) {
        // Enum → dropdown, using enum_labels for the display text.
        const opts = s.enum.map(function (v) {
          const lbl = (s.enum_labels && s.enum_labels[v] != null) ? s.enum_labels[v] : v;
          const sel = (String(v) === String(def)) ? " selected" : "";
          return '<option value="' + esc(v) + '"' + sel + '>' + esc(lbl) + "</option>";
        }).join("");
        control = '<select data-k="' + esc(name) + '" aria-label="' + esc(label) + '">' + opts + "</select>";
      } else {
        const numeric = (s.type === "integer" || s.type === "number");
        control = '<input data-k="' + esc(name) + '" aria-label="' + esc(label) + '"' + (numeric ? ' type="number"' : "") +
                  ' value="' + esc(def) + '">';
      }
      return '<div class="field"><label>' + esc(label) + "</label>" + control + hint + "</div>";
    }).join("");

    app.innerHTML = "";
    app.appendChild(el('<h1>' + esc(desc.label || "Run workflow") + '</h1>'));
    app.appendChild(el(
      '<div class="tabs">' +
        '<div class="tab active" data-tab="input">Input</div>' +
        '<div class="tab" data-tab="settings">Settings</div>' +
      '</div>'));

    const input = el('<div class="panel active" id="p-input"></div>');
    if (isText) {
      input.appendChild(el('<textarea id="textinput" aria-label="Text input" placeholder="Enter your text…"></textarea>'));
    } else {
      input.appendChild(el('<div class="drop" id="drop">Drag &amp; drop a file here, or click to choose<input id="file" type="file" hidden></div>'));
      input.appendChild(el('<div class="filechip" id="filechip"></div>'));
    }
    app.appendChild(input);

    const setp = el('<div class="panel" id="p-settings"></div>');
    setp.innerHTML = settings || '<div class="status">No settings — just run it.</div>';
    app.appendChild(setp);

    app.appendChild(el('<button class="run" id="runbtn">Run</button>'));
    app.appendChild(el('<div class="bar" id="bar"><div></div></div>'));
    app.appendChild(el('<div class="status" id="status"></div>'));
    app.appendChild(el('<div class="result" id="result"></div>'));
    app.appendChild(el('<div class="note">Powered by MOD</div>'));

    app.querySelectorAll(".tab").forEach(function (t) {
      t.onclick = function () {
        app.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
        app.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
        t.classList.add("active");
        document.getElementById("p-" + t.dataset.tab).classList.add("active");
      };
    });

    if (!isText) wireDrop();
    document.getElementById("runbtn").onclick = doRun;
  }

  function wireDrop() {
    const drop = document.getElementById("drop"), input = document.getElementById("file");
    drop.onclick = () => input.click();
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("hover"); };
    drop.ondragleave = () => drop.classList.remove("hover");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("hover"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); };
    input.onchange = () => { if (input.files[0]) upload(input.files[0]); };
  }

  function inputPreviewHTML(file, prefix) {
    const cap = '<div style="text-align:center;margin-top:4px">' + prefix + esc(file.name) + '</div>';
    if (file.type && file.type.indexOf("image/") === 0) {
      return '<img src="' + URL.createObjectURL(file) + '" style="max-height:180px;max-width:100%;border-radius:8px;border:1px solid var(--line);display:block;margin:8px auto 0">' + cap;
    }
    if (file.type && file.type.indexOf("video/") === 0) {
      return '<video src="' + URL.createObjectURL(file) + '" controls style="max-height:180px;max-width:100%;border-radius:8px;display:block;margin:8px auto 0"></video>' + cap;
    }
    return '<div style="text-align:center;padding:16px;color:var(--muted)">📄 ' + prefix + esc(file.name) + '</div>';
  }

  async function upload(file) {
    const chip = document.getElementById("filechip");
    chip.innerHTML = inputPreviewHTML(file, "Uploading ");  // instant local preview
    const fd = new FormData(); fd.append("file", file);
    try {
      const r = await api("/upload", { method: "POST", body: fd });
      if (!r.ok) throw new Error();
      const j = await r.json(); uploadedCogId = j.cog_id; fileName = file.name; inputFile = file;
      chip.innerHTML = inputPreviewHTML(file, "✓ ");
    } catch (e) { chip.innerHTML = "Upload failed — try again."; }
  }

  function collectSettings() {
    const out = {};
    app.querySelectorAll("[data-k]").forEach(function (i) {
      if (i.value !== "") { const n = Number(i.value); out[i.dataset.k] = isNaN(n) ? i.value : n; }
    });
    if (desc.input_kind === "text") { const t = document.getElementById("textinput"); if (t && t.value) out.text_content = t.value; }
    return out;
  }

  function showTab(name) {
    app.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    app.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    const t = app.querySelector('.tab[data-tab="' + name + '"]'); if (t) t.classList.add("active");
    const p = document.getElementById("p-" + name); if (p) p.classList.add("active");
  }

  function fmtElapsed() {
    const s = Math.max(0, Math.round((Date.now() - runStartMs) / 1000));
    return s < 60 ? s + "s elapsed" : Math.floor(s / 60) + "m " + (s % 60) + "s elapsed";
  }

  // Rich, live progress: a message, a step counter + elapsed, and a checklist
  // of the actual workflow steps with ⏳ / ✅ / ❌ markers.
  function renderProgress(p) {
    const bar = document.getElementById("bar");
    bar.style.display = "block";
    bar.querySelector("div").style.width = ((p && p.percent) || 5) + "%";
    const st = document.getElementById("status");
    let html = '<div style="font-weight:600;color:var(--primary)">' + esc((p && p.message) || "Working…") + "</div>";
    const sub = (p && p.total)
      ? "Step " + Math.min((p.completed || 0) + 1, p.total) + " of " + p.total + " · " + fmtElapsed()
      : fmtElapsed();
    html += '<div style="font-size:12px;color:var(--muted);margin-top:2px">' + sub + "</div>";
    if (p && p.steps && p.steps.length) {
      html += '<ul style="list-style:none;padding:0;margin:12px auto 0;text-align:left;max-width:300px">';
      p.steps.forEach(function (s) {
        const icon = s.status === "done" ? "✅" : s.status === "running" ? "⏳" : s.status === "failed" ? "❌" : "◻️";
        const col = s.status === "running" ? "var(--primary)" : s.status === "failed" ? "#dc2626" : s.status === "pending" ? "var(--muted)" : "var(--fg)";
        const wt = s.status === "running" ? "600" : "400";
        html += '<li style="padding:4px 0;color:' + col + ";font-weight:" + wt + '">' + icon + " &nbsp;" + esc(s.label) + "</li>";
      });
      html += "</ul>";
    }
    st.innerHTML = html;
  }

  function runError(msg) {
    document.getElementById("bar").style.display = "none";
    document.getElementById("status").innerHTML = '<div style="color:#dc2626;font-weight:600">' + esc(msg) + "</div>";
    const btn = document.getElementById("runbtn"); btn.disabled = false; btn.textContent = "Run"; btn.onclick = doRun;
  }

  // Copy to clipboard — the embed iframe is granted `clipboard-write`
  // (see the iframe snippet), so navigator.clipboard works cross-origin.
  // Falls back to a hidden textarea + execCommand for older browsers.
  function _fallbackCopy(txt) {
    try {
      const ta = document.createElement("textarea");
      ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.focus(); ta.select();
      document.execCommand("copy"); ta.remove();
    } catch (e) {}
  }
  function copyText(txt, btn) {
    const done = function () {
      if (!btn) return;
      const t = btn.textContent; btn.textContent = "✓ Copied";
      setTimeout(function () { btn.textContent = t; }, 1800);
    };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done, function () { _fallbackCopy(txt); done(); });
        return;
      }
    } catch (e) {}
    _fallbackCopy(txt); done();
  }

  // A failed run gets a reportable card: a short, safe message + a support
  // reference (RUN-<id>) and a one-tap "Copy error details" button so the
  // visitor can send it to the app owner / dev team. The full internal error
  // stays server-side and is looked up by this reference.
  function runFailed(runId) {
    document.getElementById("bar").style.display = "none";
    const ref = "RUN-" + runId;
    // SECURITY: the copyable details must NOT contain the run-link token or any
    // token-bearing URL (location.href carries ?t=<token>). The server maps the
    // RUN-<id> reference to the full internal error, so the reference alone is
    // enough for the owner/dev team to look it up — no secret leaves the page.
    const details =
      "MOD app — run failed\n" +
      "Reference: " + ref + "\n" +
      "When: " + new Date().toISOString() + "\n" +
      "Host page: " + (document.referrer || "(unknown)");
    document.getElementById("status").innerHTML =
      '<div style="color:#dc2626;font-weight:600">❌ Something went wrong running this app.</div>' +
      '<div class="status" style="margin-top:6px">Reference <b>' + ref + '</b> — copy the details below and send them to the app owner.</div>';
    const box = document.getElementById("result"); box.innerHTML = "";
    const pre = el('<pre style="white-space:pre-wrap;word-break:break-word;text-align:left;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;color:var(--fg);margin-top:10px"></pre>');
    pre.textContent = details;
    box.appendChild(pre);
    const cp = el('<button class="run" style="background:var(--accent);margin-top:10px">📋 Copy error details</button>');
    cp.onclick = function () { copyText(details, cp); };
    box.appendChild(cp);
    const btn = document.getElementById("runbtn"); btn.disabled = false; btn.textContent = "↻ Try again"; btn.onclick = doRun;
  }

  function setDoneButtons(downloadUrl, downloadName) {
    const btn = document.getElementById("runbtn");
    btn.disabled = false; btn.textContent = "↻ Run again"; btn.onclick = resetRun;
    let dl = document.getElementById("dlbtn");
    if (!dl) { dl = el('<button class="run" id="dlbtn" style="background:var(--accent);margin-top:8px"></button>'); btn.parentNode.insertBefore(dl, btn.nextSibling); }
    if (downloadUrl) {
      dl.style.display = "block";
      dl.textContent = "⬇ Download" + (downloadName ? " " + downloadName : "");
      dl.onclick = function () { const a = document.createElement("a"); a.href = downloadUrl; a.download = downloadName || "result"; document.body.appendChild(a); a.click(); a.remove(); };
    } else { dl.style.display = "none"; }
  }

  function resetRun() {
    uploadedCogId = null; fileName = null; inputFile = null;
    render();  // rebuilds a fresh Input/Settings/Run UI
  }

  async function doRun() {
    const st = document.getElementById("status");
    const cogIds = uploadedCogId ? [uploadedCogId] : [];
    if (desc.input_kind !== "text" && !cogIds.length) { st.textContent = "Add a file first."; return; }
    const btn = document.getElementById("runbtn"); btn.disabled = true; btn.textContent = "Running…";
    document.getElementById("result").innerHTML = "";
    runStartMs = Date.now();
    renderProgress({ percent: 5, message: "Starting up…", total: 0, steps: [] });
    let r;
    try {
      r = await api("/run", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cog_ids: cogIds, settings: collectSettings() }) });
    } catch (e) { return runError("Couldn't reach the service — please try again."); }
    if (r.status === 402 || r.status === 429) {
      const j = await r.json().catch(() => ({}));
      return runError((j.detail && (j.detail.message || j.detail)) || "This app has reached its limit.");
    }
    if (!r.ok) { return runError("Couldn't start the run — please try again."); }
    const j = await r.json(); poll(j.run_id);
  }

  // Resolve the postMessage target origin for the run-complete event.
  // SECURITY: never "*" — a "*" post is readable by ANY ancestor frame, which
  // is how the run-link token used to leak. Use the link's configured
  // frame_ancestors ONLY when the API supplies a single concrete http(s)
  // origin; "*", 'self', empty, or a multi-origin list all fall back to
  // same-origin, so a cross-origin embed simply won't receive the event
  // (fail closed) rather than broadcasting it.
  function completionTargetOrigin() {
    try {
      const fa = (desc && desc.frame_ancestors) ? String(desc.frame_ancestors).trim() : "";
      if (fa && fa !== "*") {
        const parts = fa.split(/\s+/).filter(Boolean);
        if (parts.length === 1 && /^https?:\/\/[^\s'"*]+$/.test(parts[0])) {
          return parts[0].replace(/\/$/, "");
        }
      }
    } catch (e) {}
    return location.origin;
  }

  async function poll(runId) {
    const tick = async function () {
      let r; try { r = await api("/runs/" + runId); } catch (e) { return setTimeout(tick, 2000); }
      const j = await r.json();
      renderProgress(j.progress);
      if (!j.terminal) return setTimeout(tick, 2000);
      document.getElementById("bar").querySelector("div").style.width = "100%";
      // Notify the embedding parent that the run finished. SECURITY: send ONLY
      // {runId, status} — never the run-link token or any token-bearing URL.
      // The token authorizes running the app / reading outputs / draining the
      // owner's credits; a "*" post carrying it let any embedding site harvest
      // it. The parent fetches results through its own authorized path.
      parent.postMessage({ type: "mod:run:complete", runId: runId, status: j.status },
        completionTargetOrigin());
      if (j.succeeded) { await showResult(runId); }
      else { runFailed(runId); }
    };
    tick();
  }

  async function showResult(runId) {
    let outs = [];
    try { const r = await api("/runs/" + runId + "/outputs"); outs = (await r.json()).outputs || []; } catch (e) {}
    document.getElementById("status").innerHTML = '<div style="font-weight:600;color:#059669">✓ Done</div>';
    const box = document.getElementById("result"); box.innerHTML = "";

    const imgOut = outs.find(function (o) { return /image\//.test((o.content_type || "").toLowerCase()); });
    const inputIsImage = inputFile && inputFile.type && inputFile.type.indexOf("image/") === 0;

    if (imgOut && inputIsImage) {
      // Comparison goes IN the input/run area (replaces the dropzone).
      const panel = document.getElementById("p-input");
      panel.innerHTML =
        '<div class="compare">' +
          '<figure><img src="' + URL.createObjectURL(inputFile) + '"><figcaption>Input</figcaption></figure>' +
          '<figure><img src="' + imgOut.url + '"><figcaption>Output</figcaption></figure>' +
        "</div>";
      const tabBtn = app.querySelector('.tab[data-tab="input"]'); if (tabBtn) tabBtn.textContent = "Result";
      showTab("input");
      setDoneButtons(imgOut.url, imgOut.name);
      const extras = outs.filter(function (o) { return o !== imgOut; });
      if (extras.length) {
        const ex = el('<div style="text-align:center;margin-top:10px;font-size:13px;color:var(--muted)">Also: </div>');
        extras.forEach(function (o) { ex.appendChild(el('<a href="' + esc(o.url) + '" download style="color:var(--primary);margin:0 6px">' + esc(o.name || "file") + "</a>")); });
        box.appendChild(ex);
      }
      return;
    }

    // Fallback: non-image output (or non-image input) — show in the result area.
    if (!outs.length) { box.innerHTML = '<div class="status">Run complete — no downloadable output produced.</div>'; setDoneButtons(null); return; }
    box.innerHTML = '<h3 style="font-size:15px;font-weight:600;margin:8px 0">Result</h3>';
    outs.forEach(function (o) {
      const type = (o.content_type || "").toLowerCase();
      const card = el('<div style="margin-bottom:14px"></div>');
      if (/image\//.test(type)) card.appendChild(el('<img src="' + o.url + '">'));
      else if (/video\//.test(type)) card.appendChild(el('<video src="' + o.url + '" controls></video>'));
      card.appendChild(el('<div><a class="dl" href="' + esc(o.url) + '" download>Download ' + esc(o.name || "result") + "</a></div>"));
      box.appendChild(card);
    });
    setDoneButtons(outs[0].url, outs[0].name);
  }

  init();
})();
