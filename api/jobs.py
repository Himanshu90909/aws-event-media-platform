"""POST /api/jobs — ingest endpoint (Vercel port of src/ingest/app.py).

Same contract as the AWS SAM deployment:
  POST /jobs  ->  202 {"jobId","status":"QUEUED","objectKey"}

Differences required by serverless/Vercel (documented in README):
  - DynamoDB job record -> signed receipt returned to the client
    (HMAC-SHA256 over the full record). The receipt is the durable,
    tamper-proof job record; the client stores it and presents it on
    status calls. No database needed.
  - S3 object -> stable objectKey reference (same key layout
    `media/{jobId}/{fileName}`); no bytes are stored in demo mode.
  - SQS queue -> the GET status endpoint advances the state machine
    lazily from the signed timestamps (QUEUED -> PROCESSING -> COMPLETED),
    reproducing the async worker behavior deterministically.
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

# ---------------------------------------------------------------- shared core
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

PROCESS_AFTER = 2.0    # seconds in QUEUED before the "worker" claims the job
COMPLETE_AFTER = 6.0   # seconds total until processing finishes

# In-process warm cache (per lambda instance; the receipt is the source of truth)
_STORE: dict[str, dict] = {}
_TMP_PATH = os.path.join("/tmp", "jobs.json")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def secret() -> str:
    return os.environ.get("JOB_SIGNING_SECRET", "event-media-platform-dev-secret")


def sign_record(record: dict) -> str:
    payload = _b64(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_receipt(receipt: str) -> dict | None:
    try:
        payload, sig = receipt.rsplit(".", 1)
        expect = hmac.new(secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        record = json.loads(_b64dec(payload))
        if not isinstance(record, dict) or not UUID_RE.match(str(record.get("jobId", ""))):
            return None
        return record
    except Exception:
        return None


def create_job(body: dict) -> tuple[int, dict]:
    """Validate + create. Mirrors src/ingest/app.py handler."""
    file_name = body.get("fileName")
    content_type = body.get("contentType")
    if not isinstance(file_name, str) or not SAFE_NAME.fullmatch(file_name):
        return 400, {"error": "fileName must be a safe file name up to 255 characters"}
    if not isinstance(content_type, str) or not content_type.strip() or len(content_type) > 127:
        return 400, {"error": "contentType is required"}
    job_id = str(uuid.uuid4())
    now = time.time()
    record = {
        "jobId": job_id,
        "fileName": file_name,
        "contentType": content_type.strip(),
        "objectKey": f"media/{job_id}/{file_name}",
        "createdAt": now,
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


def job_status(record: dict, now: float | None = None) -> tuple[str, str | None]:
    """Lazily advance the state machine from signed timestamps.
    Equivalent of the SQS-driven worker transitions:
    QUEUED -> PROCESSING -> COMPLETED | FAILED.
    """
    now = now if now is not None else time.time()
    elapsed = now - float(record.get("createdAt", now))
    if elapsed < PROCESS_AFTER:
        return "QUEUED", None
    if elapsed < COMPLETE_AFTER:
        return "PROCESSING", None
    if record.get("simulate") == "failure":
        return "FAILED", "Simulated processing failure: worker exhausted retries (demo mode)"
    return "COMPLETED", None


def _flush() -> None:
    """Best-effort warm-cache persistence for /tmp (same instance only)."""
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
                _STORE[rec["jobId"]] = rec
    except Exception:
        pass


def _cors(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def _send(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(data)


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
        status, payload = create_job(body)
        _send(self, status, payload)

    def do_GET(self):
        """Warm-instance job list (the client's localStorage + receipts is the
        durable list; this is a convenience view of what this instance saw)."""
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
