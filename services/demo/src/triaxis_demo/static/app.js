/* Triaxis demo console. Plain JS, no build step, no external requests.
 * All text from the API is inserted with textContent (never innerHTML).
 * Chat messages, transcripts and audio stay in this page's memory only. */
"use strict";

(function () {
  const C = window.TriaxisCharts;
  const COLORS = { navy: "#033495", indigo: "#1b1464", sky: "#77aced", slate: "#5a6472",
    hairline: "#d7e2f5", ok: "#2e7d5b", warn: "#a8660b", crit: "#b42318" };
  const LABELS = { live: "Live", simulated: "Simulated", demo_data: "Demo Data" };
  const VIEWS = {
    overview: ["Executive Overview", "Sovereignty, resilience and assurance at a glance."],
    playground: ["AI Playground", "Chat, speech-to-text and text-to-speech through the Triaxis API path."],
    security: ["Security Operations", "SOC alerts, breach and attack simulation (BAS) and remediation."],
    infrastructure: ["Riyadh / Jeddah", "Two active sites. Simulate a site outage and watch the failover."],
    tenants: ["Tenants & Audit", "Configuration-only onboarding and the metadata-only audit trail."],
  };

  const state = {
    tenants: [],
    tenant: null,
    brand: null,
    session: null,
    view: "overview",
    chat: [],
    infraTimer: null,
    auditTimer: null,
  };

  // ------------------------------------------------------------------ helpers

  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.appendChild(typeof child === "string" || typeof child === "number"
        ? document.createTextNode(String(child)) : child);
    }
    return node;
  }

  const badge = (p) => h("span", { class: `badge ${p}`, text: LABELS[p] || p });
  const pill = (value, text) => h("span", { class: `pill ${value}`, text: text || titleCase(value) });

  function titleCase(s) {
    return String(s).replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
  }

  function card(title, provenance, ...body) {
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: title }), provenance ? badge(provenance) : null),
      ...body);
  }

  function kv(pairs) {
    return h("dl", { class: "kv" }, pairs.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  async function api(path, opts) {
    const options = Object.assign({ headers: {} }, opts || {});
    if (options.json !== undefined) {
      options.body = JSON.stringify(options.json);
      options.headers["Content-Type"] = "application/json";
      delete options.json;
    }
    if (options.auth && state.session) {
      options.headers.Authorization = `Bearer ${state.session.token}`;
      if (!options.headers["X-Triaxis-Brand"]) options.headers["X-Triaxis-Brand"] = state.brand;
    }
    delete options.auth;
    const res = await fetch(path, options);
    return res;
  }

  async function apiJson(path, opts) {
    const res = await api(path, opts);
    const body = await res.json();
    if (!res.ok) throw Object.assign(new Error(body.error ? body.error.message : res.statusText), { body, status: res.status });
    return body;
  }

  function errorText(err) {
    if (err && err.body && err.body.error) {
      const e = err.body.error;
      return `HTTP ${err.status} · ${e.code}\n${e.message}\nrequest_id ${e.request_id || "—"}`;
    }
    return String(err && err.message ? err.message : err);
  }

  // ------------------------------------------------------------------ session / context

  async function loadTenants() {
    const data = await apiJson("/api/tenants");
    state.tenants = data.tenants;
    const tenantSelect = document.getElementById("tenant-select");
    clear(tenantSelect);
    for (const t of state.tenants) tenantSelect.appendChild(h("option", { value: t.id, text: t.display_name }));
    const preferred = state.tenants.find((t) => t.id === "demo-motors") || state.tenants[0];
    tenantSelect.value = preferred.id;
    tenantSelect.addEventListener("change", () => selectTenant(tenantSelect.value));
    document.getElementById("brand-select").addEventListener("change", (e) => startSession(e.target.value));
    await selectTenant(preferred.id);
  }

  async function selectTenant(id) {
    state.tenant = state.tenants.find((t) => t.id === id);
    const brandSelect = document.getElementById("brand-select");
    clear(brandSelect);
    for (const b of state.tenant.brands) brandSelect.appendChild(h("option", { value: b.id, text: b.display_name }));
    await startSession(state.tenant.brands[0].id);
  }

  async function startSession(brandId) {
    state.brand = brandId;
    document.getElementById("brand-select").value = brandId;
    state.chat = [];
    state.session = await apiJson("/api/session", { method: "POST", json: { tenant: state.tenant.id, brand: brandId } });
    const chip = document.getElementById("session-chip");
    chip.textContent = `Token: ${state.session.scopes.length} scopes · 15 min`;
    chip.title = state.session.scopes.join("\n");
    render();
  }

  function currentBrand() { return state.tenant.brands.find((b) => b.id === state.brand); }

  // ------------------------------------------------------------------ routing

  function route() {
    const view = (location.hash || "#overview").slice(1);
    state.view = VIEWS[view] ? view : "overview";
    for (const a of document.querySelectorAll("#nav a")) {
      if (a.dataset.view === state.view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
    for (const panel of document.querySelectorAll("[data-view-panel]")) panel.hidden = panel.dataset.viewPanel !== state.view;
    document.getElementById("view-title").textContent = VIEWS[state.view][0];
    document.getElementById("view-subtitle").textContent = VIEWS[state.view][1];
    document.title = `${VIEWS[state.view][0]} · Triaxis Demo`;
    render();
  }

  function render() {
    clearInterval(state.infraTimer);
    clearInterval(state.auditTimer);
    if (!state.session) return;
    const renderers = { overview: renderOverview, playground: renderPlayground, security: renderSecurity,
      infrastructure: renderInfrastructure, tenants: renderTenants };
    renderers[state.view](document.getElementById(`view-${state.view}`)).catch((err) => {
      const root = document.getElementById(`view-${state.view}`);
      clear(root);
      root.appendChild(h("div", { class: "result error", text: errorText(err) }));
    });
  }

  // ------------------------------------------------------------------ 01 overview

  async function renderOverview(root) {
    const data = await apiJson("/api/overview");
    clear(root);
    const sov = data.sovereignty;
    const res = data.resilience;
    const asr = data.assurance;
    const lic = sov.licence;

    const sovereignty = h("div", { class: "card pillar" },
      h("div", { class: "card-head" }, h("h2", { text: "Sovereignty" }), badge(sov.provenance)),
      h("div", { class: "kpi" }, sov.residency.join(", "), h("small", { text: "  data residency" })),
      kv([
        ["Third-party AI APIs called", String(sov.third_party_ai_apis)],
        ["Tenants configured", String(sov.tenants)],
        ["Models in registry", String(sov.models.length)],
        ["Cleared for download", `${lic.downloadable} of ${sov.models.length}`],
      ]),
      h("h3", { text: "Model Licence Gate" }),
      h("div", { class: "donut-wrap" },
        C.donut([
          { label: "Allow", value: lic.allow + lic.review_approved, color: COLORS.navy },
          { label: "Needs review", value: lic.needs_review, color: COLORS.sky },
          { label: "Deny", value: lic.deny, color: COLORS.crit },
        ], { center: sov.models.length }),
        h("ul", { class: "donut-legend" },
          legendItem(COLORS.navy, `Commercial licence: ${lic.allow + lic.review_approved}`),
          legendItem(COLORS.sky, `Needs review: ${lic.needs_review}`),
          legendItem(COLORS.crit, `Denied: ${lic.deny}`))),
      h("p", { class: "caption", text: "Licences are claims until verified from each model card; nothing is downloadable before verification." }));

    const sites = Object.entries(res.sites);
    const resilience = h("div", { class: "card pillar" },
      h("div", { class: "card-head" }, h("h2", { text: "Resilience" }), badge(res.provenance)),
      h("div", { class: "kpi" }, res.mode === "balanced" ? "Option B" : "Option A",
        h("small", { text: res.mode === "balanced" ? "  balanced" : "  full mirror" })),
      kv([
        ["Real-time capacity per site", `${res.rules.realtime_capacity_per_site_pct}%`],
        ["Batch during a site outage", res.rules.queues_batch_during_outage ? "Queued, caught up" : "Fully served"],
        ["Replica recovery target", `< ${res.targets.replica_s} s`],
        ["Gateway recovery target", `< ${res.targets.gateway_s} s`],
        ["Site recovery target", `≤ ${res.targets.site_s / 60} min`],
      ]),
      h("h3", {}, "Site Status ", badge(res.site_state_provenance)),
      h("div", { class: "btn-row" }, sites.map(([name, health]) => h("span", {},
        pill(health, `${titleCase(name)}: ${titleCase(health)}`)))),
      h("p", { class: "caption" }, h("a", { href: "#infrastructure", text: "Open the failover simulation →" })));

    const tm = asr.threat_model;
    const tmTotal = tm.implemented + tm.planned || 1;
    const assurance = h("div", { class: "card pillar" },
      h("div", { class: "card-head" }, h("h2", { text: "Assurance" }), badge(asr.provenance)),
      h("div", { class: "kpi" }, String(asr.adrs), h("small", { text: "  architecture decisions recorded" })),
      h("h3", { text: "Threat Model Controls" }),
      h("div", { class: "meter" }, meterFill(tm.implemented / tmTotal)),
      h("p", { class: "caption", text: `${tm.implemented} of ${tmTotal} STRIDE controls implemented in Milestone 1; ${tm.planned} planned.` }),
      h("h3", { text: "Zero-Retention Controls" }),
      h("ul", { class: "checklist" }, asr.zero_retention_controls.map((c) =>
        h("li", {}, c.control, h("span", { class: "where", text: c.where })))),
      h("p", { class: "caption", text: `Mapped to ${asr.frameworks.join(" and ")}.` }));

    const tiles = data.slo.p95.map((t) => h("div", { class: "tile" },
      h("div", { class: "label", text: t.label }),
      h("div", { class: "value", text: t.unit === "ms" && t.value >= 1000 ? `${(t.value / 1000).toFixed(2)} s` : `${t.value} ${t.unit}` }),
      h("div", { class: "target", text: `p95 target < ${t.unit === "ms" && t.target >= 1000 ? `${t.target / 1000} s` : `${t.target} ${t.unit}`}` }),
      h("div", { class: "meter sky" }, meterFill(t.value / t.target))));

    root.appendChild(h("div", { class: "grid cols-3" }, sovereignty, resilience, assurance));
    root.appendChild(h("div", { class: "section-title" }, h("h2", { text: "Response-Time Targets" }), badge(data.slo.provenance)));
    root.appendChild(h("p", { class: "caption", text: "Engineering targets from docs/slo.md. Values shown are illustrative until load tests run on reference GPU hardware (Milestone 7)." }));
    root.appendChild(h("div", { class: "grid cols-6" }, tiles));
  }

  function legendItem(color, text) {
    const sw = h("span", { class: "swatch" });
    sw.style.background = color;
    return h("li", {}, sw, text);
  }

  function meterFill(ratio) {
    const span = h("span");
    span.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
    return span;
  }

  // ------------------------------------------------------------------ 02 playground

  let playgroundTab = "chat";

  async function renderPlayground(root) {
    clear(root);
    const brand = currentBrand();
    const tabs = [["chat", "Chat", "llm"], ["stt", "Speech-to-Text", "stt"], ["tts", "Text-to-Speech", "tts"]];
    const panel = h("div");
    const tabBar = h("div", { class: "tabs", role: "tablist" }, tabs.map(([id, label]) =>
      h("button", { class: "tab", role: "tab", "aria-selected": String(id === playgroundTab),
        onclick: () => { playgroundTab = id; renderPlayground(root); }, text: label })));

    const otherBrand = state.tenant.brands.find((b) => b.id !== state.brand);
    const scopeResult = h("div", { class: "result", hidden: true });
    const strip = card("Request Identity", "live",
      h("div", { class: "scope-strip" }, "Calling as",
        h("span", { class: "chip", text: `${state.tenant.id} · ${brand.id}` }), "with scopes",
        state.session.scopes.map((s) => h("span", { class: "chip mono", text: s }))),
      h("p", { class: "caption", text: "Each request carries a 15-minute ES256 token. The token is signed by a demo identity provider (simulated Keycloak) and validated by the real Triaxis TokenValidator and scope check." }),
      otherBrand ? h("div", { class: "btn-row" },
        h("button", { class: "btn secondary small", text: `Try to call as ${otherBrand.display_name} with this token`,
          onclick: async () => {
            const res = await api("/api/chat", { method: "POST", auth: true,
              headers: { "X-Triaxis-Brand": otherBrand.id },
              json: { messages: [{ role: "user", content: "cross-brand test" }] } });
            const body = await res.json();
            scopeResult.hidden = false;
            scopeResult.className = res.status === 403 ? "result success" : "result error";
            scopeResult.textContent = res.status === 403
              ? `Denied as expected: HTTP 403 · ${body.error.code}\n${body.error.message}`
              : `Unexpected: HTTP ${res.status}`;
          } })) : null,
      scopeResult);

    root.appendChild(h("div", { class: "grid" }, strip, h("div", { class: "card" }, tabBar, panel)));
    const allowed = new Set(brand.workloads);
    const needed = { chat: "llm", stt: "stt", tts: "tts" }[playgroundTab];
    if (!allowed.has(needed)) {
      panel.appendChild(h("p", { class: "result", text: `${brand.display_name} has no "${needed}" workload. Requests would be denied with HTTP 403 (try it: the API enforces this, not the UI).` }));
    }
    ({ chat: chatPanel, stt: sttPanel, tts: ttsPanel })[playgroundTab](panel);
  }

  function chatPanel(panel) {
    const transcript = h("div", { class: "transcript", "aria-live": "polite" });
    const input = h("textarea", { dir: "auto", placeholder: "Ask a question in English or Arabic…", "aria-label": "Message" });
    const send = h("button", { class: "btn", text: "Send" });
    const drawAll = () => {
      clear(transcript);
      if (!state.chat.length) transcript.appendChild(h("p", { class: "empty", text: "No messages yet. Messages exist only in this browser tab." }));
      for (const m of state.chat) transcript.appendChild(m.node || (m.node = msgNode(m)));
      transcript.scrollTop = transcript.scrollHeight;
    };

    async function submit(text) {
      if (!text.trim()) return;
      send.disabled = true;
      state.chat.push({ role: "user", content: text });
      const reply = { role: "assistant", content: "" };
      state.chat.push(reply);
      drawAll();
      const body = reply.node.querySelector(".body");
      const meta = reply.node.querySelector(".meta");
      meta.textContent = "Streaming…";
      try {
        const res = await api("/api/chat", { method: "POST", auth: true,
          json: { messages: state.chat.filter((m) => m !== reply).map(({ role, content }) => ({ role, content })) } });
        if (!res.ok) {
          const err = await res.json();
          throw Object.assign(new Error(err.error.message), { body: err, status: res.status });
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const line = buffer.slice(0, idx).replace(/^data: /, "");
            buffer = buffer.slice(idx + 2);
            const evt = JSON.parse(line);
            if (evt.delta) { reply.content += evt.delta; body.textContent = reply.content; transcript.scrollTop = transcript.scrollHeight; }
            if (evt.done) meta.textContent = `${evt.model} · first token ${evt.ttft_ms} ms · ${evt.tokens_out} tokens · not stored`;
          }
        }
      } catch (err) {
        body.textContent = errorText(err);
        reply.node.classList.add("error");
        meta.textContent = "";
        state.chat.pop();
      } finally {
        send.disabled = false;
      }
    }

    send.addEventListener("click", () => { const t = input.value; input.value = ""; submit(t); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send.click(); } });
    panel.appendChild(h("div", { class: "card-head" }, h("h3", { text: "Chat Completions" }), badge("simulated")));
    panel.appendChild(transcript);
    panel.appendChild(h("div", { class: "result-input" }, input));
    panel.appendChild(h("div", { class: "btn-row" }, send,
      h("button", { class: "btn secondary small", text: "Sample: service booking (English)",
        onclick: () => submit("I need to book a service appointment for my car next week. What do you need from me?") }),
      h("button", { class: "btn secondary small", text: "Sample: Arabic",
        onclick: () => submit("أريد حجز موعد صيانة لسيارتي الأسبوع القادم، ما المطلوب مني؟") }),
      h("button", { class: "btn secondary small", text: "Clear", onclick: () => { state.chat = []; drawAll(); } })));
    panel.appendChild(h("p", { class: "caption", text: "Replies come from a deterministic mock model that streams token by token over SSE. Validation, auth, size limits and logging are the real Milestone 1 code. Open Tenants & Audit afterwards: the request is logged, its text is not." }));
    drawAll();
  }

  function msgNode(m) {
    return h("div", { class: `msg ${m.role}`, dir: "auto" },
      h("span", { class: "body", text: m.content }), h("span", { class: "meta" }));
  }

  function sttPanel(panel) {
    const language = h("select", { "aria-label": "Language" },
      h("option", { value: "en", text: "English" }), h("option", { value: "ar", text: "Arabic" }));
    const out = h("div", { class: "result", dir: "auto", text: "Record, upload or use the sample clip." });
    const fileInput = h("input", { type: "file", accept: "audio/*", hidden: true });
    const recordBtn = h("button", { class: "btn", text: "Record" });
    let recorder = null;
    let chunks = [];

    async function transcribe(blob, label) {
      out.className = "result";
      out.textContent = `Sending ${label} (${blob.size} bytes)…`;
      try {
        const res = await api(`/api/stt?language=${language.value}`, { method: "POST", auth: true,
          headers: { "Content-Type": "application/octet-stream" }, body: blob });
        const body = await res.json();
        if (!res.ok) throw Object.assign(new Error(""), { body, status: res.status });
        out.className = "result success";
        out.textContent = `${body.text}\n\nformat ${body.audio_format} (detected from file header) · ` +
          `${body.duration_ms !== null ? `${body.duration_ms} ms · ` : ""}${body.bytes} bytes · transcript simulated`;
      } catch (err) {
        out.className = "result error";
        out.textContent = errorText(err);
      }
    }

    recordBtn.addEventListener("click", async () => {
      if (recorder && recorder.state === "recording") { recorder.stop(); return; }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recorder = new MediaRecorder(stream);
        chunks = [];
        recorder.ondataavailable = (e) => chunks.push(e.data);
        recorder.onstop = () => {
          stream.getTracks().forEach((t) => t.stop());
          recordBtn.textContent = "Record";
          recordBtn.classList.remove("recording");
          transcribe(new Blob(chunks, { type: recorder.mimeType }), "recording");
        };
        recorder.start();
        recordBtn.textContent = "Stop recording";
        out.textContent = "Recording… audio stays in this tab until you stop.";
      } catch (err) {
        out.className = "result error";
        out.textContent = `Microphone unavailable: ${err.message}`;
      }
    });
    fileInput.addEventListener("change", () => { if (fileInput.files[0]) transcribe(fileInput.files[0], fileInput.files[0].name); });

    panel.appendChild(h("div", { class: "card-head" }, h("h3", { text: "Batch Transcription" }), badge("simulated")));
    panel.appendChild(h("div", { class: "btn-row" }, language, recordBtn,
      h("button", { class: "btn secondary", text: "Upload audio", onclick: () => fileInput.click() }), fileInput,
      h("button", { class: "btn secondary", text: "Use sample clip", onclick: async () => {
        const res = await fetch("/api/stt/sample");
        transcribe(await res.blob(), "sample clip");
      } }),
      h("button", { class: "btn secondary small", text: "Send a non-audio file",
        onclick: () => transcribe(new Blob(["this is plain text, not audio"]), "text file") })));
    panel.appendChild(out);
    panel.appendChild(h("p", { class: "caption", text: "Audio is validated by its magic bytes (WAV, FLAC, OGG, WebM, MP3), never by file name; WAV duration is capped at 60 s. The body is sent raw (no multipart), so nothing is spooled to disk. The transcript is a canned sample." }));
  }

  function ttsPanel(panel) {
    const text = h("textarea", { dir: "auto", "aria-label": "Text to synthesise" });
    text.value = "Welcome to the service centre. Your appointment is confirmed for Tuesday at nine. Is there anything else I can help with?";
    const voice = h("select", { "aria-label": "Voice" }, h("option", { value: "warm", text: "Warm" }), h("option", { value: "deep", text: "Deep" }));
    const out = h("div", { class: "result", text: "Synthesised audio will play here." });
    const player = h("audio", { controls: true, hidden: true });
    let url = null;

    async function synth() {
      out.className = "result";
      out.textContent = "Synthesising…";
      try {
        const started = performance.now();
        const res = await api("/api/tts", { method: "POST", auth: true, json: { input: text.value, voice: voice.value } });
        if (!res.ok) { const body = await res.json(); throw Object.assign(new Error(""), { body, status: res.status }); }
        const blob = await res.blob();
        if (url) URL.revokeObjectURL(url);
        url = URL.createObjectURL(blob);
        player.src = url;
        player.hidden = false;
        player.play().catch(() => {});
        out.className = "result success";
        out.textContent = `${res.headers.get("x-triaxis-sentences")} sentences · ${res.headers.get("x-triaxis-duration-ms")} ms of audio · ` +
          `${Math.round(performance.now() - started)} ms round trip · tone placeholder, no speech model loaded`;
      } catch (err) {
        out.className = "result error";
        out.textContent = errorText(err);
      }
    }

    panel.appendChild(h("div", { class: "card-head" }, h("h3", { text: "Speech Synthesis" }), badge("simulated")));
    panel.appendChild(text);
    panel.appendChild(h("div", { class: "btn-row" }, voice, h("button", { class: "btn", text: "Synthesise", onclick: synth })));
    panel.appendChild(player);
    panel.appendChild(out);
    panel.appendChild(h("p", { class: "caption", text: "The mock splits text at sentence boundaries (including Arabic punctuation) and renders one soft tone per word, in memory. A real voice model (Chatterbox Multilingual candidate) replaces it in Milestone 2." }));
  }

  // ------------------------------------------------------------------ 03 security

  async function renderSecurity(root) {
    const data = await apiJson("/api/security");
    clear(root);
    const soc = data.soc;
    const bas = data.bas;
    const sev = soc.by_severity;

    root.appendChild(h("div", { class: "notice" }, badge(data.provenance), h("span", { text: data.notice })));

    const socCard = card("SOC Summary", data.provenance,
      h("div", { class: "kpi" }, String(soc.open), h("small", { text: "  open alerts" })),
      C.stack([
        { label: "Critical", value: sev.critical, color: COLORS.crit },
        { label: "High", value: sev.high, color: COLORS.warn },
        { label: "Medium", value: sev.medium, color: COLORS.navy },
        { label: "Low", value: sev.low, color: COLORS.hairline },
      ]),
      kv([["Critical", String(sev.critical)], ["High", String(sev.high)], ["Medium", String(sev.medium)], ["Low", String(sev.low)]]));

    const basCard = card("Breach & Attack Simulation", data.provenance,
      h("div", { class: "kpi" }, `${bas.prevented + bas.detected}/${bas.results.length}`, h("small", { text: "  techniques stopped or detected" })),
      C.stack([
        { label: "Prevented", value: bas.prevented, color: COLORS.ok },
        { label: "Detected", value: bas.detected, color: COLORS.sky },
        { label: "Missed", value: bas.missed, color: COLORS.crit },
      ]),
      kv([["Prevented", String(bas.prevented)], ["Detected", String(bas.detected)], ["Missed", String(bas.missed)], ["Last run", bas.last_run]]));

    const remCard = card("Remediation Queue", data.provenance,
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["ID", "Action", "Severity", "Owner", "Due", "Status", ""].map((x) => h("th", { text: x })))),
        h("tbody", {}, data.remediation.map((r) => h("tr", {},
          h("td", { class: "mono", text: r.id }), h("td", { text: r.title }), h("td", {}, pill(r.severity)),
          h("td", { text: r.owner }), h("td", { text: r.due }), h("td", {}, pill(r.status)),
          h("td", {}, r.status === "verified" ? null : h("button", { class: "btn secondary small", text: "Advance",
            onclick: async () => { await apiJson(`/api/security/remediation/${r.id}/advance`, { method: "POST" }); renderSecurity(root); } }))))))));

    const alerts = card("SOC Alerts (Last 24 h)", data.provenance,
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Time", "ID", "Severity", "Source", "Site", "Alert", "Status"].map((x) => h("th", { text: x })))),
        h("tbody", {}, soc.alerts.map((a) => h("tr", {},
          h("td", { class: "mono", text: a.time }), h("td", { class: "mono", text: a.id }), h("td", {}, pill(a.severity)),
          h("td", { text: a.source }), h("td", { text: titleCase(a.site) }), h("td", { text: a.title }), h("td", {}, pill(a.status))))))));

    const basTable = card("BAS Results by MITRE ATT&CK Technique", data.provenance,
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Technique", "Name", "Outcome"].map((x) => h("th", { text: x })))),
        h("tbody", {}, bas.results.map((r) => h("tr", {},
          h("td", { class: "mono", text: r.technique }), h("td", { text: r.name }), h("td", {}, pill(r.outcome))))))));

    root.appendChild(h("div", { class: "grid cols-2" }, socCard, basCard));
    root.appendChild(alerts);
    root.appendChild(remCard);
    root.appendChild(basTable);
  }

  // ------------------------------------------------------------------ 04 infrastructure

  const PHASES = {
    normal: "Normal operation · 50/50",
    detecting: "Detecting outage",
    rerouting: "Rerouting real-time traffic",
    failed_over: "Failed over · single site serving",
    rebalancing: "Site restored · rebalancing",
    catching_up: "Catching up queued batch work",
    recovered: "Recovered · 50/50",
  };

  async function renderInfrastructure(root) {
    const draw = (s) => drawInfra(root, s);
    draw(await apiJson("/api/infra"));
    state.infraTimer = setInterval(async () => {
      if (state.view !== "infrastructure") return;
      draw(await apiJson("/api/infra"));
    }, 500);
  }

  function drawInfra(root, s) {
    clear(root);
    const act = async (path, json) => {
      try { drawInfra(root, await apiJson(path, { method: "POST", json })); }
      catch (err) { alert(errorText(err)); }
    };
    const active = ["detecting", "rerouting", "failed_over"].includes(s.phase);
    const controls = card("Simulation Controls", null,
      h("div", { class: "btn-row" },
        h("span", { text: "Resilience mode:" }),
        h("button", { class: `btn small ${s.mode === "balanced" ? "" : "secondary"}`, disabled: active,
          text: "Option B · Balanced", onclick: () => act("/api/infra/mode", { mode: "balanced" }) }),
        h("button", { class: `btn small ${s.mode === "mirror" ? "" : "secondary"}`, disabled: active,
          text: "Option A · Mirror", onclick: () => act("/api/infra/mode", { mode: "mirror" }) }),
        badge(s.provenance.capacity_rules)),
      h("div", { class: "btn-row" },
        h("button", { class: "btn", disabled: s.phase !== "normal" && s.phase !== "recovered", text: "Simulate Riyadh outage",
          onclick: () => act("/api/infra/failover", { site: "riyadh" }) }),
        h("button", { class: "btn", disabled: s.phase !== "normal" && s.phase !== "recovered", text: "Simulate Jeddah outage",
          onclick: () => act("/api/infra/failover", { site: "jeddah" }) }),
        h("button", { class: "btn secondary", disabled: !active, text: "Restore site", onclick: () => act("/api/infra/restore") }),
        h("button", { class: "btn secondary", text: "Reset", onclick: () => act("/api/infra/reset") })),
      h("p", { class: "caption", text: `Capacity rules are validated by the real ResilienceConfig (ADR 0004). Site behaviour is simulated; 1 s of demo time represents ${s.time_scale} s.` }));

    const site = (key) => {
      const x = s.sites[key];
      return h("div", { class: `site ${x.health === "down" ? "down" : ""}` },
        h("div", { class: "card-head" }, h("h3", { text: x.name }), pill(x.health)),
        layer("Gateway pair (Envoy · WAF · mTLS)", x.gateway_pair),
        layer("Real-time lane (priority 1)", `${x.realtime_share_pct}% of traffic`),
        layer("Batch lane (priority 2)", `${x.batch_share_pct}% of jobs`),
        layer("GPU utilisation", `${x.gpu_utilisation_pct}%`),
        h("div", { class: "meter" }, meterFill(x.gpu_utilisation_pct / 100)),
        h("div", { class: "cells" }, Array.from({ length: x.cells }, (_, i) =>
          h("div", { class: "cell", text: `Cell ${i + 1} · STT/TTS + LLM` }))));
    };
    const ra = s.sites.riyadh.realtime_share_pct;
    const ja = s.sites.jeddah.realtime_share_pct;
    const split = h("div", { class: "split" }, segment("a", ra, `Riyadh ${ra}%`), segment("b", ja, `Jeddah ${ja}%`));
    const gtm = h("div", { class: "gtm" },
      h("div", { class: "box", text: "Global traffic manager · health-checked" }), split,
      h("div", { class: "wg", text: "WireGuard: artefacts, configuration and security records only. Never customer data." }));

    const rec = s.realtime_recovery;
    const status = card("Failover Status", s.provenance.site_state,
      h("div", { class: "phase", text: PHASES[s.phase] || s.phase }),
      kv([
        ["Mode", s.mode === "balanced" ? "Option B · balanced" : "Option A · mirror"],
        ["Real-time recovery (simulated)", rec ? `${rec.simulated_seconds} s of ${rec.target_seconds} s target` : "—"],
        ["Within ≤ 5 min target", rec ? (rec.complete ? (rec.within_target ? "Yes" : "No") : "In progress") : "—"],
        ["Batch jobs queued in memory", String(s.batch_backlog_jobs)],
      ]));
    const timeline = card("Event Timeline", s.provenance.site_state,
      s.timeline.length ? h("ul", { class: "timeline" }, s.timeline.map((e) =>
        h("li", {}, h("span", { class: "t", text: e.t }), h("span", { text: e.event })))) :
        h("p", { class: "empty", text: "No events. Start a simulated outage above." }));

    root.appendChild(controls);
    root.appendChild(h("div", { class: "infra" }, site("riyadh"), gtm, site("jeddah")));
    root.appendChild(h("div", { class: "grid cols-2" }, status, timeline));
    root.appendChild(h("p", { class: "caption", text: "GPU and traffic figures are demo data. Real failover tests (replica < 60 s, gateway < 10 s, site ≤ 5 min) arrive in Milestone 8." }));
  }

  function layer(label, value) { return h("div", { class: "layer" }, h("span", { text: label }), h("strong", { text: value })); }
  function segment(cls, pct, text) {
    const span = h("span", { class: cls, text: pct >= 28 ? text : "" });
    span.style.width = `${pct}%`;
    return span;
  }

  // ------------------------------------------------------------------ 05 tenants & audit

  async function renderTenants(root) {
    clear(root);
    const t = state.tenant;
    const tenantCard = card(`${t.display_name}`, "live",
      kv([
        ["Tenant ID", t.id],
        ["Data residency", t.data_residency],
        ["Approved model origins", t.approved_origin_countries.join(", ")],
        ["Voice model", t.models.llm_voice], ["Chat model", t.models.llm_chat],
        ["STT model", t.models.stt], ["TTS model", t.models.tts],
      ]),
      h("p", { class: "caption", text: "Loaded from config/tenants/*.yaml and validated by TenantConfig. Onboarding a tenant is configuration only: this file plus a Keycloak client and scopes." }));
    const brands = h("div", { class: "brand-cards" }, t.brands.map((b) => h("div", { class: `brand-card ${b.id === state.brand ? "active" : ""}` },
      h("div", { class: "card-head" }, h("h3", { text: b.display_name }),
        b.id === state.brand ? h("span", { class: "chip", text: "Active session" }) :
          h("button", { class: "btn secondary small", text: "Switch", onclick: () => startSession(b.id) })),
      h("div", { class: "scope-strip" }, b.workloads.map((w) => h("span", { class: "chip mono", text: `${t.id}:${b.id}:${w}` }))),
      h("h3", { text: "Quotas (Per Brand)" }),
      kv(Object.entries(b.quotas).map(([w, q]) => [w.toUpperCase(), `${q.requests_per_minute} req/min · ${q.concurrent_streams} streams`])),
      h("p", { class: "caption", text: `Guardrail policy: ${b.guardrail_policy}` }))));

    const search = h("input", { type: "search", placeholder: "Search the audit log (try a phrase you sent in the Playground)", "aria-label": "Search audit log" });
    const onlyTenant = h("input", { type: "checkbox", checked: true, id: "only-tenant" });
    const auto = h("input", { type: "checkbox", checked: true, id: "auto-refresh" });
    const tableHost = h("div", { class: "table-wrap" });
    const searchNote = h("p", { class: "caption" });

    async function refresh() {
      const data = await apiJson("/api/audit?limit=200");
      const q = search.value.trim().toLowerCase();
      let rows = data.records;
      if (onlyTenant.checked) rows = rows.filter((r) => r.tenant);
      if (q) rows = rows.filter((r) => JSON.stringify(r).toLowerCase().includes(q));
      searchNote.textContent = q
        ? `${rows.length} record(s) match "${search.value}". Prompts, transcripts and audio are never logged, so content you sent cannot be found here.`
        : "Every field shown is on the allow-list schema (ADR 0007). Each record is metadata only.";
      clear(tableHost);
      tableHost.appendChild(rows.length ? h("table", {},
        h("thead", {}, h("tr", {}, ["Time (UTC)", "Event", "Tenant", "Brand", "Workload", "Status", "Duration", "In / Out", "Error code", "Request ID"].map((x) => h("th", { text: x })))),
        h("tbody", {}, rows.map((r) => h("tr", {},
          h("td", { class: "mono", text: r.ts.slice(11, 19) }), h("td", { text: r.event }),
          h("td", { text: r.tenant || "—" }), h("td", { text: r.brand || "—" }), h("td", { text: r.workload || "—" }),
          h("td", {}, r.status ? pill(r.status < 400 ? "ok" : r.status < 500 ? "high" : "critical", String(r.status)) : "—"),
          h("td", { text: r.duration_ms !== undefined ? `${r.duration_ms} ms` : "—" }),
          h("td", { text: r.bytes_in !== undefined ? `${r.bytes_in} / ${r.bytes_out} B` : "—" }),
          h("td", { text: r.error_code || "—" }), h("td", { class: "mono", text: r.request_id ? r.request_id.slice(0, 12) : "—" })))))
        : h("p", { class: "empty", text: "No matching records yet. Send a request from the AI Playground." }));
    }

    search.addEventListener("input", refresh);
    onlyTenant.addEventListener("change", refresh);
    const audit = card("Audit Log", "live",
      h("div", { class: "toolbar" }, search,
        h("label", {}, onlyTenant, " Tenant requests only"),
        h("label", {}, auto, " Auto-refresh"),
        h("button", { class: "btn secondary small", text: "Refresh", onclick: refresh })),
      searchNote, tableHost);

    root.appendChild(h("div", { class: "grid" }, tenantCard,
      card("Brands and Scopes", "live", brands), audit));
    await refresh();
    state.auditTimer = setInterval(() => { if (auto.checked && state.view === "tenants") refresh(); }, 3000);
  }

  // ------------------------------------------------------------------ boot

  async function boot() {
    const logo = new Image();
    logo.alt = "Quantum Seal — The Future of Security";
    logo.onload = () => { const l = document.getElementById("lockup"); clear(l); l.appendChild(logo); };
    logo.src = "/static/brand/quantum-seal-logo.svg";
    window.addEventListener("hashchange", route);
    try {
      const meta = await apiJson("/api/meta");
      document.getElementById("notice-text").textContent = meta.notice;
      await loadTenants();
    } catch (err) {
      document.getElementById("view-overview").appendChild(h("div", { class: "result error", text: errorText(err) }));
    }
    route();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
