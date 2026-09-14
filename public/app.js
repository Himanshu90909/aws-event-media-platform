/* Event Media Platform — live pipeline console (v3: integration + webhooks) */
"use strict";

const API = "/api/jobs";
const LS_KEY = "emp.jobs.v3";
const STAGE_ICONS = { ingest: "⇪", store: "▣", queue: "⇄", transcode: "◧", thumbnail: "☫", metadata: "≡", publish: "✓" };

const $ = (s) => document.querySelector(s);
const list = $("#jobList"), term = $("#term");

/* ---------------- registry -------------------------------------------- */
function loadJobs() { try { return JSON.parse(localStorage.getItem(LS_KEY) || "[]"); } catch { return []; } }
function saveJobs(j) { localStorage.setItem(LS_KEY, JSON.stringify(j.slice(0, 80))); }
function upsertJob(job) {
  const jobs = loadJobs(), i = jobs.findIndex((x) => x.jobId === job.jobId);
  if (i >= 0) { jobs[i] = { ...jobs[i], ...job }; } else { jobs.unshift(job); }
  saveJobs(jobs);
}

/* ---------------- event log -------------------------------------------- */
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function ts() { return new Date().toLocaleTimeString([], { hour12: false }); }
function log(kind, msg) {
  const line = document.createElement("div");
  line.className = "ln " + kind;
  line.innerHTML = `<span class="t">${ts()}</span> ${esc(msg)}`;
  term.appendChild(line);
  while (term.children.length > 220) term.removeChild(term.firstChild);
  term.scrollTop = term.scrollHeight;
}

/* ---------------- API ---------------------------------------------------- */
async function apiPost(fileName, contentType, simulate, callbackUrl) {
  const res = await fetch(API, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fileName, contentType,
      ...(simulate ? { simulate: "failure" } : {}),
      ...(callbackUrl ? { callbackUrl } : {}) }) });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  const job = { jobId: data.jobId, status: data.status, objectKey: data.objectKey,
                receipt: data.receipt, fileName, contentType, createdAt: Date.now(),
                stages: [], worker: "?", progress: 0, error: null,
                callbackUrl: data.callbackUrl || null, callbackDelivered: false, cbLogged: false };
  upsertJob(job);
  log("info", `▸ POST /api/jobs → 202 accepted · ${fileName} [${short(data.jobId)}]`);
  log("info", `⇪ ingest λ validated + stored · objectKey ${data.objectKey}`);
  if (data.callbackUrl) log("info", `🔗 webhook armed → ${data.callbackUrl} (fires on completion)`);
  return data;
}
async function apiGet(job) {
  const res = await fetch(`${API}/${job.jobId}?receipt=${encodeURIComponent(job.receipt)}`);
  return res.ok ? res.json() : null;
}

/* ---------------- pipeline diffing → event stream ------------------------- */
function diffEvents(job, data) {
  const prev = job.stages || [], id = short(job.jobId);
  data.stages.forEach((st, i) => {
    const p = prev[i] || { status: "pending", attempts: 0 };
    if (st.status === "done" && p.status !== "done") {
      if (st.name === "queue") log("info", `⇄ ${id} enqueued to SQS · visible to workers`);
      else if (i >= 3) log("ok", `✓ ${id} ${st.name} done (${st.duration}s) on ${data.worker}`);
      else log("dim", `✓ ${id} ${st.name} done`);
    }
    if (st.status === "active" && p.status !== "active") {
      if (i >= 3) log("info", `◧ ${data.worker} claimed ${id} → ${st.name}`);
    }
    if (st.status === "retrying" && (p.attempts !== st.attempts || p.status !== "retrying")) {
      log("warn", `⚠ ${id} ${st.name} attempt ${st.attempts} failed on ${data.worker} → redrive (retry in ~1.2s)`);
    }
  });
  if (data.callbackDelivered && !job.cbLogged && data.callbackUrl) {
    log("ok", `🔗 signed webhook delivered → ${data.callbackUrl}`);
  }
  if (data.status === "COMPLETED" && job.status !== "COMPLETED") {
    const dt = ((Date.now() - job.createdAt) / 1000).toFixed(1);
    log("ok", `✓ ${id} published — pipeline complete in ${dt}s`);
  }
  if (data.status === "FAILED" && job.status !== "FAILED") {
    log("bad", `✗ ${id} 3 attempts exhausted → message moved to DLQ`);
  }
}

/* ---------------- rendering --------------------------------------------- */
const BADGE = { QUEUED: "queued", PROCESSING: "processing", COMPLETED: "completed", FAILED: "failed" };

function stageChip(st) {
  const cls = st.status, icon = STAGE_ICONS[st.name] || "";
  const att = st.attempts > 1 ? `<em>${st.attempts}/3</em>` : "";
  return `<span class="chip ${cls}">${icon} ${st.name}${att}</span>`;
}

function card(job, live) {
  const meta = BADGE[job.status] || "queued";
  const chips = (job.stages || []).map(stageChip).join("");
  const err = job.error ? `<p class="job-error">⚠ ${esc(job.error)}</p>` : "";
  const hook = job.callbackDelivered ? `<span class="dlq ok">✓ hooked</span>` : "";
  return `
  <article class="job ${live ? "" : "settled"}" data-id="${job.jobId}">
    <div class="job-head">
      <div><h3>${esc(job.fileName)}</h3>
        <p class="job-sub">${esc(job.contentType)} · <code>${short(job.jobId)}</code> · ${esc(job.worker || "")}</p></div>
      <div class="head-right">
        ${hook}
        ${job.status === "FAILED" ? '<span class="dlq">DLQ</span>' : ""}
        <span class="badge ${meta}">${job.status}</span>
      </div>
    </div>
    <div class="bar"><div class="fill ${meta}" style="width:${job.progress || 0}%"></div></div>
    <div class="bar-row"><span>${Math.round(job.progress || 0)}%</span><span class="objkey">${esc(job.objectKey)}</span></div>
    <div class="chips">${chips}</div>
    ${err}
  </article>`;
}
function short(id) { return id.slice(0, 8); }

function render() {
  const jobs = loadJobs();
  $("#jobCount").textContent = jobs.length;
  $("#emptyState").classList.toggle("hidden", jobs.length > 0);
  list.innerHTML = jobs.map((j) => card(j, j.status === "QUEUED" || j.status === "PROCESSING")).join("");

  const done = jobs.filter((j) => j.status === "COMPLETED").length;
  const failed = jobs.filter((j) => j.status === "FAILED").length;
  const active = jobs.filter((j) => j.status === "QUEUED" || j.status === "PROCESSING").length;
  const mDone = jobs.map((j) => j.doneMs).filter(Boolean);
  $("#mTotal").textContent = jobs.length;
  $("#mActive").textContent = active;
  $("#mDone").textContent = done;
  $("#mFailed").textContent = failed;
  $("#mRate").textContent = done + failed ? Math.round((done / (done + failed)) * 100) + "%" : "–";
  $("#mAvg").textContent = mDone.length ? (mDone.reduce((a, b) => a + b, 0) / mDone.length / 1000).toFixed(1) + "s" : "–";

  const activeNodes = new Set(), busy = new Set();
  let depth = 0;
  for (const j of jobs) {
    (j.stages || []).forEach((s) => {
      if (s.status === "active" || s.status === "retrying") {
        activeNodes.add(s.name);
        if (["transcode", "thumbnail", "metadata", "publish"].includes(s.name)) busy.add(j.worker);
      }
      if (s.name === "queue" && s.status !== "done") depth++;
    });
  }
  $("#qDepth").textContent = depth;
  $("#wBusy").textContent = `${busy.size || (active ? 1 : 0)}/4`;
  document.querySelectorAll(".node").forEach((n) => {
    n.classList.toggle("on", activeNodes.has(n.dataset.node) ||
      (n.dataset.node === "client" && active > 0) ||
      (n.dataset.node === "publish" && done > 0));
  });
  document.querySelectorAll(".link").forEach((l) => l.classList.toggle("on", active > 0));
  const dots = $("#dots");
  dots.innerHTML = "";
  for (let i = 0; i < Math.min(6, active); i++) {
    const d = document.createElement("span");
    d.className = "dot-fly";
    d.style.animationDuration = (2.6 + (i * 0.45 % 1.6)) + "s";
    d.style.animationDelay = (i * 0.42) + "s";
    d.style.bottom = (4 + (i * 13) % 22) + "px";
    dots.appendChild(d);
  }
}

/* ---------------- polling ------------------------------------------------- */
async function tick() {
  const active = loadJobs().filter((j) => j.status === "QUEUED" || j.status === "PROCESSING"
    || (j.callbackUrl && !j.callbackDelivered));
  await Promise.all(active.map(async (job) => {
    const data = await apiGet(job);
    if (!data || !data.status) return;
    diffEvents(job, data);
    upsertJob({ ...data, cbLogged: job.cbLogged || data.callbackDelivered,
      doneMs: data.status === "COMPLETED" ? Date.now() - job.createdAt : job.doneMs });
  }));
  render();
}

/* ---------------- form / burst --------------------------------------------- */
const SAMPLES = ["drone-4k-cut.mp4", "podcast-ep12.mp3", "keynote-master.mp4", "teaser-15s.mp4",
  "product-render.png", "interview-camA.mp4", "bts-reel.mp4", "webinar-hq.webm", "trailer-v2.mp4", "b-roll-dusk.mp4"];

async function submit(fileName, contentType, callbackUrl) {
  const chaos = +$("#chaos").value;
  const simulate = Math.random() * 100 < chaos;
  await apiPost(fileName, contentType, simulate, callbackUrl);
}

$("#jobForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#formError").classList.add("hidden");
  try { await submit($("#fileName").value.trim() || "clip.mp4", $("#contentType").value,
    $("#cbUrl").value.trim() || null); }
  catch (ex) { const el = $("#formError"); el.textContent = ex.message; el.classList.remove("hidden"); }
});

$("#burstBtn").addEventListener("click", async () => {
  const n = +$("#burstN").value;
  const chaos = +$("#chaos").value;
  log("info", `🚀 burst: launching ${n} jobs${chaos ? ` with ${chaos}% chaos` : ""}`);
  const ctype = $("#contentType").value, cb = $("#cbUrl").value.trim() || null;
  for (let i = 0; i < n; i++) {
    setTimeout(() => submit(SAMPLES[i % SAMPLES.length], ctype, cb).catch(() => {}), i * 160);
  }
});

$("#chaos").addEventListener("input", (e) => { $("#chaosVal").textContent = e.target.value + "%"; });

/* ---------------- integration snippets ------------------------------------- */
function fillSnippets() {
  const o = location.origin, cb = "https://your-app.com/webhooks/em";
  $("#snipCurl").textContent =
`curl -X POST "${o}/api/jobs" \\
  -H "Content-Type: application/json" \\
  -d '{"fileName":"clip.mp4", "callbackUrl":"${cb}"}'

# poll status
curl "${o}/api/jobs/<jobId>?receipt=<receipt>"`;
  $("#snipJs").textContent =
`const res = await fetch("${o}/api/jobs", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    fileName: "clip.mp4",        // contentType inferred
    sourceApp: "my-service",
    callbackUrl: "${cb}",        // signed webhook on completion
  }),
});
const { jobId, receipt } = await res.json();`;
  $("#snipPy").textContent =
`import requests

r = requests.post("${o}/api/jobs", json={
    "file": "clip.mp4",           # also: fileName / name
    "tags": ["ugc"],
    "callbackUrl": "${cb}",
})
job = r.json()  # {jobId, receipt, status: QUEUED}`;
  $("#snipHook").textContent =
`POST ${cb}
X-EM-Signature: sha256=<hex HMAC-SHA256 of raw body>

{"event": "job.completed",
 "jobId": "…", "status": "COMPLETED",
 "objectKey": "media/…/clip.mp4",
 "durationMs": 8130, "worker": "worker-2"}`;
}

/* ---------------- init ----------------------------------------------------- */
async function health() {
  const h = $("#apiHealth");
  try {
    const res = await fetch(API, { method: "OPTIONS" });
    h.innerHTML = res.status < 500 ? '<span class="dot ok"></span> API live · open CORS' : '<span class="dot bad"></span> API error';
  } catch { h.innerHTML = '<span class="dot bad"></span> API unreachable'; }
}

log("dim", "console attached · polling pipeline every 800ms");
fillSnippets();
render();
tick();
setInterval(tick, 800);
health();
