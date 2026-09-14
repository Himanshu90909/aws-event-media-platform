/* Event Media Platform — dashboard logic (vanilla JS, no build step) */
"use strict";

const API = "/api/jobs";
const LS_KEY = "emp.jobs.v1";
const PROCESS_MS = 2000, COMPLETE_MS = 6000;

const $ = (s) => document.querySelector(s);
const form = $("#jobForm"), list = $("#jobList"), empty = $("#emptyState");

/* ---------------- local job registry (receipts are durable records) ---- */
function loadJobs() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "[]"); } catch { return []; }
}
function saveJobs(jobs) { localStorage.setItem(LS_KEY, JSON.stringify(jobs.slice(0, 50))); }
function upsertJob(job) {
  const jobs = loadJobs();
  const i = jobs.findIndex((j) => j.jobId === job.jobId);
  if (i >= 0) jobs[i] = job; else jobs.unshift(job);
  saveJobs(jobs);
  render();
}

/* ---------------- API ------------------------------------------------- */
async function createJob(fileName, contentType, simulate) {
  const res = await fetch(API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fileName, contentType, ...(simulate ? { simulate: "failure" } : {}) }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  upsertJob({ jobId: data.jobId, status: data.status, objectKey: data.objectKey,
              receipt: data.receipt, fileName, contentType, createdAt: Date.now() });
  return data;
}

async function pollJob(job) {
  const url = `${API}/${job.jobId}?receipt=${encodeURIComponent(job.receipt)}`;
  const res = await fetch(url);
  if (!res.ok) return null;
  return res.json();
}

/* ---------------- rendering -------------------------------------------- */
const STATUS_META = {
  QUEUED:     { label: "QUEUED",     cls: "queued" },
  PROCESSING: { label: "PROCESSING", cls: "processing" },
  COMPLETED:  { label: "COMPLETED",  cls: "completed" },
  FAILED:     { label: "FAILED",     cls: "failed" },
};

function progressOf(status, createdAt) {
  const elapsed = Date.now() - createdAt;
  if (status === "COMPLETED") return 100;
  if (status === "FAILED") return 100;
  if (status === "QUEUED") return Math.min(33, (elapsed / PROCESS_MS) * 33);
  return Math.min(99, 33 + ((elapsed - PROCESS_MS) / (COMPLETE_MS - PROCESS_MS)) * 66);
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function jobCard(job) {
  const meta = STATUS_META[job.status] || STATUS_META.QUEUED;
  const pct = Math.round(progressOf(job.status, job.createdAt));
  const err = job.error ? `<p class="job-error">⚠ ${escapeHtml(job.error)}</p>` : "";
  return el(`
    <article class="job" data-id="${job.jobId}">
      <div class="job-head">
        <div>
          <h3>${escapeHtml(job.fileName)}</h3>
          <p class="job-sub">${escapeHtml(job.contentType)} · <code>${job.jobId}</code></p>
        </div>
        <span class="badge ${meta.cls}">${meta.label}</span>
      </div>
      <div class="bar"><div class="fill ${meta.cls}" style="width:${pct}%"></div></div>
      <div class="bar-row"><span>${pct}%</span><span class="objkey">${escapeHtml(job.objectKey)}</span></div>
      ${err}
      <div class="job-foot">
        <span>${new Date(job.createdAt).toLocaleTimeString()}</span>
        <button class="mini" data-copy="${job.jobId}">copy cURL</button>
      </div>
    </article>`);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function curlFor(job) {
  return `curl "${location.origin}/api/jobs/${job.jobId}?receipt=${job.receipt}"`;
}

function render() {
  const jobs = loadJobs();
  $("#jobCount").textContent = jobs.length;
  empty.classList.toggle("hidden", jobs.length > 0);
  list.innerHTML = "";
  for (const job of jobs) {
    const card = jobCard(job);
    card.querySelector("[data-copy]").addEventListener("click", () => {
      navigator.clipboard.writeText(curlFor(job)).then(() => {
        const b = card.querySelector("[data-copy]");
        b.textContent = "copied!";
        setTimeout(() => (b.textContent = "copy cURL"), 1200);
      });
    });
    list.appendChild(card);
  }
}

/* ---------------- polling loop ------------------------------------------ */
let polling = null;
function startPolling() {
  if (polling) clearInterval(polling);
  polling = setInterval(async () => {
    const jobs = loadJobs().filter((j) => j.status === "QUEUED" || j.status === "PROCESSING");
    if (!jobs.length) return;
    await Promise.all(jobs.map(async (job) => {
      const data = await pollJob(job);
      if (data && data.status !== job.status) {
        upsertJob({ ...job, status: data.status, error: data.error || null });
      }
    }));
  }, 1000);
}

/* ---------------- form -------------------------------------------------- */
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#formError");
  err.classList.add("hidden");
  const btn = form.querySelector(".btn");
  btn.disabled = true; btn.textContent = "Submitting…";
  try {
    await createJob(
      $("#fileName").value.trim(),
      $("#contentType").value,
      $("#simulate").checked
    );
  } catch (ex) {
    err.textContent = ex.message;
    err.classList.remove("hidden");
  } finally {
    btn.disabled = false; btn.textContent = "Submit job";
  }
});

/* ---------------- init -------------------------------------------------- */
async function healthCheck() {
  const h = $("#apiHealth");
  try {
    const res = await fetch(API, { method: "OPTIONS" });
    h.innerHTML = res.status < 500
      ? '<span class="dot ok"></span> API live'
      : '<span class="dot bad"></span> API error';
  } catch {
    h.innerHTML = '<span class="dot bad"></span> API unreachable';
  }
}

const origin = location.origin;
$("#curlPost").textContent =
  `curl -X POST "${origin}/api/jobs" \\\n  -H "Content-Type: application/json" \\\n  -d '{"fileName":"video.mp4","contentType":"video/mp4"}'`;
$("#curlGet").textContent =
  `curl "${origin}/api/jobs/<jobId>?receipt=<receipt>"`;

render();
startPolling();
healthCheck();
