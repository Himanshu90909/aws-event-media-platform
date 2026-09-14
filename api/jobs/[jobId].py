"""GET /api/jobs/{jobId} — status endpoint (Vercel port of src/status/app.py).

Same contract as the AWS SAM deployment:
  GET /jobs/{jobId} -> 200 job | 404 unknown | 400 invalid uuid

Job records are signed receipts (see api/jobs.py, POST endpoint). Pass the
receipt as a `receipt` query parameter to reconstruct state on a cold
instance — the stateless replacement for the DynamoDB lookup. The state
machine advances lazily from the signed timestamps, reproducing the
SQS-driven worker: QUEUED -> PROCESSING -> COMPLETED | FAILED.

AWS mapping:  DynamoDB get_item  -> receipt verification (HMAC-SHA256)
              SQS worker         -> lazy timestamp-driven transitions

Self-contained by design: Vercel bundles each endpoint independently.
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

_STORE: dict[str, dict] = {}  # warm per-instance cache; receipts are the truth


def _b64dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> str:
    return os.environ.get("JOB_SIGNING_SECRET", "event-media-platform-dev-secret")


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


def job_status(record: dict, now: float | None = None) -> tuple[str, str | None]:
    """Lazily advance the state machine from signed timestamps.
    Equivalent of the SQS worker transitions:
    QUEUED -> PROCESSING -> COMPLETED | FAILED (conditional, terminal)."""
    now = now if now is not None else time.time()
    elapsed = now - float(record.get("createdAt", now))
    if elapsed < PROCESS_AFTER:
        return "QUEUED", None
    if elapsed < COMPLETE_AFTER:
        return "PROCESSING", None
    if record.get("simulate") == "failure":
        return "FAILED", "Simulated processing failure: worker exhausted retries (demo mode)"
    return "COMPLETED", None


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


def _send(h: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    h.wfile.write(data)


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        job_id = ""
        if len(parts) >= 3 and parts[-2] == "jobs":
            job_id = unquote(parts[-1])
        receipt = (parse_qs(parsed.query).get("receipt") or [None])[0]
        status, payload = get_job(job_id, receipt)
        _send(self, status, payload)
