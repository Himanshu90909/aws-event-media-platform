"""Event Media Platform — single Vercel Python entrypoint (v2: staged pipeline).

Public API (unchanged contract, richer payload):
  POST /api/jobs            -> 202 {"jobId","status":"QUEUED","objectKey","receipt"}
  GET  /api/jobs/{jobId}    -> 200 job + stage timeline | 404 | 400
  GET  /api/jobs            -> 200 {jobs:[...]} warm-instance list

Pipeline model (stateless, deterministic):
  Each job walks a 7-stage media pipeline:
    ingest -> store -> queue -> transcode -> thumbnail -> metadata -> publish
  Stage durations are derived deterministically from a hash of the jobId
  (every job looks different, zero stored state). The signed receipt is the
  durable record (HMAC-SHA256); the status endpoint recomputes the full
  timeline from (createdAt, elapsed).

  Failure mode ("simulate": "failure"): one seeded worker stage fails.
  The worker retries up to 3 attempts (SQS-style redrive with backoff),
  then the job is dead-lettered: status FAILED, attempts 3, DLQ.

AWS mapping:
  ingest/store/queue      -> API Gateway + Ingest Lambda + S3 + SQS   (status QUEUED)
  transcode..publish      -> Worker Lambda fleet (worker-1..4)        (status PROCESSING)
  3 failed attempts       -> SQS redrive policy -> DLQ                (status FAILED)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

STAGES = ["ingest", "store", "queue", "transcode", "thumbnail", "metadata", "publish"]
WORKER_FIRST = 3          # index of first worker stage
MAX_ATTEMPTS = 3
RETRY_DELAY = 1.2

_STORE: dict[str, dict] = {}
_TMP_PATH = os.path.join("/tmp", "jobs.json")


# ------------------------------------------------------------------ crypto
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> str:
    return os.environ.get("JOB_SIGNING_SECRET", "event-media-platform-dev-secret")


def sign_record(record: dict) -> str:
    payload = _b64(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_receipt(receipt: str) -> dict | None:
    try:
        payload, sig = receipt.rsplit(".", 1)
        expect = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        record = json.loads(_b64dec(payload))
        if not isinstance(record, dict) or not UUID_RE.match(str(record.get("jobId", ""))):
            return None
        return record
    except Exception:
        return None


# --------------------------------------------------------- deterministic plan
def _seed_of(job_id: str) -> int:
    return int(hashlib.md5(job_id.encode()).hexdigest()[:8], 16)


def _rand(seed: int):
    x = seed or 1
    while True:  # deterministic LCG
        x = (1664525 * x + 1013904223) % 2147483647
        yield x / 2147483647


def build_plan(job_id: str, simulate: bool) -> dict:
    """Stage durations + failure point, derived from the jobId hash."""
    r = _rand(_seed_of(job_id))
    durs = [round(0.45 + 1.35 * next(r), 2) for _ in STAGES]
    fail_stage = None
    if simulate:
        fail_stage = WORKER_FIRST + int(next(r) * 4)  # transcode..metadata
    return {"durs": durs, "failStage": fail_stage}


def worker_of(job_id: str) -> str:
    return f"worker-{1 + _seed_of(job_id) % 4}"


# --------------------------------------------------------- timeline compute
def compute_timeline(record: dict, now: float | None = None) -> dict:
    """Full pipeline state for a job at time `now` — stateless & deterministic."""
    now = now if now is not None else time.time()
    plan = build_plan(record["jobId"], record.get("simulate") == "failure")
    durs, fail_stage = plan["durs"], plan["failStage"]
    elapsed = now - float(record.get("createdAt", now))
    total = sum(durs)

    stages, t, done_dur, current, failed_final = [], 0.0, 0.0, None, False
    attempts_at_fail, fail_stage_status = 0, "pending"

    for i, name in enumerate(STAGES):
        dur = durs[i]
        if elapsed < t:
            stages.append({"name": name, "status": "pending", "attempts": 0, "duration": dur})
            if fail_stage == i:
                fail_stage_status = "pending"
            continue
        if fail_stage == i:
            cycle = dur + RETRY_DELAY
            a = min(MAX_ATTEMPTS, int((elapsed - t) / cycle) + 1)
            run_end = t + (a - 1) * cycle + dur
            if elapsed < run_end:  # attempt `a` currently executing
                st = "active" if a == 1 else "retrying"
                stages.append({"name": name, "status": st, "attempts": a, "duration": dur})
                current, attempts_at_fail = i, a
                fail_stage_status = st
            elif a >= MAX_ATTEMPTS:  # third attempt finished -> dead-letter
                stages.append({"name": name, "status": "failed", "attempts": MAX_ATTEMPTS, "duration": dur})
                failed_final, attempts_at_fail = True, MAX_ATTEMPTS
                fail_stage_status = "failed"
            else:  # redrive pause between attempts
                stages.append({"name": name, "status": "retrying", "attempts": a, "duration": dur})
                current, attempts_at_fail = i, a
                fail_stage_status = "retrying"
            continue
        # normal stage
        if elapsed < t + dur:
            stages.append({"name": name, "status": "active", "attempts": 1, "duration": dur})
            current = i
            break  # everything after is pending; fill below
        stages.append({"name": name, "status": "done", "attempts": 1, "duration": dur})
        done_dur += dur
        t += dur

    if len(stages) < len(STAGES):  # pad pending after break
        for j in range(len(stages), len(STAGES)):
            stages.append({"name": STAGES[j], "status": "pending", "attempts": 0, "duration": durs[j]})

    # overall status + progress
    if failed_final:
        status, error = "FAILED", (
            f"Stage '{STAGES[fail_stage]}' failed after {MAX_ATTEMPTS} attempts "
            f"on {worker_of(record['jobId'])}; message moved to DLQ"
        )
        progress = 100.0
    elif all(s["status"] == "done" for s in stages):
        status, error, progress = "COMPLETED", None, 100.0
    else:
        active = next((s for s in stages if s["status"] in ("active", "retrying")), None)
        if current is not None and current >= WORKER_FIRST:
            status = "PROCESSING"
        else:
            status = "QUEUED"
        error = None
        if active and active["status"] == "active":
            idx = STAGES.index(active["name"])
            progress = min(99.0, (done_dur + (elapsed - sum(durs[:idx]))) / total * 100)
        else:
            progress = done_dur / total * 100

    return {
        "status": status,
        "progress": round(progress, 1),
        "stages": stages,
        "worker": worker_of(record["jobId"]),
        "error": error,
    }


# ---------------------------------------------------------------- endpoints
def create_job(body: dict) -> tuple[int, dict]:
    file_name = body.get("fileName")
    content_type = body.get("contentType")
    if not isinstance(file_name, str) or not SAFE_NAME.fullmatch(file_name):
        return 400, {"error": "fileName must be a safe file name up to 255 characters"}
    if not isinstance(content_type, str) or not content_type.strip() or len(content_type) > 127:
        return 400, {"error": "contentType is required"}
    job_id = str(uuid.uuid4())
    record = {
        "jobId": job_id,
        "fileName": file_name,
        "contentType": content_type.strip(),
        "objectKey": f"media/{job_id}/{file_name}",
        "createdAt": time.time(),
        "simulate": "failure" if body.get("simulate") == "failure" else None,
    }
    receipt = sign_record(record)
    _STORE[job_id] = dict(record, receipt=receipt)
    _flush()
    return 202, {
        "jobId": job_id,
        "status": "QUEUED",
        "objectKey": record["objectKey"],
        "receipt": receipt,
    }


def get_job(job_id: str, receipt: str | None) -> tuple[int, dict]:
    if not job_id or not UUID_RE.match(job_id):
        return 400, {"error": "jobId must be a valid UUID"}
    record = _STORE.get(job_id)
    if record is None and receipt:
        rebuilt = verify_receipt(receipt)
        if rebuilt and rebuilt.get("jobId") == job_id:
            record = rebuilt
            _STORE[job_id] = rebuilt
    if record is None:
        return 404, {"error": "Job not found"}
    snap = compute_timeline(record)
    return 200, {
        "jobId": record["jobId"],
        "fileName": record["fileName"],
        "contentType": record["contentType"],
        "status": snap["status"],
        "progress": snap["progress"],
        "stages": snap["stages"],
        "worker": snap["worker"],
        "createdAt": record["createdAt"],
        "updatedAt": record["createdAt"],
        "objectKey": record["objectKey"],
        "error": snap["error"],
    }


def _flush() -> None:
    try:
        with open(_TMP_PATH, "w") as f:
            json.dump(list(_STORE.values()), f)
    except Exception:
        pass


def _load() -> None:
    if _STORE:
        return
    try:
        with open(_TMP_PATH) as f:
            for rec in json.load(f):
                if "jobId" in rec:
                    _STORE[rec["jobId"]] = rec
    except Exception:
        pass


def list_jobs() -> dict:
    _load()
    jobs = []
    for rec in _STORE.values():
        snap = compute_timeline(rec)
        jobs.append({
            "jobId": rec["jobId"], "fileName": rec["fileName"],
            "contentType": rec["contentType"], "objectKey": rec["objectKey"],
            "createdAt": rec["createdAt"], **snap,
        })
    jobs.sort(key=lambda j: j["createdAt"], reverse=True)
    return {"jobs": jobs, "count": len(jobs)}


# ------------------------------------------------------------------ HTTP
def _send(h: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    h.wfile.write(data)


def _cors(h: BaseHTTPRequestHandler) -> None:
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "Content-Type")


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        _cors(self)
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            _send(self, 400, {"error": "Request body must be valid JSON"})
            return
        _send(self, *create_job(body))

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        parts = [p for p in parsed.path.split("/") if p]
        job_id = ""
        if len(parts) >= 3 and parts[-2] == "jobs":
            job_id = unquote(parts[-1])
        if not job_id:
            job_id = (query.get("jobId") or [""])[0]
        receipt = (query.get("receipt") or [None])[0]
        if job_id:
            _send(self, *get_job(job_id, receipt))
        else:
            _send(self, 200, list_jobs())
