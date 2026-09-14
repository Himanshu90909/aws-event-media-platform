"""Event Media Platform — single Vercel Python entrypoint.

Public API (unchanged from the AWS SAM contract):
  POST /api/jobs            -> 202 {"jobId","status":"QUEUED","objectKey","receipt"}
  GET  /api/jobs/{jobId}    -> 200 job | 404 unknown | 400 invalid uuid
  GET  /api/jobs            -> 200 {jobs:[...]} warm-instance list

AWS mapping (serverless, no external database):
  - DynamoDB job record -> signed receipt (HMAC-SHA256 over the full record).
    POST returns it; the client stores it and presents it as `?receipt=` on
    status calls; GET verifies the signature before reconstructing state.
  - SQS worker -> lazy timestamp-driven transitions
    QUEUED -> PROCESSING -> COMPLETED | FAILED (conditional state machine).
  - S3 private object -> stable objectKey reference media/{jobId}/{fileName}.

Vercel's Python runtime serves a single entrypoint per project, so the
dynamic route is delivered via a vercel.json rewrite:
  /api/jobs/(.+)  ->  /api/jobs?jobId=$1
and this handler also accepts the path form directly when the runtime
preserves it.
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

PROCESS_AFTER = 2.0    # seconds in QUEUED before the "worker" claims the job
COMPLETE_AFTER = 6.0   # seconds total until processing finishes

# Warm per-instance cache; signed receipts remain the source of truth
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


# ------------------------------------------------------------------ state
def job_status(record: dict, now: float | None = None) -> tuple[str, str | None]:
    """Lazily advance the state machine from signed timestamps.
    Mirrors src/common/state.py transitions:
    QUEUED -> PROCESSING -> COMPLETED | FAILED (terminal)."""
    now = now if now is not None else time.time()
    elapsed = now - float(record.get("createdAt", now))
    if elapsed < PROCESS_AFTER:
        return "QUEUED", None
    if elapsed < COMPLETE_AFTER:
        return "PROCESSING", None
    if record.get("simulate") == "failure":
        return "FAILED", "Simulated processing failure: worker exhausted retries (demo mode)"
    return "COMPLETED", None


# --------------------------------------------------------------- ingest
def create_job(body: dict) -> tuple[int, dict]:
    """Validate + create. Mirrors src/ingest/app.py."""
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


# ---------------------------------------------------------------- status
def get_job(job_id: str, receipt: str | None) -> tuple[int, dict]:
    """Mirrors src/status/app.py: 200 | 404 | 400."""
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

    status, error = job_status(record)
    return 200, {
        "jobId": record["jobId"],
        "fileName": record["fileName"],
        "contentType": record["contentType"],
        "status": status,
        "createdAt": record["createdAt"],
        "updatedAt": record["createdAt"],
        "objectKey": record["objectKey"],
        "error": error,
    }


# ---------------------------------------------------------------- storage
def _flush() -> None:
    """Best-effort warm-cache persistence (same instance only)."""
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

        # dynamic route: /api/jobs/{jobId} (path form or rewrite-injected query)
        parts = [p for p in parsed.path.split("/") if p]
        job_id = ""
        if len(parts) >= 3 and parts[-2] == "jobs":
            job_id = unquote(parts[-1])
        if not job_id:
            job_id = (query.get("jobId") or [""])[0]
        receipt = (query.get("receipt") or [None])[0]

        if job_id:
            _send(self, *get_job(job_id, receipt))
            return

        # list view (warm instance convenience)
        _load()
        jobs = []
        for rec in _STORE.values():
            status, error = job_status(rec)
            jobs.append({
                "jobId": rec["jobId"],
                "fileName": rec["fileName"],
                "contentType": rec["contentType"],
                "status": status,
                "createdAt": rec["createdAt"],
                "updatedAt": rec["createdAt"],
                "objectKey": rec["objectKey"],
                "error": error,
            })
        jobs.sort(key=lambda j: j["createdAt"], reverse=True)
        _send(self, 200, {"jobs": jobs, "count": len(jobs)})
