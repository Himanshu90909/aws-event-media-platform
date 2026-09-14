"""Event Media Platform — single Vercel Python entrypoint (v3: open integration API).

Any application can POST directly to this API:

  POST /api/jobs
    Content-Type: application/json   (or application/x-www-form-urlencoded)
    {
      "fileName":   "clip.mp4",            # also accepted: file / name / filename
      "contentType":"video/mp4",            # optional — inferred from extension
      "callbackUrl":"https://your-app/hook",# optional — signed webhook on completion
      "sourceApp":  "my-service",          # optional metadata
      "tags":       ["ugc","beta"]         # optional metadata
    }
    -> 202 {"jobId","status","objectKey","receipt","callbackUrl"}
    -> 400 on invalid input

  GET /api/jobs/{jobId}?receipt=<receipt>  -> full stage timeline
  GET /api/jobs/{jobId}                    -> works while the creating instance is warm

Webhook delivery (at-least-once, industry standard):
  when a job reaches COMPLETED or FAILED and a callbackUrl is present, the
  next status poll fires the callback synchronously:

    POST <callbackUrl>
    X-EM-Signature: sha256=<hex HMAC-SHA256 of the raw body, JOB_SIGNING_SECRET>
    {
      "event":"job.completed", "jobId":"...", "status":"COMPLETED",
      "objectKey":"media/{jobId}/{fileName}", "durationMs":8130,
      "worker":"worker-2", "error":null
    }

  Receivers verify: HMAC-SHA256(JOB_SIGNING_SECRET, raw_body) == signature.

CORS is fully open (Access-Control-Allow-Origin: *) — browsers can post too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
URL_RE = re.compile(r"^https?://[^\s]{3,2044}$", re.I)

STAGES = ["ingest", "store", "queue", "transcode", "thumbnail", "metadata", "publish"]
WORKER_FIRST = 3
MAX_ATTEMPTS = 3
RETRY_DELAY = 1.2

EXT_TYPES = {
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "pdf": "application/pdf",
}

_STORE: dict[str, dict] = {}
_CB_OK: dict[str, bool] = {}   # warm-instance webhook delivery ledger
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


def sign_payload(body: bytes) -> str:
    return "sha256=" + hmac.new(_secret().encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------- deterministic plan
def _seed_of(job_id: str) -> int:
    return int(hashlib.md5(job_id.encode()).hexdigest()[:8], 16)


def _rand(seed: int):
    x = seed or 1
    while True:
        x = (1664525 * x + 1013904223) % 2147483647
        yield x / 2147483647


def build_plan(job_id: str, simulate: bool) -> dict:
    r = _rand(_seed_of(job_id))
    durs = [round(0.45 + 1.35 * next(r), 2) for _ in STAGES]
    fail_stage = None
    if simulate:
        fail_stage = WORKER_FIRST + int(next(r) * 4)
    return {"durs": durs, "failStage": fail_stage}


def worker_of(job_id: str) -> str:
    return f"worker-{1 + _seed_of(job_id) % 4}"


def compute_timeline(record: dict, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    plan = build_plan(record["jobId"], record.get("simulate") == "failure")
    durs, fail_stage = plan["durs"], plan["failStage"]
    elapsed = now - float(record.get("createdAt", now))
    total = sum(durs)

    stages, t, done_dur, current, failed_final = [], 0.0, 0.0, None, False

    for i, name in enumerate(STAGES):
        dur = durs[i]
        if elapsed < t:
            stages.append({"name": name, "status": "pending", "attempts": 0, "duration": dur})
            continue
        if fail_stage == i:
            cycle = dur + RETRY_DELAY
            a = min(MAX_ATTEMPTS, int((elapsed - t) / cycle) + 1)
            run_end = t + (a - 1) * cycle + dur
            if elapsed < run_end:
                st = "active" if a == 1 else "retrying"
                stages.append({"name": name, "status": st, "attempts": a, "duration": dur})
                current = i
            elif a >= MAX_ATTEMPTS:
                stages.append({"name": name, "status": "failed", "attempts": MAX_ATTEMPTS, "duration": dur})
                failed_final = True
            else:
                stages.append({"name": name, "status": "retrying", "attempts": a, "duration": dur})
                current = i
            continue
        if elapsed < t + dur:
            stages.append({"name": name, "status": "active", "attempts": 1, "duration": dur})
            current = i
            break
        stages.append({"name": name, "status": "done", "attempts": 1, "duration": dur})
        done_dur += dur
        t += dur

    if len(stages) < len(STAGES):
        for j in range(len(stages), len(STAGES)):
            stages.append({"name": STAGES[j], "status": "pending", "attempts": 0, "duration": durs[j]})

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
        status = ("PROCESSING" if current is not None and current >= WORKER_FIRST else "QUEUED")
        error = None
        if active and active["status"] == "active":
            idx = STAGES.index(active["name"])
            progress = min(99.0, (done_dur + (elapsed - sum(durs[:idx]))) / total * 100)
        else:
            progress = done_dur / total * 100

    return {
        "status": status, "progress": round(progress, 1),
        "stages": stages, "worker": worker_of(record["jobId"]), "error": error,
    }


# --------------------------------------------------------- webhook delivery
def fire_callback(record: dict, snap: dict, now: float) -> bool:
    """Signed webhook POST — fired at-least-once when the job is terminal."""
    url = record.get("callbackUrl")
    if not url or snap["status"] not in ("COMPLETED", "FAILED"):
        return False
    event = "job.completed" if snap["status"] == "COMPLETED" else "job.failed"
    payload = {
        "event": event,
        "jobId": record["jobId"],
        "fileName": record["fileName"],
        "contentType": record.get("contentType"),
        "status": snap["status"],
        "objectKey": record.get("objectKey"),
        "durationMs": round((now - float(record["createdAt"])) * 1000),
        "worker": snap["worker"],
        "error": snap.get("error"),
        "signedAt": round(now * 1000),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-EM-Signature": sign_payload(body),
            "X-EM-Event": event,
            "User-Agent": "event-media-platform/1.0",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- endpoints
def create_job(body: dict) -> tuple[int, dict]:
    """Flexible ingest — accepts several field spellings, infers content type,
    stores optional callbackUrl + metadata. Mirrors src/ingest/app.py."""
    file_name = body.get("fileName") or body.get("file") or body.get("name") or body.get("filename")
    content_type = body.get("contentType") or body.get("type") or body.get("mimeType")
    if not content_type and isinstance(file_name, str) and "." in file_name:
        content_type = EXT_TYPES.get(file_name.rsplit(".", 1)[-1].lower(), "application/octet-stream")

    if not isinstance(file_name, str) or not SAFE_NAME.fullmatch(file_name):
        return 400, {"error": "fileName must be a safe file name up to 255 characters"}
    if not isinstance(content_type, str) or not content_type.strip() or len(content_type) > 127:
        return 400, {"error": "contentType is required"}

    callback = body.get("callbackUrl") or body.get("webhook") or body.get("callback")
    if callback is not None:
        if not isinstance(callback, str) or not URL_RE.fullmatch(callback.strip()):
            return 400, {"error": "callbackUrl must be a valid http(s) URL up to 2048 chars"}
        callback = callback.strip()

    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    tags = [str(t)[:64] for t in tags[:5]]
    source_app = body.get("sourceApp") or body.get("source")
    source_app = source_app[:64] if isinstance(source_app, str) else None

    job_id = str(uuid.uuid4())
    record = {
        "jobId": job_id,
        "fileName": file_name,
        "contentType": content_type.strip(),
        "objectKey": f"media/{job_id}/{file_name}",
        "createdAt": time.time(),
        "simulate": "failure" if body.get("simulate") == "failure" else None,
        "callbackUrl": callback,
        "tags": tags,
        "sourceApp": source_app,
    }
    receipt = sign_record(record)
    _STORE[job_id] = dict(record, receipt=receipt)
    _flush()
    return 202, {
        "jobId": job_id,
        "status": "QUEUED",
        "objectKey": record["objectKey"],
        "receipt": receipt,
        "callbackUrl": callback,
        "sourceApp": source_app,
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

    now = time.time()
    snap = compute_timeline(record, now=now)

    delivered = bool(_CB_OK.get(job_id))
    if not delivered and record.get("callbackUrl") and snap["status"] in ("COMPLETED", "FAILED"):
        delivered = fire_callback(record, snap, now)
        if delivered:
            _CB_OK[job_id] = True

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
        "callbackUrl": record.get("callbackUrl"),
        "callbackDelivered": delivered,
        "tags": record.get("tags", []),
        "sourceApp": record.get("sourceApp"),
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



# ---------------------------------------------------------------- job radar
RADAR_CACHE = os.path.join("/tmp", "radar_cache.json")


def _http_json(url: str, timeout: float = 4.5) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "job-radar/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def radar(q: str) -> dict:
    """Live backend-engineer openings from public job boards (no API keys).
    Cached 30 min per instance to stay well under rate limits."""
    ts = time.time()
    jobs = []
    try:
        with open(RADAR_CACHE) as f:
            cached = json.load(f)
        if ts - cached.get("ts", 0) < 1800:
            jobs = cached["jobs"]
    except Exception:
        pass
    if not jobs:
        try:  # Remotive — remote jobs, free public API
            d = _http_json("https://remotive.com/api/remote-jobs?search=backend%20engineer&limit=50")
            for j in d.get("jobs", []):
                jobs.append({
                    "id": f"rem-{j.get('id')}",
                    "title": (j.get("title") or "")[:120],
                    "company": j.get("company_name") or "?",
                    "url": j.get("url"),
                    "location": j.get("candidate_required_location") or "Remote",
                    "remote": True,
                    "tags": ([t.lower() for t in [(j.get("category") or "")] if t] or [])[:3],
                    "posted": (j.get("publication_date") or "")[:10],
                })
        except Exception:
            pass
        try:  # Arbeitnow — free public job board API
            d = _http_json("https://www.arbeitnow.com/api/job-board-api")
            for j in d.get("data", []):
                title = (j.get("title") or "").lower()
                if "backend" not in title and "back-end" not in title and "back end" not in title:
                    continue
                jobs.append({
                    "id": f"arb-{j.get('slug')}",
                    "title": (j.get("title") or "")[:120],
                    "company": j.get("company_name") or "?",
                    "url": j.get("url"),
                    "location": j.get("location") or "?",
                    "remote": bool(j.get("remote")),
                    "tags": (j.get("tags") or [])[:4],
                    "posted": (j.get("created_at") or "")[:10],
                })
        except Exception:
            pass
        try:
            with open(RADAR_CACHE, "w") as f:
                json.dump({"ts": ts, "jobs": jobs}, f)
        except Exception:
            pass
    # hard backend filter (sources can leak off-topic listings)
    def is_backend(j):
        hay = " ".join([j.get("title", "")] + [t or "" for t in j.get("tags", [])]).lower()
        return any(k in hay for k in ("backend", "back-end", "back end"))
    jobs = [j for j in jobs if is_backend(j)]
    if q:
        ql = q.lower()
        jobs = [j for j in jobs if ql in j["title"].lower() or ql in (j["company"] or "").lower()
                or any(ql in (t or "").lower() for t in j.get("tags", []))]
    return {"jobs": jobs[:60], "count": len(jobs), "fetchedAt": ts}


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
    h.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")


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
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/x-www-form-urlencoded" in ctype:
                body = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
            else:
                body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            _send(self, 400, {"error": "Request body must be valid JSON or form-encoded"})
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
        if parts and parts[-1] == "radar":
            _send(self, 200, radar((query.get("q") or [""])[0]))
            return
        receipt = (query.get("receipt") or [None])[0]
        if job_id:
            _send(self, *get_job(job_id, receipt))
        else:
            _send(self, 200, list_jobs())
