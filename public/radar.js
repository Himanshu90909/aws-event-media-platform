/* Job Radar — live backend openings + application pipeline tracker */
"use strict";

const API = "/api/radar";
const LS = "emp.radar.apps.v1";
const STAGES = ["SAVED", "APPLIED", "INTERVIEW", "OFFER"];
const $ = (s) => document.querySelector(s);

/* ---------------- tracker store ------------------------------------------ */
function loadApps() { try { return JSON.parse(localStorage.getItem(LS) || "[]"); } catch { return []; } }
function saveApps(a) { localStorage.setItem(LS, JSON.stringify(a.slice(0, 200))); }
function upsert(app) {
  const a = loadApps(), i = a.findIndex((x) => x.id === app.id);
  if (i >= 0) a[i] = app; else a.unshift(app);
  saveApps(a);
}

/* ---------------- live radar --------------------------------------------- */
let JOBS = [];

async function scan(q) {
  $("#apiHealth").innerHTML = '<span class="dot"></span> scanning…';
  try {
    const res = await fetch(q ? `${API}?q=${encodeURIComponent(q)}` : API);
    const data = await res.json();
    JOBS = data.jobs || [];
    $("#mOpen").textContent = data.count ?? JOBS.length;
    $("#apiHealth").innerHTML = '<span class="dot ok"></span> live · updated ' +
      new Date(data.fetchedAt * 1000).toLocaleTimeString();
    renderRadar();
  } catch {
    $("#apiHealth").innerHTML = '<span class="dot bad"></span> scan failed';
  }
}

function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function renderRadar() {
  const tracked = new Set(loadApps().map((a) => a.id));
  const box = $("#radarList");
  if (!JOBS.length) { box.innerHTML = '<div class="empty">No matches on this scan — try another keyword.</div>'; return; }
  box.innerHTML = JOBS.map((j) => `
    <div class="rjob">
      <div class="rjob-main">
        <p class="rtitle">${esc(j.title)}</p>
        <p class="rmeta">${esc(j.company)}${j.location ? " · " + esc(j.location) : ""}${j.remote ? ' · <span class="remote">REMOTE</span>' : ""}${j.posted ? " · " + esc(j.posted) : ""}</p>
        ${j.tags && j.tags.length ? `<p class="rtags">${j.tags.map((t) => `<span>${esc(t)}</span>`).join("")}</p>` : ""}
      </div>
      <div class="rjob-actions">
        ${tracked.has(j.id)
          ? '<span class="badge completed">✓ tracked</span>'
          : `<button class="mini" data-track="${esc(j.id)}">+ track</button>`}
        <a class="mini" href="${esc(j.url)}" target="_blank" rel="noopener">apply ↗</a>
      </div>
    </div>`).join("");
  box.querySelectorAll("[data-track]").forEach((b) =>
    b.addEventListener("click", () => {
      const j = JOBS.find((x) => String(x.id) === b.dataset.track);
      if (!j) return;
      upsert({ id: j.id, title: j.title, company: j.company, url: j.url,
               stage: "SAVED", addedAt: Date.now(), note: "" });
      renderRadar(); renderKanban();
    }));
}

/* ---------------- application pipeline ------------------------------------ */
function renderKanban() {
  const apps = loadApps();
  const k = $("#kanban");
  k.innerHTML = STAGES.map((st) => {
    const items = apps.filter((a) => a.stage === st);
    const cls = { SAVED: "queued", APPLIED: "queued", INTERVIEW: "processing", OFFER: "completed" }[st];
    return `<div class="kcol">
      <p class="khead"><span class="badge ${cls}">${st}</span> <span class="kcount">${items.length}</span></p>
      ${items.map((a) => `
        <div class="kcard" data-id="${esc(a.id)}">
          <p class="rtitle">${esc(a.title)}</p>
          <p class="rmeta">${esc(a.company)}</p>
          <div class="kacts">
            ${st !== "OFFER" ? `<button class="mini" data-adv="${esc(a.id)}">→</button>` : ""}
            <a class="mini" href="${esc(a.url)}" target="_blank" rel="noopener">↗</a>
            <button class="mini bad" data-del="${esc(a.id)}">✕</button>
          </div>
        </div>`).join("") || '<p class="kempty">—</p>'}
    </div>`;
  }).join("");

  k.querySelectorAll("[data-adv]").forEach((b) => b.addEventListener("click", () => {
    const a = loadApps().find((x) => x.id === b.dataset.adv);
    if (!a) return;
    a.stage = STAGES[Math.min(STAGES.length - 1, STAGES.indexOf(a.stage) + 1)];
    upsert(a); renderKanban(); renderRadar();
  }));
  k.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => {
    saveApps(loadApps().filter((x) => x.id !== b.dataset.del));
    renderKanban(); renderRadar();
  }));

  // metrics
  const by = (s) => apps.filter((a) => a.stage === s).length;
  $("#mSaved").textContent = by("SAVED");
  $("#mApplied").textContent = by("APPLIED");
  $("#mInterview").textContent = by("INTERVIEW");
  $("#mOffer").textContent = by("OFFER");
  const reached = by("INTERVIEW") + by("OFFER"), appliedAll = by("APPLIED") + reached;
  $("#mRate").textContent = appliedAll ? Math.round((reached / appliedAll) * 100) + "%" : "–";
}

/* ---------------- controls ------------------------------------------------- */
let qTimer = null;
$("#q").addEventListener("input", (e) => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => scan(e.target.value.trim()), 350);
});
$("#refreshBtn").addEventListener("click", () => scan($("#q").value.trim()));

scan("");
renderKanban();
