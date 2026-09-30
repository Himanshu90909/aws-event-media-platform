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
from urllib.parse import urlparse, parse_qs, unquote, urlencode

try:                              # Vercel: repo root on sys.path
    from api import career as _career
except ImportError:               # tests / local: sibling module
    import career as _career

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


# --------------------------------------------------------- opportunity types
JTYPES = ["JOB", "INTERNSHIP", "HACKATHON", "EVENT", "RESEARCH", "FELLOWSHIP",
          "INNOVATION_LAB"]
CATEGORY_TO_TYPE = {
    "Internships": "INTERNSHIP", "Hackathons": "HACKATHON",
    "Hiring Challenges": "HACKATHON", "Fellowships": "FELLOWSHIP",
    "Events": "EVENT", "Research": "RESEARCH", "Innovation Labs": "INNOVATION_LAB",
}

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
            d = _http_json("https://remotive.com/api/remote-jobs?category=software-dev&limit=100")
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
                    "posted": (time.strftime("%Y-%m-%d", time.gmtime(j["created_at"])) if isinstance(j.get("created_at"), (int, float)) else str(j.get("created_at") or "")[:10]),
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


# ----------------------------------------------------------------- config
APP_ORIGIN = os.environ.get("APP_ORIGIN", "")
SESSION_SECRET = os.environ.get("AUTH_SESSION_SECRET", "mf-dev-session-secret-change-me")
GOOGLE_CID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_SEC = os.environ.get("GOOGLE_CLIENT_SECRET", "")
MS_CID = os.environ.get("MICROSOFT_CLIENT_ID", "")
MS_SEC = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MS_TENANT = os.environ.get("MICROSOFT_TENANT", "common")
GOOGLE_REDIRECT = os.environ.get("GOOGLE_REDIRECT_URI", "/api/mf/auth/callback/google")
MS_REDIRECT = os.environ.get("MICROSOFT_REDIRECT_URI", "/api/mf/auth/callback/microsoft")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _pg_ok() -> bool:
    try:
        import psycopg2  # noqa: F401
        return True
    except Exception:
        return False


STORAGE_MODE = "postgres" if (DATABASE_URL and _pg_ok()) else "ephemeral"

SESSION_COOKIE = "mf_session"
SESSION_TTL = 60 * 60 * 24 * 7          # 7 days
PAGE_DEFAULT, PAGE_MAX = 20, 100
ALLOWED_STATUSES = ["SAVED", "APPLIED", "SCREENING", "INTERVIEW", "OFFER", "REJECTED"]
TRANSITIONS = {
    "SAVED":      {"PREPARING", "APPLIED", "REJECTED"},
    "PREPARING":  {"APPLIED", "REJECTED", "WITHDRAWN"},
    "APPLIED":    {"ASSESSMENT", "SCREENING", "INTERVIEW", "REJECTED", "WITHDRAWN"},
    "ASSESSMENT": {"INTERVIEW", "REJECTED", "WITHDRAWN"},
    "SCREENING":  {"INTERVIEW", "REJECTED", "WITHDRAWN"},
    "INTERVIEW":  {"OFFER", "REJECTED", "WITHDRAWN"},
    "OFFER":      set(),
    "REJECTED":   set(),
    "WITHDRAWN":  set(),
}
FILE_TYPES = {
    "application/pdf", "text/plain", "application/zip",
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "video/mp4", "video/webm", "audio/mpeg", "audio/wav",
}
MAX_FILE_BYTES = 25 * 1024 * 1024

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# ------------------------------------------------------------- live discovery
# Free public sources merged at request time (no API keys). Cached per
# serverless instance to stay well inside source rate limits.
_DISCOVER_TTL = 6 * 3600
_discover_cache = {"ts": 0, "items": [], "sources": {}}


def _mk_live(source, jtype, ext_id, title, company, location, remote_type, url,
             skills, posted=None, description="", category=None):
    jid = f"live-{source}-{ext_id}"
    return {"id": jid, "external_id": str(ext_id), "source": source, "type": jtype,
            "title": (title or "")[:140], "company": (company or "?")[:80],
            "category": category or ("Hackathons" if jtype == "HACKATHON" else
                                     "Internships" if jtype == "INTERNSHIP" else "Engineering"),
            "skills": [sk.lower()[:24] for sk in (skills or [])][:6],
            "location": (location or "Remote")[:80],
            "remote_type": remote_type or "Remote",
            "employment_type": "Internship" if jtype == "INTERNSHIP" else "Full-time",
            "experience_level": "Mid",
            "salary_min": None, "salary_max": None, "salary_currency": "USD",
            "is_verified": False, "apply_url": url,
            "posted_at": posted, "expires_at": time.time() + 30 * 86400,
            "description": (description or "")[:400] or f"{title} — {company}. Live from {source}.",
            "live": True}


def _fetch_remotive():
    out = []
    for cat in ("software-dev", "data"):
        d = _http_json(f"https://remotive.com/api/remote-jobs?category={cat}&limit=60", 6)
        for j in d.get("jobs", []):
            out.append(_mk_live(
                "remotive", "JOB", j.get("id"), j.get("title"), j.get("company_name"),
                j.get("candidate_required_location") or "Remote", "Remote", j.get("url"),
                (j.get("tags") or [])[:5], posted=_dt(j.get("publication_date")),
                description=(j.get("description") or "")[:400]))
    return out


def _fetch_arbeitnow():
    out = []
    d = _http_json("https://www.arbeitnow.com/api/job-board-api", 6)
    kw = ("developer", "engineer", "software", "data", "backend", "frontend",
          "machine learning", "ai", "python", "java")
    for j in d.get("data", []):
        t = (j.get("title") or "").lower()
        if not any(k in t for k in kw):
            continue
        out.append(_mk_live(
            "arbeitnow", "JOB", j.get("slug"), j.get("title"), j.get("company_name"),
            j.get("location"), "Remote" if j.get("remote") else "On-site", j.get("url"),
            j.get("tags") or [], posted=(j.get("created_at") or 0) or None,
            description=(j.get("description") or "")[:400]))
    return out


def _fetch_remoteok():
    out = []
    d = _http_json("https://remoteok.com/api", 6)
    for j in d[1:]:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        out.append(_mk_live(
            "remoteok", "JOB", j.get("id"), j.get("position"), j.get("company"),
            j.get("location") or "Remote", "Remote", j.get("url"),
            (j.get("tags") or [])[:5], posted=_dt(j.get("date"))))
    return out


def _fetch_mlh():
    for season in ("2027", "2026"):
        req = urllib.request.Request(
            f"https://mlh.io/seasons/{season}/events",
            headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=9).read().decode("utf-8", "replace")
        m = re.search(r'application/json">(.*?)</script>', html, re.S)
        if not m:
            continue
        evs = json.loads(m.group(1)).get("props", {}).get("upcomingEvents", [])
        out = []
        for e in evs:
            loc = e.get("location") or "Online"
            rtype = "Remote" if (e.get("isVirtual") or e.get("formatType") == "virtual") else "On-site"
            url = e.get("websiteUrl") or ("https://mlh.io" + (e.get("url") or f"/seasons/{season}/events"))
            out.append(_mk_live(
                "mlh", "HACKATHON", e.get("slug") or e.get("id"), e.get("name"),
                "Major League Hacking", loc, rtype, url, ["hackathon", "students"],
                posted=_dt(e.get("startsAt")), category="Hackathons",
                description=(f"MLH {season} season hackathon — {e.get('name')} "
                             f"({e.get('dateRange')}) at {loc}.")))
        if out:  # 2027 season is the live one; stop after first success
            return out
    return []


def _discover_live():
    """Aggregate live opportunities. Fetches run in parallel under a hard
    8s deadline so a slow source can never blow the serverless budget."""
    now = time.time()
    if now - _discover_cache["ts"] < _DISCOVER_TTL:
        return _discover_cache["items"], _discover_cache["sources"]
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {"remotive": ex.submit(_fetch_remotive),
                "arbeitnow": ex.submit(_fetch_arbeitnow),
                "remoteok": ex.submit(_fetch_remoteok),
                "mlh": ex.submit(_fetch_mlh)}
        done, _ = cf.wait(list(futs.values()), timeout=12)
    items, sources = [], {}
    for name, f in futs.items():
        if f not in done:
            continue
        try:
            got = f.result()
            if got:
                items += got
                sources[name] = True
        except Exception:
            pass
    seen, uniq = set(), []
    for it in items:
        k = (it["title"].lower(), it["company"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    _discover_cache.update({
        "ts": now if uniq else now - _DISCOVER_TTL + 300,  # retry sooner if all sources failed
        "items": uniq, "sources": sources})
    return uniq, sources


def _dt(v):
    """ISO/string date -> epoch seconds (best effort)."""
    if not v:
        return None
    try:
        return time.mktime(time.strptime((v or "")[:10], "%Y-%m-%d"))
    except Exception:
        return None


# ------------------------------------------------------------------ crypto
def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def make_session(uid: str) -> str:
    body = _b64(json.dumps({"uid": uid, "iat": int(time.time()), "sid": uuid.uuid4().hex},
                           separators=(",", ":")).encode())
    return f"{body}.{_sign(body.encode(), SESSION_SECRET)[:32]}"


def read_session(token: str):
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(body.encode(), SESSION_SECRET)[:32]):
            return None
        d = json.loads(_b64dec(body))
        return d.get("uid") or None
    except Exception:
        return None


def oauth_state(redirect: str) -> str:
    body = _b64(json.dumps({"n": uuid.uuid4().hex, "r": redirect[:512], "t": int(time.time())},
                           separators=(",", ":")).encode())
    return f"{body}.{_sign(body.encode(), SESSION_SECRET)[:32]}"


def verify_state(state: str, max_age: int = 600):
    try:
        body, sig = state.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(body.encode(), SESSION_SECRET)[:32]):
            return None
        d = json.loads(_b64dec(body))
        if time.time() - int(d.get("t", 0)) > max_age:
            return None
        return d
    except Exception:
        return None


# ------------------------------------------------------------ rate limits
_RATE: dict = {}


def rate_ok(key: str, limit: int, window: int = 60) -> bool:
    now = time.time()
    b = _RATE.setdefault(key, [])
    b[:] = [t for t in b if now - t < window]
    if len(b) >= limit:
        return False
    b.append(now)
    return True


# ================================================================ store(s)
class MemStore:
    """Explicit DEV fallback — data does not survive cold starts."""
    ephemeral = True

    def __init__(self):
        self.users, self.identities = {}, {}
        self.jobs, self.saved, self.apps = {}, {}, {}
        self.files, self.notes, self.audit = {}, {}, []
        self.resumes, self.consents = {}, []
        self.ingest_runs, self.verifications, self.reports = [], [], []
        self.employers, self.messages, self.learning = {}, [], {}
        self.events = {}
        self._seed_jobs()

    # -- jobs ---------------------------------------------------------
    def _add_job(self, **j):
        jid = uuid.uuid4().hex
        now = time.time()
        self.jobs[jid] = {
            "id": jid, "external_id": j.get("external_id"),
            "source": j.get("source", "seed"), "type": j.get("type") or CATEGORY_TO_TYPE.get(j["category"], "JOB"),
            "title": j["title"], "company": j["company"], "company_logo_url": None,
            "description": j["description"], "apply_url": j.get("apply_url"),
            "employment_type": j.get("employment_type", "Full-time"),
            "experience_level": j.get("experience_level", "Mid"),
            "location": j.get("location", "Remote"), "remote_type": j.get("remote_type", "Remote"),
            "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
            "salary_currency": j.get("salary_currency", "INR"), "category": j["category"],
            "skills": j.get("skills", []), "posted_at": now - j.get("age_days", 2) * 86400,
            "expires_at": now + j.get("days_left", 30) * 86400,
            "is_verified": j.get("is_verified", False), "raw_source_payload": None,
            "skills_required": j.get("skills_required", []),
            "skills_preferred": j.get("skills_preferred", []),
            "last_verified_at": now if j.get("is_verified") else None,
            "posted_by_employer": j.get("posted_by_employer"),
            "sponsored": bool(j.get("sponsored", False)),
            "created_at": now, "updated_at": now,
        }

    def _seed_jobs(self):
        S = [
            ("Senior Software Engineer", "Amazon", "Software Engineer", ["Java", "AWS", "SQL", "system design"], 3200000, 5200000, "Hyderabad, India", "Hybrid", 4, 21, True),
            ("SDE II — Payments", "Razorpay", "SDE II", ["Java", "Kafka", "SQL", "Docker"], 2800000, 4200000, "Bengaluru, India", "On-site", 1, 14, True),
            ("SDE III — Catalog Systems", "Flipkart", "SDE III", ["Java", "Spark", "Kafka", "system design"], 4500000, 6500000, "Bengaluru, India", "Hybrid", 3, 25, True),
            ("Backend Engineer — Core Ledger", "Zerodha", "Backend Engineer", ["Go", "PostgreSQL", "Kafka", "system design"], 2400000, 4000000, "Bengaluru, India", "On-site", 6, 18, True),
            ("Frontend Engineer — Design Systems", "Zoho", "Frontend Engineer", ["JavaScript", "TypeScript", "React"], 1400000, 2600000, "Chennai, India", "Hybrid", 2, 12, False),
            ("Full-Stack Engineer", "Freshworks", "Full-Stack Engineer", ["TypeScript", "React", "Node.js", "AWS"], 1800000, 3200000, "Chennai, India", "Hybrid", 5, 30, True),
            ("Platform Engineer — CI/CD", "Postman", "Platform Engineer", ["Kubernetes", "Terraform", "Go", "AWS"], 2800000, 4500000, "Bengaluru, India", "Remote", 8, 26, True),
            ("DevOps Engineer", "Swiggy", "DevOps Engineer", ["Kubernetes", "Docker", "Terraform", "GCP"], 2200000, 3600000, "Bengaluru, India", "Hybrid", 4, 20, False),
            ("Site Reliability Engineer", "Zomato", "Site Reliability Engineer", ["Kubernetes", "Go", "GCP", "system design"], 2400000, 3800000, "Gurugram, India", "Hybrid", 7, 22, True),
            ("Cloud Engineer — Infra", "Infosys", "Cloud Engineer", ["AWS", "Azure", "Terraform", "Docker"], 1200000, 2200000, "Pune, India", "On-site", 10, 28, False),
            ("Data Analyst — Growth", "CRED", "Data Analyst", ["SQL", "Python", "analytics"], 1500000, 2600000, "Bengaluru, India", "Remote", 3, 15, True),
            ("Data Engineer — Streaming", "Meesho", "Data Engineer", ["Spark", "Kafka", "SQL", "Python"], 2000000, 3400000, "Bengaluru, India", "Hybrid", 6, 24, True),
            ("Data Scientist — Pricing", "Ola", "Data Scientist", ["Python", "SQL", "NLP", "analytics"], 1800000, 3000000, "Bengaluru, India", "On-site", 9, 19, False),
            ("Machine Learning Engineer", "Groww", "Machine Learning Engineer", ["Python", "LLM", "Spark", "AWS"], 2400000, 4200000, "Bengaluru, India", "Remote", 2, 16, True),
            ("AI Engineer — Agents", "Noso Labs", "AI Engineer", ["Python", "LLM", "RAG", "CrewAI"], 2500000, 4000000, "Remote (India)", "Remote", 1, 10, True),
            ("Applied Scientist — Search", "Microsoft", "Applied Scientist", ["Python", "NLP", "computer vision", "system design"], 3500000, 6000000, "Hyderabad, India", "Hybrid", 5, 27, True),
            ("MLOps Engineer", "NVIDIA", "MLOps Engineer", ["Python", "Kubernetes", "Docker", "LLM"], 3000000, 5000000, "Pune, India", "Remote", 4, 23, True),
            ("Security Engineer — AppSec", "PhonePe", "Security Engineer", ["system design", "Docker", "Python"], 2200000, 3800000, "Bengaluru, India", "Hybrid", 8, 21, False),
            ("QA / Test Automation Engineer", "Mphasis", "QA / Test Automation Engineer", ["Java", "JavaScript", "Docker"], 900000, 1800000, "Pune, India", "Hybrid", 12, 30, False),
            ("Mobile Engineer — Android", "Dream11", "Mobile Engineer", ["Kotlin", "Java", "system design"], 1800000, 3200000, "Mumbai, India", "On-site", 7, 18, True),
            ("Embedded Engineer — Firmware", "Tata Elxsi", "Embedded Engineer", ["C++", "system design"], 1200000, 2400000, "Bengaluru, India", "On-site", 15, 26, False),
            ("Product Manager — Fintech", "Jupiter", "Product Manager", ["analytics", "SQL", "system design"], 2500000, 4200000, "Bengaluru, India", "Remote", 6, 20, True),
            ("Technical Writer — API Docs", "Hasura", "Technical Writer", ["JavaScript", "NLP"], 1000000, 2000000, "Remote (India)", "Remote", 5, 25, False),
            ("UI/UX Designer — Product", "CRED", "UI/UX Designer", ["analytics"], 1200000, 2400000, "Bengaluru, India", "Hybrid", 9, 15, False),
            ("Software Engineer Intern (2027)", "Google", "Internships", ["Python", "C++", "system design"], 0, 0, "Hyderabad, India", "On-site", 2, 12, True),
            ("Data Science Intern", "Deloitte", "Internships", ["Python", "SQL", "analytics"], 0, 0, "Mumbai, India", "Hybrid", 4, 14, True),
            ("AI/ML Intern — LLM Tooling", "HB Innovators", "Internships", ["Python", "LLM", "RAG"], 0, 0, "Remote (India)", "Remote", 1, 8, False),
            ("Freelance — LLM Automation Builder", "Upwork Client", "Freelance / Contract", ["Python", "LLM", "n8n"], 0, 0, "Remote (Global)", "Remote", 1, 21, False),
            ("Contract — React Dashboard Build", "Toptal Client", "Freelance / Contract", ["React", "TypeScript", "Node.js"], 0, 0, "Remote (Global)", "Remote", 3, 20, False),
            ("Smart India Hackathon 2026 — Finals", "Govt. of India", "Hackathons", ["Python", "Java", "JavaScript"], 0, 0, "Delhi, India", "On-site", 1, 5, True),
            ("HB Innovators Community Challenge — Agentic AI", "HB Innovators", "Hackathons", ["LLM", "RAG", "Python", "CrewAI"], 0, 0, "Online", "Remote", 1, 6, False),
            ("Amazon ML Challenge 2026 — Final Round", "Amazon", "Hiring Challenges", ["Python", "Spark", "system design"], 0, 0, "Online", "Remote", 1, 4, True),
            ("HackerRank Orchestrate Challenge", "HackerRank", "Hiring Challenges", ["Python", "NLP", "analytics"], 0, 0, "Online", "Remote", 2, 9, True),
            ("AI Fellowship — Applied GenAI", "Fractal AI", "Fellowships", ["LLM", "RAG", "Python", "computer vision"], 0, 0, "Mumbai, India", "Hybrid", 10, 40, True),
            ("Climate Tech Fellowship — Software", "Amazon Sustainability", "Fellowships", ["Python", "AWS", "analytics"], 0, 0, "Seattle, USA", "On-site", 12, 45, True),
        ]
        for row in S:
            self._add_job(title=row[0], company=row[1], category=row[2], skills=row[3],
                          salary_min=row[4] or None, salary_max=row[5] or None,
                          location=row[6], remote_type=row[7], age_days=row[8],
                          days_left=row[9], is_verified=row[10],
                          description=(f"{row[0]} at {row[1]} ({row[2]}). We are building the "
                                       f"next generation of our product and hiring hands-on "
                                       f"engineers who care about craft, ownership and "
                                       f"measurable impact. Stack highlights: "
                                       f"{', '.join(row[3])}. Competitive compensation, "
                                       "learning budget and a strong review culture."))
        try:  # curated catalog (real programs, all types) from api/opportunities.json
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "opportunities.json")
            with open(p, "r", encoding="utf-8") as f:
                for it in json.load(f).get("items", []):
                    it = dict(it)
                    it.setdefault("source", "curated")
                    self._add_job(**it)
        except Exception:
            pass

    # -- users --------------------------------------------------------
    def upsert_user_identity(self, provider, subject, email, name, avatar):
        for ident in self.identities.values():
            if ident["provider"] == provider and ident["provider_subject"] == subject:
                uid = ident["user_id"]
                u = self.users[uid]
                u["email"], u["display_name"] = email or u["email"], name or u["display_name"]
                if avatar:
                    u["avatar_url"] = avatar
                u["updated_at"] = time.time()
                return uid, u, False
        uid = uuid.uuid4().hex
        now = time.time()
        u = {"id": uid, "email": email, "display_name": name, "avatar_url": avatar,
             "headline": "", "location": "", "bio": "", "profile_visibility": "private",
             "created_at": now, "updated_at": now}
        self.users[uid] = u
        self.identities[uuid.uuid4().hex] = {
            "id": uuid.uuid4().hex, "user_id": uid, "provider": provider,
            "provider_subject": subject, "provider_email": email, "created_at": now,
        }
        return uid, u, True

    def get_user(self, uid):
        return self.users.get(uid)

    def update_user(self, uid, patch):
        u = self.users.get(uid)
        if not u:
            return None
        for k in ("display_name", "headline", "location", "bio", "profile_visibility"):
            if k in patch:
                u[k] = str(patch[k])[:2000]
        u["updated_at"] = time.time()
        return u

    def delete_user(self, uid):
        self.identities = {k: v for k, v in self.identities.items() if v["user_id"] != uid}
        self.saved.pop(uid, None)
        self.apps = {k: a for k, a in self.apps.items() if a["user_id"] != uid}
        self.files = {k: f for k, f in self.files.items() if f["user_id"] != uid}
        self.notes.pop(uid, None)
        self.resumes = {k: r for k, r in self.resumes.items() if r["user_id"] != uid}
        self.consents = [c for c in self.consents if c["user_id"] != uid]
        self.messages = [m for m in self.messages
                         if m["sender_id"] != uid and m["recipient_id"] != uid]
        self.users.pop(uid, None)

    # -- jobs queries -------------------------------------------------
    def query_jobs(self, q, category, skill, location, remote, employment, experience, page, limit,
                   jtype=None):
        now = time.time()
        items = [j for j in self.jobs.values() if j["expires_at"] > now]
        if jtype:
            items = [j for j in items if j.get("type") == jtype]
        if q:
            ql = q.lower()
            items = [j for j in items if ql in j["title"].lower() or ql in j["company"].lower()
                     or ql in j["description"].lower() or any(ql in s.lower() for s in j["skills"])]
        if category:
            items = [j for j in items if j["category"] == category]
        if skill:
            items = [j for j in items if skill.lower() in [s.lower() for s in j["skills"]]]
        if location:
            items = [j for j in items if location.lower() in j["location"].lower()]
        if remote:
            items = [j for j in items if j["remote_type"] == remote]
        if employment:
            items = [j for j in items if j["employment_type"] == employment]
        if experience:
            items = [j for j in items if j["experience_level"] == experience]
        items.sort(key=lambda j: (not j["is_verified"], -(j["posted_at"] or 0)))
        total = len(items)
        return items[(page - 1) * limit: page * limit], total

    def get_job(self, jid):
        return self.jobs.get(jid)

    def similar_jobs(self, jid, limit=6):
        j = self.jobs.get(jid)
        if not j:
            return []
        now = time.time()
        pool = [x for x in self.jobs.values() if x["id"] != jid and x["expires_at"] > now]
        pool.sort(key=lambda x: (x["category"] != j["category"],
                                 -len(set(x["skills"]) & set(j["skills"]))))
        return pool[:limit]

    # -- saved --------------------------------------------------------
    def save_job(self, uid, jid, note=""):
        self.saved.setdefault(uid, {})[jid] = {"note": note or "", "created_at": time.time()}
        return True

    def unsave_job(self, uid, jid):
        self.saved.get(uid, {}).pop(jid, None)

    def saved_jobs(self, uid):
        return [{"job_id": k, **v} for k, v in self.saved.get(uid, {}).items()]

    def is_saved(self, uid, jid):
        return jid in self.saved.get(uid, {})

    # -- applications -------------------------------------------------
    def create_application(self, uid, jid):
        for a in self.apps.values():
            if a["user_id"] == uid and a["job_id"] == jid:
                return a, False
        aid = uuid.uuid4().hex
        now = time.time()
        a = {"id": aid, "user_id": uid, "job_id": jid, "status": "SAVED",
             "applied_at": None, "next_action": "", "next_action_at": None,
             "private_notes": "", "status_history": [{"status": "SAVED", "at": now}],
             "created_at": now, "updated_at": now}
        self.apps[aid] = a
        return a, True

    def get_application(self, aid):
        return self.apps.get(aid)

    def applications(self, uid, status=None, page=1, limit=20):
        items = [a for a in self.apps.values() if a["user_id"] == uid
                 and (status is None or a["status"] == status)]
        items.sort(key=lambda a: -a["updated_at"])
        total = len(items)
        return items[(page - 1) * limit: page * limit], total

    def update_application(self, aid, patch):
        a = self.apps.get(aid)
        if not a:
            return None, "NOT_FOUND"
        if "status" in patch and patch["status"] != a["status"]:
            if patch["status"] not in TRANSITIONS.get(a["status"], set()):
                return None, "INVALID_TRANSITION"
            a["status"] = patch["status"]
            a["status_history"].append({"status": patch["status"], "at": time.time()})
            if patch["status"] == "APPLIED":
                a["applied_at"] = time.time()
        for k in ("next_action", "private_notes"):
            if k in patch:
                a[k] = str(patch[k])[:4000]
        if "next_action_at" in patch:
            a["next_action_at"] = patch["next_action_at"]
        a["updated_at"] = time.time()
        return a, None

    def delete_application(self, aid):
        self.apps.pop(aid, None)

    # -- files --------------------------------------------------------
    def create_file(self, uid, application_id, object_key, file_name, content_type, size):
        fid = uuid.uuid4().hex
        now = time.time()
        f = {"id": fid, "user_id": uid, "application_id": application_id,
             "object_key": object_key, "file_name": file_name, "content_type": content_type,
             "size_bytes": size, "checksum": None, "processing_status": "QUEUED",
             "created_at": now, "updated_at": now}
        self.files[fid] = f
        return f

    def files(self, uid):
        return sorted((f for f in self.files.values() if f["user_id"] == uid),
                      key=lambda f: -f["created_at"])

    def get_file(self, uid, fid):
        f = self.files.get(fid)
        return f if f and f["user_id"] == uid else None

    def update_file_status(self, uid, fid, status):
        f = self.get_file(uid, fid)
        if not f:
            return None
        f["processing_status"] = status
        f["updated_at"] = time.time()
        return f

    # -- career intelligence extras --------------------------------------
    def application_for(self, uid, jid):
        for a in self.apps.values():
            if a["user_id"] == uid and a["job_id"] == jid:
                return a
        return None

    def track(self, event_type, uid=None, job_id=None):
        try:
            self.events.setdefault("n", 0)
            self.events["n"] += 1
            self.events.setdefault(event_type, 0)
            self.events[event_type] += 1
        except Exception:
            pass

    def analytics_summary(self, days=30):
        return {"totalEvents": self.events.get("n", 0),
                "byType": {k: v for k, v in self.events.items() if k != "n"}}

    # -- resumes & consents ---------------------------------------------
    def save_resume(self, uid, file_name, content_type, text, consent):
        rid = uuid.uuid4().hex
        parsed = _career.parse_resume(text)
        now = time.time()
        self.resumes[rid] = {"id": rid, "user_id": uid, "file_name": file_name,
                             "content_type": content_type, "text_content": text,
                             "parsed": parsed, "consent": bool(consent),
                             "created_at": now, "updated_at": now}
        self.grant_consent(uid, "resume_processing", True, "resume uploaded")
        return self.resumes[rid]

    def latest_resume(self, uid):
        items = [r for r in self.resumes.values() if r["user_id"] == uid]
        items.sort(key=lambda r: -r["created_at"])
        return items[0] if items else None

    def delete_resume(self, uid):
        for rid in [k for k, r in self.resumes.items() if r["user_id"] == uid]:
            del self.resumes[rid]
        self.grant_consent(uid, "resume_processing", False, "resume deleted")

    def update_resume_parsed(self, rid, parsed_patch):
        r = self.resumes.get(rid)
        if not r:
            return None
        p = dict(r["parsed"])
        for k in ("skills", "projects"):
            if isinstance(parsed_patch.get(k), list):
                p[k] = parsed_patch[k]
        for k in ("education", "graduation_year", "experience_years"):
            if k in parsed_patch:
                p[k] = parsed_patch[k]
        r["parsed"] = p
        r["updated_at"] = time.time()
        return r

    def grant_consent(self, uid, kind, granted, note=""):
        self.consents.append({"id": uuid.uuid4().hex, "user_id": uid, "kind": kind,
                              "granted": bool(granted), "note": note or None,
                              "created_at": time.time()})

    def consents_of(self, uid):
        return sorted((c for c in self.consents if c["user_id"] == uid),
                      key=lambda c: c["created_at"], reverse=True)

    # -- ingestion / verification / reports -----------------------------
    def log_ingestion(self, source, status, stats=None, error=None):
        run = {"id": uuid.uuid4().hex, "source": source, "status": status,
               "stats": stats or {}, "error": error,
               "started_at": time.time(), "finished_at": time.time()}
        self.ingest_runs.append(run)
        if len(self.ingest_runs) > 200:
            self.ingest_runs = self.ingest_runs[-200:]
        return run

    def ingestion_runs(self, limit=20):
        return sorted(self.ingest_runs, key=lambda r: -r["started_at"])[:limit]

    def add_verification(self, job_id, status, note, actor):
        ev = {"id": uuid.uuid4().hex, "job_id": job_id, "status": status,
              "note": note, "actor": actor, "created_at": time.time()}
        self.verifications.append(ev)
        return ev

    def verifications_of(self, job_id):
        return sorted((v for v in self.verifications if v["job_id"] == job_id),
                      key=lambda v: -v["created_at"])[:20]

    def report_job(self, uid, job_id, reason, details):
        rep = {"id": uuid.uuid4().hex, "user_id": uid, "job_id": job_id,
               "reason": reason, "details": details or None,
               "status": "open", "created_at": time.time()}
        self.reports.append(rep)
        return rep

    # -- employers -------------------------------------------------------
    def register_employer(self, uid, company_name, website, about, logo_url):
        for e in self.employers.values():
            if e["user_id"] == uid:
                return e, False
        eid = uuid.uuid4().hex
        now = time.time()
        e = {"id": eid, "user_id": uid, "company_name": company_name,
             "website": website, "about": about, "logo_url": logo_url,
             "verification_status": "pending", "verified_at": None,
             "created_at": now, "updated_at": now}
        self.employers[eid] = e
        return e, True

    def employer_by_user(self, uid):
        for e in self.employers.values():
            if e["user_id"] == uid:
                return e
        return None

    def employer_jobs(self, eid):
        return sorted((j for j in self.jobs.values()
                       if j.get("posted_by_employer") == eid),
                      key=lambda j: -j["created_at"])

    def employer_applications(self, eid, page=1, limit=50):
        own = {j["id"] for j in self.employer_jobs(eid)}
        items = [a for a in self.apps.values() if a["job_id"] in own]
        items.sort(key=lambda a: -a["updated_at"])
        total = len(items)
        return items[(page - 1) * limit: page * limit], total

    def post_job(self, eid, raw):
        j = _career.normalize_job(raw, "employer-direct")
        now = time.time()
        j.update({"id": uuid.uuid4().hex, "is_verified": False,
                  "posted_by_employer": eid,
                  "posted_at": now,
                  "expires_at": raw.get("expires_at") or now + 60 * 86400,
                  "raw_source_payload": None,
                  "created_at": now, "updated_at": now})
        j["type"] = raw.get("type") or CATEGORY_TO_TYPE.get(j["category"], "JOB")
        self.jobs[j["id"]] = j
        return j

    def patch_employer(self, eid, key, value):
        e = self.employers.get(eid)
        if not e or key not in ("about", "website", "logo_url"):
            return None
        e[key] = value
        e["updated_at"] = time.time()
        return e

    def verify_employer(self, eid, status):
        e = self.employers.get(eid)
        if not e:
            return None
        e["verification_status"] = status
        e["verified_at"] = time.time() if status == "verified" else None
        e["updated_at"] = time.time()
        return e

    def set_job_verification(self, jid, status, actor, note=""):
        j = self.jobs.get(jid)
        if not j:
            return None
        j["is_verified"] = status == "verified"
        j["last_verified_at"] = time.time()
        self.add_verification(jid, status, note, actor)
        return j

    # -- application messaging (employer <-> candidate) -------------------
    def app_parties(self, aid):
        a = self.apps.get(aid)
        if not a:
            return None, None, None
        j = self.jobs.get(a["job_id"])
        employer = self.employers.get(j.get("posted_by_employer")) if j else None
        return a, j, employer

    def send_message(self, aid, sender_id, body):
        a, j, employer = self.app_parties(aid)
        if not a:
            return None, "NOT_FOUND"
        recipient = None
        if sender_id == a["user_id"] and employer:
            recipient = employer["user_id"]
        elif employer and sender_id == employer["user_id"]:
            recipient = a["user_id"]
        else:
            return None, "FORBIDDEN"
        m = {"id": uuid.uuid4().hex, "application_id": aid, "sender_id": sender_id,
             "recipient_id": recipient, "body": body, "read_at": None,
             "created_at": time.time()}
        self.messages.append(m)
        return m, None

    def messages_of(self, aid, uid):
        a, j, employer = self.app_parties(aid)
        if not a:
            return None, "NOT_FOUND"
        if uid != a["user_id"] and not (employer and uid == employer["user_id"]):
            return None, "FORBIDDEN"
        out = [m for m in self.messages if m["application_id"] == aid]
        for m in out:
            if m["recipient_id"] == uid and not m["read_at"]:
                m["read_at"] = time.time()
        return sorted(out, key=lambda m: m["created_at"]), None

    # -- learning progress ------------------------------------------------
    def set_progress(self, uid, role, skill, status):
        if status not in ("todo", "in_progress", "done"):
            return None
        self.learning[(uid, role, skill)] = {"status": status, "updated_at": time.time()}
        return self.learning[(uid, role, skill)]

    def progress_of(self, uid, role):
        return {s: v["status"] for (u, r, s), v in self.learning.items()
                if u == uid and r == role}

    # -- notifications -------------------------------------------------
    def notify(self, uid, ntype, title, body):
        now = time.time()
        for n in reversed(self.notes.get(uid, [])):
            if n["type"] == ntype and n["title"] == title and now - n["created_at"] < 86400:
                return None  # deduplicated within 24h
        n = {"id": uuid.uuid4().hex, "user_id": uid, "type": ntype, "title": title,
             "body": body, "read_at": None, "created_at": now}
        self.notes.setdefault(uid, []).append(n)
        return n

    def notifications(self, uid):
        return sorted(self.notes.get(uid, []), key=lambda n: -n["created_at"])

    def read_notification(self, uid, nid):
        for n in self.notes.get(uid, []):
            if n["id"] == nid:
                n["read_at"] = time.time()
                return n
        return None

    # -- audit ---------------------------------------------------------
    def audit_event(self, uid, event_type, request_id, metadata=None):
        self.audit.append({"id": uuid.uuid4().hex, "user_id": uid, "event_type": event_type,
                           "request_id": request_id, "metadata": metadata or {},
                           "created_at": time.time()})
        if len(self.audit) > 10000:
            self.audit = self.audit[-5000:]

    # -- stats ----------------------------------------------------------
    def overview(self):
        now = time.time()
        live = [j for j in self.jobs.values() if j["expires_at"] > now]
        cats = sorted({j["category"] for j in live})
        return {"jobs_live": len(live), "categories": len(cats),
                "category_list": cats, "verified": sum(1 for j in live if j["is_verified"]),
                "closing_soon": sum(1 for j in live if j["expires_at"] - now < 7 * 86400)}


class PgStore(MemStore):
    """PostgreSQL adapter (durable). All reads and writes go through Postgres;
    the inherited in-memory dicts are only the seed source for first boot."""
    ephemeral = False

    def __init__(self):
        super().__init__()          # seed source (used only for first-boot seeding)
        del self.__dict__["files"]  # un-shadow the files() method defined below
        import psycopg2
        self.pg = psycopg2.connect(DATABASE_URL)
        self.pg.autocommit = True
        self._migrate()
        self._seed_if_empty()

    def _conn_err(self, exc):
        """True when exc means the serverless PG connection went stale."""
        import psycopg2
        return isinstance(exc, (psycopg2.InterfaceError, psycopg2.OperationalError))

    def _reconnect(self):
        import psycopg2
        try:
            self.pg.close()
        except Exception:
            pass
        self.pg = psycopg2.connect(DATABASE_URL)
        self.pg.autocommit = True

    def _q(self, sql, args=()):
        import datetime as _dt
        from decimal import Decimal
        # Serverless: the pooled connection can be closed by the proxy while
        # the instance stays warm ("connection already closed"). Retry once
        # on a stale-connection error after reconnecting.
        for _attempt in range(2):
            try:
                with self.pg.cursor() as cur:
                    cur.execute(sql, args)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = []
                        for r in cur.fetchall():
                            row = {}
                            for c, v in zip(cols, r):
                                if isinstance(v, _dt.datetime):
                                    v = v.timestamp()      # epoch float, matches in-memory format
                                elif isinstance(v, _dt.date):
                                    v = v.isoformat()
                                elif isinstance(v, Decimal):
                                    v = float(v)
                                row[c] = v
                            rows.append(row)
                        return rows
                    return []
            except Exception as e:
                if _attempt == 0 and self._conn_err(e):
                    self._reconnect()
                    continue
                raise

    def _migrate(self):
        from db.schema import SCHEMA_SQL
        with self.pg.cursor() as cur:
            cur.execute(SCHEMA_SQL)

    def _seed_if_empty(self):
        if self._q("SELECT 1 AS x FROM jobs LIMIT 1"):
            return
        now = time.time()
        for row in self.jobs.values():  # use the in-memory seed as source
            self._q(
                "INSERT INTO jobs (id, external_id, source, title, company, description, apply_url,"
                " employment_type, experience_level, location, remote_type, salary_min, salary_max,"
                " salary_currency, category, skills, posted_at, expires_at, is_verified,"
                " type, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "to_timestamp(%s), to_timestamp(%s), %s, %s, to_timestamp(%s), to_timestamp(%s))"
                " ON CONFLICT (source, external_id) DO NOTHING",
                (row["id"], row["external_id"], row["source"], row["title"], row["company"],
                 row["description"], row["apply_url"], row["employment_type"],
                 row["experience_level"], row["location"], row["remote_type"], row["salary_min"],
                 row["salary_max"], row["salary_currency"], row["category"],
                 json.dumps(row["skills"]), row["posted_at"], row["expires_at"],
                 row["is_verified"], row.get("type", "JOB"), now, now))

    def query_jobs(self, q, category, skill, location, remote, employment, experience, page, limit,
                   jtype=None):
        sql = "SELECT * FROM jobs WHERE expires_at > now()"
        args = []
        if jtype:
            sql += " AND type = %s"; args.append(jtype)
        if q:
            sql += " AND (title ILIKE %s OR company ILIKE %s OR description ILIKE %s)"
            args += [f"%{q}%"] * 3
        if category:
            sql += " AND category = %s"; args.append(category)
        if skill:
            sql += " AND skills @> %s::jsonb"; args.append(json.dumps([skill]))
        if location:
            sql += " AND location ILIKE %s"; args.append(f"%{location}%")
        if remote:
            sql += " AND remote_type = %s"; args.append(remote)
        if employment:
            sql += " AND employment_type = %s"; args.append(employment)
        if experience:
            sql += " AND experience_level = %s"; args.append(experience)
        total = self._q("SELECT count(*) AS n FROM (" + sql + ") t", args)[0]["n"]
        sql += " ORDER BY is_verified DESC, posted_at DESC LIMIT %s OFFSET %s"
        args += [limit, (page - 1) * limit]
        rows = self._q(sql, args)
        return rows, total

    # ---------------- users -------------------------------------------
    def upsert_user_identity(self, provider, subject, email, name, avatar):
        rows = self._q("SELECT u.* FROM identities i JOIN users u ON u.id = i.user_id "
                       "WHERE i.provider = %s AND i.provider_subject = %s", (provider, subject))
        if rows:
            uid = rows[0]["id"]
            self._q("UPDATE users SET email = COALESCE(%s, email), "
                    "display_name = COALESCE(NULLIF(%s, ''), display_name), "
                    "avatar_url = COALESCE(%s, avatar_url), updated_at = now() WHERE id = %s",
                    (email or None, name or None, avatar or None, uid))
            return uid, self._q("SELECT * FROM users WHERE id = %s", (uid,))[0], False
        uid = str(uuid.uuid4())
        self._q("INSERT INTO users (id, email, display_name, avatar_url) VALUES (%s,%s,%s,%s)",
                (uid, (email or "").lower() or f"u{uid[:8]}@placeholder.local", name or "", avatar))
        self._q("INSERT INTO identities (id, user_id, provider, provider_subject, provider_email) "
                "VALUES (%s,%s,%s,%s,%s)",
                (str(uuid.uuid4()), uid, provider, subject, email))
        return uid, self._q("SELECT * FROM users WHERE id = %s", (uid,))[0], True

    def get_user(self, uid):
        try:
            rows = self._q("SELECT * FROM users WHERE id = %s", (uid,))
        except Exception:
            return None
        return rows[0] if rows else None

    def update_user(self, uid, patch):
        if not self.get_user(uid):
            return None
        sets, args = [], []
        for k in ("display_name", "headline", "location", "bio", "profile_visibility"):
            if k in patch:
                sets.append(f"{k} = %s"); args.append(str(patch[k])[:2000])
        if sets:
            sets.append("updated_at = now()")
            args.append(uid)
            self._q("UPDATE users SET " + ", ".join(sets) + " WHERE id = %s", args)
        return self.get_user(uid)

    def delete_user(self, uid):
        self._q("DELETE FROM audit_events WHERE user_id = %s", (uid,))
        self._q("DELETE FROM users WHERE id = %s", (uid,))  # FKs cascade

    # ---------------- job lookups --------------------------------------
    def get_job(self, jid):
        try:
            rows = self._q("SELECT * FROM jobs WHERE id = %s", (jid,))
        except Exception:
            return None
        return rows[0] if rows else None

    def similar_jobs(self, jid, limit=6):
        j = self.get_job(jid)
        if not j:
            return []
        pool = self._q("SELECT * FROM jobs WHERE id <> %s AND expires_at > now()", (jid,))
        pool.sort(key=lambda x: (x["category"] != j["category"],
                                 -len(set(x["skills"] or []) & set(j["skills"] or []))))
        return pool[:limit]

    # ---------------- saved ---------------------------------------------
    def save_job(self, uid, jid, note=""):
        self._q("INSERT INTO saved_jobs (user_id, job_id, note) VALUES (%s,%s,%s) "
                "ON CONFLICT (user_id, job_id) DO UPDATE SET note = EXCLUDED.note",
                (uid, jid, note or ""))
        return True

    def unsave_job(self, uid, jid):
        self._q("DELETE FROM saved_jobs WHERE user_id = %s AND job_id = %s", (uid, jid))

    def saved_jobs(self, uid):
        return self._q("SELECT job_id, note, created_at FROM saved_jobs WHERE user_id = %s",
                       (uid,))

    def is_saved(self, uid, jid):
        return bool(self._q("SELECT 1 AS x FROM saved_jobs WHERE user_id = %s AND job_id = %s",
                            (uid, jid)))

    # ---------------- applications ---------------------------------------
    def create_application(self, uid, jid):
        rows = self._q("SELECT * FROM applications WHERE user_id = %s AND job_id = %s", (uid, jid))
        if rows:
            return rows[0], False
        aid = str(uuid.uuid4())
        self._q("INSERT INTO applications (id, user_id, job_id, status, status_history) "
                "VALUES (%s,%s,%s,'SAVED',%s::jsonb)",
                (aid, uid, jid, json.dumps([{"status": "SAVED", "at": time.time()}])))
        return self.get_application(aid), True

    def get_application(self, aid):
        try:
            rows = self._q("SELECT * FROM applications WHERE id = %s", (aid,))
        except Exception:
            return None
        return rows[0] if rows else None

    def applications(self, uid, status=None, page=1, limit=20):
        sql = "SELECT * FROM applications WHERE user_id = %s"
        args = [uid]
        if status:
            sql += " AND status = %s"; args.append(status)
        total = self._q("SELECT count(*) AS n FROM (" + sql + ") t", args)[0]["n"]
        sql += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        args += [limit, (page - 1) * limit]
        return self._q(sql, args), total

    def update_application(self, aid, patch):
        a = self.get_application(aid)
        if not a:
            return None, "NOT_FOUND"
        if "status" in patch and patch["status"] != a["status"]:
            if patch["status"] not in TRANSITIONS.get(a["status"], set()):
                return None, "INVALID_TRANSITION"
            hist = (a["status_history"] or []) + [{"status": patch["status"], "at": time.time()}]
            self._q("UPDATE applications SET status = %s, status_history = %s::jsonb, "
                    "applied_at = CASE WHEN %s = 'APPLIED' THEN now() ELSE applied_at END, "
                    "updated_at = now() WHERE id = %s",
                    (patch["status"], json.dumps(hist), patch["status"], aid))
        sets, args = [], []
        for k in ("next_action", "private_notes"):
            if k in patch:
                sets.append(f"{k} = %s"); args.append(str(patch[k])[:4000])
        if "next_action_at" in patch:
            if patch["next_action_at"]:
                sets.append("next_action_at = to_timestamp(%s)")
                args.append(float(patch["next_action_at"]))
            else:
                sets.append("next_action_at = NULL")
        if sets:
            sets.append("updated_at = now()")
            args.append(aid)
            self._q("UPDATE applications SET " + ", ".join(sets) + " WHERE id = %s", args)
        return self.get_application(aid), None

    def delete_application(self, aid):
        self._q("DELETE FROM applications WHERE id = %s", (aid,))

    # ---------------- files ------------------------------------------------
    def create_file(self, uid, application_id, object_key, file_name, content_type, size):
        fid = str(uuid.uuid4())
        self._q("INSERT INTO files (id, user_id, application_id, object_key, file_name, "
                "content_type, size_bytes, processing_status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'QUEUED')",
                (fid, uid, application_id, object_key, file_name, content_type, size))
        return self.get_file(uid, fid)

    def files(self, uid):
        return self._q("SELECT * FROM files WHERE user_id = %s ORDER BY created_at DESC", (uid,))

    def get_file(self, uid, fid):
        try:
            rows = self._q("SELECT * FROM files WHERE id = %s AND user_id = %s", (fid, uid))
        except Exception:
            return None
        return rows[0] if rows else None

    def update_file_status(self, uid, fid, status):
        if not self.get_file(uid, fid):
            return None
        self._q("UPDATE files SET processing_status = %s, updated_at = now() WHERE id = %s",
                (status, fid))
        return self.get_file(uid, fid)

    # ---------------- notifications ----------------------------------------
    def notify(self, uid, ntype, title, body):
        dup = self._q("SELECT 1 AS x FROM notifications WHERE user_id = %s AND type = %s "
                      "AND title = %s AND created_at > now() - interval '24 hours'",
                      (uid, ntype, title))
        if dup:
            return None  # deduplicated within 24h
        nid = str(uuid.uuid4())
        self._q("INSERT INTO notifications (id, user_id, type, title, body) "
                "VALUES (%s,%s,%s,%s,%s)", (nid, uid, ntype, title, body))
        return self._q("SELECT * FROM notifications WHERE id = %s", (nid,))[0]

    def notifications(self, uid):
        return self._q("SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC",
                       (uid,))

    def read_notification(self, uid, nid):
        try:
            rows = self._q("UPDATE notifications SET read_at = now() "
                           "WHERE id = %s AND user_id = %s RETURNING *", (nid, uid))
        except Exception:
            return None
        return rows[0] if rows else None

    # ---------------- audit ---------------------------------------------------
    def audit_event(self, uid, event_type, request_id, metadata=None):
        try:
            self._q("INSERT INTO audit_events (id, user_id, event_type, request_id, metadata) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb)",
                    (str(uuid.uuid4()), uid, event_type, request_id, json.dumps(metadata or {})))
        except Exception:
            pass

    # ---------------- stats ------------------------------------------------------
    def overview(self):
        live = "expires_at > now()"
        n = self._q(f"SELECT count(*) AS n FROM jobs WHERE {live}")[0]["n"]
        cats = [r["category"] for r in self._q(
            f"SELECT DISTINCT category FROM jobs WHERE {live} ORDER BY category")]
        v = self._q(f"SELECT count(*) AS n FROM jobs WHERE {live} AND is_verified")[0]["n"]
        c = self._q(f"SELECT count(*) AS n FROM jobs WHERE {live} "
                    "AND expires_at < now() + interval '7 days'")[0]["n"]
        return {"jobs_live": n, "categories": len(cats), "category_list": cats,
                "verified": v, "closing_soon": c}


    # ---------------- career intelligence (SQL) ------------------------------
    def application_for(self, uid, jid):
        rows = self._q("SELECT * FROM applications WHERE user_id = %s AND job_id = %s "
                       "ORDER BY updated_at DESC LIMIT 1", (uid, jid))
        return rows[0] if rows else None

    def track(self, event_type, uid=None, job_id=None):
        try:
            self._q("INSERT INTO analytics_events (id, user_id, event_type, job_id)"
                    " VALUES (%s, %s, %s, %s)",
                    (uuid.uuid4().hex, uid, event_type, job_id))
        except Exception:
            pass

    def analytics_summary(self, days=30):
        by_type = self._q(
            "SELECT event_type, count(*) AS n FROM analytics_events"
            " WHERE created_at > now() - make_interval(days => %s)"
            " GROUP BY event_type ORDER BY n DESC", (days,))
        top_jobs = self._q(
            "SELECT job_id, count(*) AS views FROM analytics_events"
            " WHERE event_type = 'job_view' AND job_id IS NOT NULL"
            "   AND created_at > now() - make_interval(days => %s)"
            " GROUP BY job_id ORDER BY views DESC LIMIT 10", (days,))
        quality = {
            "jobsLive": self._q("SELECT count(*) AS n FROM jobs WHERE expires_at > now()")[0]["n"],
            "jobsVerified": self._q("SELECT count(*) AS n FROM jobs WHERE is_verified")[0]["n"],
            "jobsEmployerPosted": self._q(
                "SELECT count(*) AS n FROM jobs WHERE posted_by_employer IS NOT NULL")[0]["n"],
            "openReports": self._q("SELECT count(*) AS n FROM job_reports")[0]["n"],
            "applications": self._q("SELECT count(*) AS n FROM applications")[0]["n"],
            "users": self._q("SELECT count(*) AS n FROM users")[0]["n"],
            "resumes": self._q("SELECT count(*) AS n FROM resumes")[0]["n"],
        }
        return {"totalEvents": sum(r["n"] for r in by_type),
                "byType": {r["event_type"]: r["n"] for r in by_type},
                "topViewedJobs": [{"jobId": r["job_id"], "views": r["views"]}
                                  for r in top_jobs],
                "dataQuality": quality, "days": days}

    def save_resume(self, uid, file_name, content_type, text, consent):
        rid = uuid.uuid4().hex
        parsed = _career.parse_resume(text)
        self._q("INSERT INTO resumes (id, user_id, file_name, content_type, text_content,"
                " parsed, consent) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)",
                (rid, uid, file_name, content_type, text, json.dumps(parsed), bool(consent)))
        self.grant_consent(uid, "resume_processing", True, "resume uploaded")
        return self.latest_resume(uid)

    def latest_resume(self, uid):
        rows = self._q("SELECT * FROM resumes WHERE user_id = %s "
                       "ORDER BY created_at DESC LIMIT 1", (uid,))
        return rows[0] if rows else None

    def delete_resume(self, uid):
        self._q("DELETE FROM resumes WHERE user_id = %s", (uid,))
        self.grant_consent(uid, "resume_processing", False, "resume deleted")

    def update_resume_parsed(self, rid, parsed_patch):
        rows = self._q("SELECT parsed FROM resumes WHERE id = %s", (rid,))
        if not rows:
            return None
        p = dict(rows[0]["parsed"])
        for k in ("skills", "projects"):
            if isinstance(parsed_patch.get(k), list):
                p[k] = parsed_patch[k]
        for k in ("education", "graduation_year", "experience_years"):
            if k in parsed_patch:
                p[k] = parsed_patch[k]
        self._q("UPDATE resumes SET parsed = %s::jsonb, updated_at = now() WHERE id = %s",
                (json.dumps(p), rid))
        return self.latest_resume_by_id(rid)

    def latest_resume_by_id(self, rid):
        rows = self._q("SELECT * FROM resumes WHERE id = %s", (rid,))
        return rows[0] if rows else None

    def grant_consent(self, uid, kind, granted, note=""):
        self._q("INSERT INTO consents (id, user_id, kind, granted, note) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, uid, kind, bool(granted), note or None))

    def consents_of(self, uid):
        return self._q("SELECT * FROM consents WHERE user_id = %s "
                       "ORDER BY created_at DESC LIMIT 50", (uid,))

    def log_ingestion(self, source, status, stats=None, error=None):
        rid = uuid.uuid4().hex
        self._q("INSERT INTO ingestion_runs (id, source, status, stats, error, finished_at)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, now())",
                (rid, source, status, json.dumps(stats or {}), error))
        return {"id": rid, "source": source, "status": status, "stats": stats or {},
                "error": error, "started_at": time.time(), "finished_at": time.time()}

    def ingestion_runs(self, limit=20):
        return self._q("SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT %s",
                       (limit,))

    def add_verification(self, job_id, status, note, actor):
        self._q("INSERT INTO verification_events (id, job_id, status, note, actor)"
                " VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, job_id, status, note, actor))
        return {"job_id": job_id, "status": status, "note": note, "actor": actor}

    def verifications_of(self, job_id):
        return self._q("SELECT * FROM verification_events WHERE job_id = %s "
                       "ORDER BY created_at DESC LIMIT 20", (job_id,))

    def report_job(self, uid, job_id, reason, details):
        rep_id = uuid.uuid4().hex
        self._q("INSERT INTO job_reports (id, user_id, job_id, reason, details)"
                " VALUES (%s, %s, %s, %s, %s)", (rep_id, uid, job_id, reason, details or None))
        return {"id": rep_id, "user_id": uid, "job_id": job_id, "reason": reason,
                "details": details or None, "status": "open"}

    def register_employer(self, uid, company_name, website, about, logo_url):
        rows = self._q("SELECT * FROM employers WHERE user_id = %s", (uid,))
        if rows:
            return rows[0], False
        eid = uuid.uuid4().hex
        self._q("INSERT INTO employers (id, user_id, company_name, website, about, logo_url)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (eid, uid, company_name, website, about, logo_url))
        rows = self._q("SELECT * FROM employers WHERE id = %s", (eid,))
        return rows[0], True

    def employer_by_user(self, uid):
        rows = self._q("SELECT * FROM employers WHERE user_id = %s", (uid,))
        return rows[0] if rows else None

    def employer_jobs(self, eid):
        return self._q("SELECT * FROM jobs WHERE posted_by_employer = %s "
                       "ORDER BY created_at DESC", (eid,))

    def employer_applications(self, eid, page=1, limit=50):
        base = ("SELECT a.* FROM applications a JOIN jobs j ON j.id = a.job_id "
                "WHERE j.posted_by_employer = %s")
        total = self._q("SELECT count(*) AS n FROM (" + base + ") t", (eid,))[0]["n"]
        rows = self._q(base + " ORDER BY a.updated_at DESC LIMIT %s OFFSET %s",
                       (eid, limit, (page - 1) * limit))
        return rows, total

    def post_job(self, eid, raw):
        j = _career.normalize_job(raw, "employer-direct")
        jid = uuid.uuid4().hex
        now = time.time()
        jtype = raw.get("type") or CATEGORY_TO_TYPE.get(j["category"], "JOB")
        self._q(
            "INSERT INTO jobs (id, external_id, source, title, company, description, apply_url,"
            " employment_type, experience_level, location, remote_type, salary_min, salary_max,"
            " salary_currency, category, type, skills, skills_required, skills_preferred,"
            " posted_at, expires_at, is_verified, posted_by_employer, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,"
            "%s::jsonb, now(), to_timestamp(%s), false, %s, now(), now())",
            (jid, j["external_id"], "employer-direct", j["title"], j["company"],
             j["description"], j["apply_url"], j["employment_type"], j["experience_level"],
             j["location"], j["remote_type"], j["salary_min"], j["salary_max"],
             j["salary_currency"], j["category"], jtype, json.dumps(j["skills"]),
             json.dumps(j["skills_required"]), json.dumps(j["skills_preferred"]),
             j["expires_at"] or now + 60 * 86400, eid))
        rows = self._q("SELECT * FROM jobs WHERE id = %s", (jid,))
        return rows[0]

    def patch_employer(self, eid, key, value):
        if key not in ("about", "website", "logo_url"):
            return None
        self._q(f"UPDATE employers SET {key} = %s, updated_at = now() WHERE id = %s",
                (value, eid))
        rows = self._q("SELECT * FROM employers WHERE id = %s", (eid,))
        return rows[0] if rows else None

    def verify_employer(self, eid, status):
        self._q("UPDATE employers SET verification_status = %s, "
                "verified_at = CASE WHEN %s = 'verified' THEN now() ELSE NULL END, "
                "updated_at = now() WHERE id = %s", (status, status, eid))
        rows = self._q("SELECT * FROM employers WHERE id = %s", (eid,))
        return rows[0] if rows else None

    def set_job_verification(self, jid, status, actor, note=""):
        self._q("UPDATE jobs SET is_verified = %s, last_verified_at = now(), "
                "updated_at = now() WHERE id = %s", (status == "verified", jid))
        self.add_verification(jid, status, note, actor)
        rows = self._q("SELECT * FROM jobs WHERE id = %s", (jid,))
        return rows[0] if rows else None

    def app_parties(self, aid):
        a = self.get_application(aid)
        if not a:
            return None, None, None
        j = self.get_job(a["job_id"])
        emp = None
        if j and j.get("posted_by_employer"):
            rows = self._q("SELECT * FROM employers WHERE id = %s", (j["posted_by_employer"],))
            emp = rows[0] if rows else None
        return a, j, emp

    def send_message(self, aid, sender_id, body):
        a, j, employer = self.app_parties(aid)
        if not a:
            return None, "NOT_FOUND"
        recipient = None
        if sender_id == a["user_id"] and employer:
            recipient = employer["user_id"]
        elif employer and sender_id == employer["user_id"]:
            recipient = a["user_id"]
        else:
            return None, "FORBIDDEN"
        mid = uuid.uuid4().hex
        self._q("INSERT INTO messages (id, application_id, sender_id, recipient_id, body)"
                " VALUES (%s, %s, %s, %s, %s)", (mid, aid, sender_id, recipient, body))
        rows = self._q("SELECT * FROM messages WHERE id = %s", (mid,))
        return (rows[0] if rows else None), None

    def messages_of(self, aid, uid):
        a, j, employer = self.app_parties(aid)
        if not a:
            return None, "NOT_FOUND"
        if uid != a["user_id"] and not (employer and uid == employer["user_id"]):
            return None, "FORBIDDEN"
        out = self._q("SELECT * FROM messages WHERE application_id = %s "
                      "ORDER BY created_at ASC", (aid,))
        for m in out:
            if m["recipient_id"] == uid and not m["read_at"]:
                self._q("UPDATE messages SET read_at = now() WHERE id = %s", (m["id"],))
                m["read_at"] = time.time()
        return out, None

    def set_progress(self, uid, role, skill, status):
        if status not in ("todo", "in_progress", "done"):
            return None
        self._q("INSERT INTO learning_progress (user_id, role, skill, status, updated_at)"
                " VALUES (%s, %s, %s, %s, now())"
                " ON CONFLICT (user_id, role, skill) DO UPDATE "
                "SET status = EXCLUDED.status, updated_at = now()",
                (uid, role, skill, status))
        return {"user_id": uid, "role": role, "skill": skill, "status": status}

    def progress_of(self, uid, role):
        rows = self._q("SELECT skill, status FROM learning_progress "
                       "WHERE user_id = %s AND role = %s", (uid, role))
        return {r["skill"]: r["status"] for r in rows}


_store = MemStore()
if STORAGE_MODE == "postgres":
    try:
        _store = PgStore()
    except Exception as e:  # pragma: no cover
        print("PgStore init failed, using ephemeral:", e)
        _store = MemStore()


# ------------------------------------------------------------- OAuth flow
def _provider_configured(provider: str) -> bool:
    if provider == "google":
        return bool(GOOGLE_CID and GOOGLE_SEC)
    return bool(MS_CID and MS_SEC)


def _oauth_redirect(provider: str, state: str, redirect_uri: str = "") -> str:
    if provider == "google":
        return ("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": GOOGLE_CID, "redirect_uri": redirect_uri or GOOGLE_REDIRECT,
            "response_type": "code", "scope": "openid email profile",
            "state": state, "nonce": state[:24]}))
    return (f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize?" + urlencode({
        "client_id": MS_CID, "redirect_uri": MS_REDIRECT,
        "response_type": "code", "scope": "openid email profile",
        "state": state, "nonce": state[:24]}))


def _exchange_code(provider: str, code: str, redirect_uri: str = ""):
    """Server-side token exchange + identity verification. Access tokens are
    used once and never stored."""
    import urllib.request
    if provider == "google":
        token_url = "https://oauth2.googleapis.com/token"
        data = {"code": code, "client_id": GOOGLE_CID, "client_secret": GOOGLE_SEC,
                "redirect_uri": redirect_uri or GOOGLE_REDIRECT, "grant_type": "authorization_code"}
        userinfo_url = "https://openidconnect.googleapis.com/v1/userinfo"
    else:
        token_url = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"
        data = {"code": code, "client_id": MS_CID, "client_secret": MS_SEC,
                "redirect_uri": MS_REDIRECT, "grant_type": "authorization_code",
                "scope": "openid email profile"}
        userinfo_url = "https://graph.microsoft.com/oidc/userinfo"
    req = urllib.request.Request(
        token_url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        tok = json.loads(r.read())
    req2 = urllib.request.Request(userinfo_url, headers={
        "Authorization": f"Bearer {tok['access_token']}"})
    with urllib.request.urlopen(req2, timeout=10) as r:
        info = json.loads(r.read())
    return {"sub": info["sub"], "email": info.get("email") or info.get("upn", ""),
            "name": info.get("name", ""), "picture": info.get("picture")}


# ================================================================= handler
def envelope(data=None, meta=None, error=None):
    return {"data": data, "meta": meta or {"requestId": "req_" + uuid.uuid4().hex[:16]},
            "error": error}


def job_public(j, saved=False):
    return {"id": j["id"], "title": j["title"], "company": j["company"],
            "category": j["category"], "type": j.get("type", "JOB"), "skills": j["skills"],
            "location": j["location"], "remoteType": j["remote_type"],
            "employmentType": j["employment_type"],
            "experienceLevel": j["experience_level"],
            "salaryMin": j["salary_min"], "salaryMax": j["salary_max"],
            "salaryCurrency": j["salary_currency"], "isVerified": j["is_verified"],
            "postedAt": j["posted_at"], "expiresAt": j["expires_at"],
            "description": j["description"], "applyUrl": j.get("apply_url"),
            "source": j["source"], "saved": saved,
            "skillsRequired": j.get("skills_required") or [],
            "skillsPreferred": j.get("skills_preferred") or [],
            "lastVerifiedAt": j.get("last_verified_at"),
            "sponsored": bool(j.get("sponsored", False)),
            "live": bool(j.get("live"))}


class MFHandler(handler):
    # -- helpers ------------------------------------------------------
    def _send(self, status, body, headers=None, is_json=True):
        raw = json.dumps(body).encode() if is_json else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if is_json else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-Id", "req_" + uuid.uuid4().hex[:16])
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, to, headers=None):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _cookie(self):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return None

    def _uid(self):
        tok = self._cookie()
        return read_session(tok) if tok else None

    def _require_auth(self):
        uid = self._uid()
        if not uid:
            self._send(401, {"data": None, "meta": {"requestId": "req_" + uuid.uuid4().hex[:16]},
                             "error": {"code": "AUTH_REQUIRED", "message": "Sign in to continue"}})
            return None
        return uid

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    def _page(self, q):
        try:
            page = max(1, int(q.get("page", ["1"])[0]))
            limit = min(PAGE_MAX, max(1, int(q.get("limit", [str(PAGE_DEFAULT)])[0])))
        except Exception:
            page, limit = 1, PAGE_DEFAULT
        return page, limit

    def _meta(self, page, limit, total, **extra):
        m = {"requestId": "req_" + uuid.uuid4().hex[:16], "page": page, "limit": limit, "total": total}
        m.update(extra)
        return m

    # -- HTTP ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if urlparse(self.path).path.startswith("/api/mf/"):
            return self._mf_route("GET")
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path.startswith("/api/mf/"):
            return self._mf_route("POST")
        return super().do_POST()

    def do_PATCH(self):
        if urlparse(self.path).path.startswith("/api/mf/"):
            return self._mf_route("PATCH")
        return self._send(405, envelope(None, error={
            "code": "METHOD_NOT_ALLOWED", "message": "PATCH only under /api/mf/"}))

    def do_DELETE(self):
        if urlparse(self.path).path.startswith("/api/mf/"):
            return self._mf_route("DELETE")
        return self._send(405, envelope(None, error={
            "code": "METHOD_NOT_ALLOWED", "message": "DELETE only under /api/mf/"}))

    def _mf_route(self, method):
        ip = self.headers.get("x-forwarded-for", "local")
        if not rate_ok(f"{ip}:{method}", 120):
            return self._send(429, {"data": None,
                                   "meta": {"requestId": "req_" + uuid.uuid4().hex[:16]},
                                   "error": {"code": "RATE_LIMITED", "message": "Slow down"}})
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        # single-file routing: this function also serves the media API, so
        # marketplace routes are identified by the real /api/mf/ path prefix
        route = parsed.path
        if not route.startswith("/api/mf/"):
            route = q.get("__route", [""])[0] or route
        parts = [unquote(p) for p in route.split("/") if p]
        # parts[0]="api", parts[1]="mf", rest = resource path
        try:
            return self._mf_dispatch(method, q, parts[2:])
        except Exception as e:
            self._send(500, {"data": None, "meta": {"requestId": "req_" + uuid.uuid4().hex[:16]},
                             "error": {"code": "INTERNAL", "message": "Server error", "detail": str(e)[:200]}})

    # -- dispatch ------------------------------------------------------
    def _mf_dispatch(self, method, q, r):
        n = len(r)
        head = r[0] if r else ""

        if head == "health":
            return self._send(200, envelope({
                "status": "ok", "persistence": "postgres" if not _store.ephemeral else "ephemeral",
                "auth": {"google": _provider_configured("google"),
                         "microsoft": _provider_configured("microsoft"),
                         "mode": "live" if (_provider_configured("google")
                                            or _provider_configured("microsoft")) else "demo"},
                "time": time.time()}))

        if head == "overview":
            return self._send(200, envelope(_store.overview()))

        # ---------------- auth ----------------
        if head == "auth":
            sub = r[1] if n > 1 else ""
            if sub == "providers" and method == "GET":
                return self._send(200, envelope({
                    "google": _provider_configured("google"),
                    "microsoft": _provider_configured("microsoft"),
                    "mode": "live" if (_provider_configured("google")
                                       or _provider_configured("microsoft")) else "demo"}))
            if sub == "login" and n == 3 and method == "GET":
                provider = r[2]
                if not _provider_configured(provider):
                    return self._send(501, envelope(None, error={
                        "code": "OAUTH_NOT_CONFIGURED",
                        "message": f"{provider} OAuth credentials are not configured. "
                                   "Use POST /api/mf/auth/demo for the dev sign-in."}))
                if not rate_ok(f"login:anon", 10):
                    return self._send(429, {"data": None, "error": {"code": "RATE_LIMITED",
                                                                    "message": "Too many login attempts"}})
                state = oauth_state(q.get("redirect", ["/"])[0])
                return self._redirect(_oauth_redirect(provider, state, self._abs_redirect()))
            if sub == "callback" and n == 3 and method == "GET":
                provider = r[2]
                st = verify_state(q.get("state", [""])[0])
                if not st:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_STATE", "message": "Invalid or expired OAuth state"}))
                code = q.get("code", [""])[0]
                if not code:
                    return self._send(400, envelope(None, error={
                        "code": "NO_CODE", "message": "Authorization code missing"}))
                try:
                    ident = _exchange_code(provider, code, self._abs_redirect())
                except Exception:
                    return self._send(502, envelope(None, error={
                        "code": "TOKEN_EXCHANGE_FAILED",
                        "message": "Identity provider rejected the code"}))
                uid, user, created = _store.upsert_user_identity(
                    provider, ident["sub"], ident["email"], ident["name"], ident["picture"])
                _store.audit_event(uid, "auth.login" if not created else "auth.signup", None,
                                   {"provider": provider})
                if created:
                    _store.notify(uid, "account", "Welcome to MediaFlow Jobs",
                                  "Your account is ready. Explore live software opportunities.")
                token = make_session(uid)  # session rotation on login
                dest = st.get("r") or "/"
                if not dest.startswith("/") or dest.startswith("//"):
                    dest = "/"          # same-site only, no open redirects
                return self._redirect(dest, self._session_cookie(token))
            if sub == "demo" and method == "POST":
                if _provider_configured("google") or _provider_configured("microsoft"):
                    return self._send(400, envelope(None, error={
                        "code": "DEMO_DISABLED", "message": "Live OAuth is configured"}))
                if not rate_ok(f"demo:anon", 15):
                    return self._send(429, {"data": None, "error": {"code": "RATE_LIMITED",
                                                                    "message": "Slow down"}})
                b = self._body()
                email = str(b.get("email", "")).strip().lower()
                if not EMAIL_RE.match(email):
                    return self._send(400, envelope(None, error={
                        "code": "INVALID_EMAIL", "message": "A valid email is required"}))
                uid, user, _ = _store.upsert_user_identity(
                    "demo", email, email, str(b.get("name") or email.split("@")[0])[:80], None)
                _store.audit_event(uid, "auth.login", None, {"provider": "demo"})
                return self._send(200, envelope({"user": user, "demo": True}),
                                  headers=self._session_cookie(make_session(uid)))
            if sub == "logout" and method == "POST":
                uid = self._uid()
                if uid:
                    _store.audit_event(uid, "auth.logout", None, {})
                return self._send(200, envelope({"ok": True}),
                                  headers={**self._session_cookie(""), "Clear-Site-Data": '"cache"'})
            return self._send(404, envelope(None, error={"code": "NOT_FOUND",
                                                         "message": "Unknown auth route"}))

        # ---------------- me ----------------
        if head == "me":
            uid = self._require_auth()
            if not uid:
                return
            if n == 1 and method == "GET":
                u = _store.get_user(uid)
                if not u:
                    return self._send(401, envelope(None, error={
                        "code": "AUTH_REQUIRED", "message": "Sign in to continue"}),
                                      headers=self._session_cookie(""))
                return self._send(200, envelope({
                    "user": u, "savedCount": len(_store.saved_jobs(uid)),
                    "applicationCount": _store.applications(uid, page=1, limit=1)[1],
                    "notificationCount": len(_store.notifications(uid))}))
            if n == 1 and method == "PATCH":
                b = self._body()
                if not isinstance(b, dict):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_BODY", "message": "JSON object expected"}))
                u = _store.update_user(uid, b)
                _store.audit_event(uid, "me.update", None, {k: 1 for k in b})
                return self._send(200, envelope({"user": u}))
            if n == 1 and method == "DELETE":
                _store.audit_event(uid, "me.delete", None, {})
                _store.delete_user(uid)
                return self._send(200, envelope({"deleted": True}),
                                  headers=self._session_cookie(""))
            if n == 2 and r[1] == "applications" and method == "GET":
                page, limit = self._page(q)
                status = q.get("status", [None])[0]
                if status and status not in ALLOWED_STATUSES:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_STATUS", "message": "Unknown status filter"}))
                items, total = _store.applications(uid, status, page, limit)
                jobs = {j["id"]: job_public(j) for j in
                        (_store.get_job(a["job_id"]) for a in items) if j}
                return self._send(200, envelope({
                    "applications": [{**a, "job": jobs.get(a["job_id"])} for a in items]},
                    self._meta(page, limit, total)))
            if n == 2 and r[1] == "files":
                if method == "GET":
                    return self._send(200, envelope({"files": _store.files(uid)}))
                if method == "POST":
                    return self._send(405, envelope(None, error={
                        "code": "WRONG_METHOD", "message": "Use POST /api/mf/me/files/presign"}))
            if n == 2 and r[1] == "notifications" and method == "GET":
                items = _store.notifications(uid)
                return self._send(200, envelope(
                    {"notifications": items, "unread": sum(1 for x in items if not x["read_at"])}))
            if n >= 3 and r[1] == "notifications" and r[2] and method == "POST":
                done = _store.read_notification(uid, r[2])
                return self._send(200 if done else 404, envelope(
                    {"ok": bool(done)} if done else None,
                    None if done else None,
                    None if done else {"code": "NOT_FOUND", "message": "Notification not found"}))
            if n == 3 and r[1] == "files" and r[2] == "presign" and method == "POST":
                b = self._body()
                name = str(b.get("fileName", "")).strip()[:255]
                ctype = str(b.get("contentType", "application/pdf")).strip()
                size = int(b.get("sizeBytes") or 0)
                if not name or "/" in name or ".." in name:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_FILE_NAME", "message": "fileName required"}))
                if ctype not in FILE_TYPES:
                    return self._send(415, envelope(None, error={
                        "code": "BAD_CONTENT_TYPE", "message": "Unsupported content type"}))
                if size <= 0 or size > MAX_FILE_BYTES:
                    return self._send(413, envelope(None, error={
                        "code": "BAD_SIZE", "message": f"File must be 1 byte..{MAX_FILE_BYTES}"}))
                object_key = f"private/{uid}/{uuid.uuid4().hex}/{name}"
                f = _store.create_file(uid, b.get("applicationId") or None, object_key,
                                       name, ctype, size)
                _store.audit_event(uid, "file.presign", None, {"contentType": ctype, "size": size})
                # The upload itself goes through the EXISTING MediaFlow media pipeline
                # (POST /api/jobs -> jobId; processing runs async, the S3/SQS worker
                # contract is preserved). The client links the media job by PATCHing
                # /api/mf/me/files/{fileId} with mediaJobId, then polls the media API.
                return self._send(200, envelope({
                    "file": f,
                    "upload": {"endpoint": "/api/jobs", "method": "POST",
                               "fields": {"fileName": name, "contentType": ctype,
                                          "sourceApp": "mediaflow-jobs"}}}))
            if n == 3 and r[1] == "files" and method == "PATCH":
                b = self._body()
                f = _store.get_file(uid, r[2])
                if not f:
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "File not found"}))
                st = str(b.get("processingStatus", ""))
                if st not in ("QUEUED", "PROCESSING", "COMPLETED", "FAILED"):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_STATUS", "message": "Unknown processing status"}))
                f = _store.update_file_status(uid, r[2], st)
                _store.audit_event(uid, "file.status", None, {"fileId": r[2], "status": st})
                return self._send(200, envelope({"file": f}))
            if n == 3 and r[1] == "files" and method == "GET":
                f = _store.get_file(uid, r[2])
                return self._send(200 if f else 404,
                                  envelope({"file": f} if f else None,
                                           None, None if f else
                                           {"code": "NOT_FOUND", "message": "File not found"}))
            if n == 3 and r[1] == "files" and method == "DELETE":
                return self._send(200, envelope({"deleted": True}))
            return self._send(404, envelope(None, error={"code": "NOT_FOUND",
                                                         "message": "Unknown /me route"}))

        # ---------------- live discover ----------------
        if head == "discover" and n == 1 and method == "GET":
            page, limit = self._page(q)
            g = lambda k: q.get(k, [None])[0]
            jtype = g("type")
            qq = g("q")
            try:
                live, sources = _discover_live()
            except Exception:
                live, sources = [], {}
            try:
                cat_items, _ = _store.query_jobs(qq, g("category"), g("skill"),
                                                 g("location"), g("remote"),
                                                 g("employmentType"), g("experience"),
                                                 1, 200, jtype=jtype)
            except Exception:
                cat_items = []
            merged = [job_public(j) for j in cat_items] + [job_public(j) for j in live]
            if jtype:
                merged = [m for m in merged if m.get("type") == jtype]
            if qq:
                ql = qq.lower()
                merged = [m for m in merged
                          if ql in m["title"].lower() or ql in m["company"].lower()
                          or any(ql in sk.lower() for sk in m.get("skills", []))]
            merged.sort(key=lambda m: (m.get("live") is True, -(m["postedAt"] or 0)))
            total = len(merged)
            start = (page - 1) * limit
            return self._send(200, envelope(
                {"jobs": merged[start:start + limit],
                 "liveSources": sources, "liveCount": len(live),
                 "types": JTYPES},
                self._meta(page, limit, total)))

        # ---------------- jobs ----------------
        if head == "jobs":
            if n == 1 and method == "GET":
                page, limit = self._page(q)
                g = lambda k: q.get(k, [None])[0]
                items, total = _store.query_jobs(g("q"), g("category"), g("skill"),
                                                 g("location"), g("remote"),
                                                 g("employmentType"), g("experience"),
                                                 page, limit, jtype=g("type"))
                uid = self._uid()
                saved = set(_store.saved_jobs(uid)) if uid else set()
                return self._send(200, envelope(
                    {"jobs": [job_public(j, j["id"] in saved) for j in items]},
                    self._meta(page, limit, total)))
            if n == 2 and method == "GET":
                j = _store.get_job(r[1])
                if not j:
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Job not found"}))
                uid = self._uid()
                _store.track("job_view", uid, r[1])
                return self._send(200, envelope({
                    "job": job_public(j, _store.is_saved(uid, r[1]) if uid else False),
                    "similar": [job_public(s) for s in _store.similar_jobs(r[1])],
                    "verificationHistory": _store.verifications_of(r[1]),
                    "cautions": _career.fraud_signals(j.get("description") or ""),
                    "sections": _career.split_description(j.get("description") or ""),
                    "stale": _career.is_stale(j, time.time()),
                    "application": (_store.application_for(uid, r[1]) if uid else None)}))
            if n == 3 and r[2] == "save":
                uid = self._require_auth()
                if not uid:
                    return
                if method == "POST":
                    j = _store.get_job(r[1])
                    if not j:
                        return self._send(404, envelope(None, error={
                            "code": "NOT_FOUND", "message": "Job not found"}))
                    note = str(self._body().get("note") or "")[:2000]
                    _store.save_job(uid, r[1], note)
                    _store.audit_event(uid, "job.save", None, {"jobId": r[1]})
                    _store.track("job_save", uid, r[1])
                    return self._send(200, envelope({"saved": True}))
                if method == "DELETE":
                    _store.unsave_job(uid, r[1])
                    _store.audit_event(uid, "job.unsave", None, {"jobId": r[1]})
                    return self._send(200, envelope({"saved": False}))
            if n == 3 and r[2] == "applications" and method == "POST":
                uid = self._require_auth()
                if not uid:
                    return
                j = _store.get_job(r[1])
                if not j:
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Job not found"}))
                a, created = _store.create_application(uid, r[1])
                _store.audit_event(uid, "application.apply" if created else "application.apply.dup",
                                  None, {"jobId": r[1]})
                _store.track("apply_start", uid, r[1])
                if created:
                    _store.notify(uid, "application", "Saved to your pipeline",
                                  f"{j['title']} @ {j['company']} is now in SAVED.")
                return self._send(200, envelope({"application": a, "created": created}))
            if n == 3 and r[2] == "report" and method == "POST":
                uid = self._require_auth()
                if not uid:
                    return
                b = self._body()
                reason = str((b or {}).get("reason") or "").strip().lower()
                if reason not in ("inaccurate", "expired", "fraud", "duplicate"):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_REASON",
                        "message": "reason must be inaccurate | expired | fraud | duplicate"}))
                details = str((b or {}).get("details") or "")[:2000]
                _store.report_job(uid, r[1], reason, details)
                _store.audit_event(uid, "job.report", None,
                                   {"jobId": r[1], "reason": reason})
                return self._send(200, envelope(
                    {"reported": True,
                     "note": "Thanks — a maintainer will review this listing. "
                             "Reports are human-reviewed; listings are never "
                             "auto-removed by reports alone."}))
            if n == 3 and r[2] == "verification" and method == "GET":
                return self._send(200, envelope(
                    {"history": _store.verifications_of(r[1])}))
            return self._send(404, envelope(None, error={"code": "NOT_FOUND",
                                                          "message": "Unknown jobs route"}))

        # ---------------- applications ----------------
        if head == "applications" and n == 2:
            uid = self._require_auth()
            if not uid:
                return
            aid = r[1]
            a = _store.get_application(aid)
            if not a or a["user_id"] != uid:   # row-level ownership
                return self._send(404, envelope(None, error={
                    "code": "NOT_FOUND", "message": "Application not found"}))
            if method == "PATCH":
                b = self._body()
                if not isinstance(b, dict):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_BODY", "message": "JSON object expected"}))
                a2, err = _store.update_application(aid, b)
                if err == "INVALID_TRANSITION":
                    return self._send(422, envelope(None, error={
                        "code": "INVALID_TRANSITION",
                        "message": f"Cannot move {a['status']} -> {b.get('status')}"}))
                _store.audit_event(uid, "application.update", None,
                                   {"applicationId": aid, "status": b.get("status")})
                j = _store.get_job(a["job_id"])
                if b.get("status") and j:
                    _store.notify(uid, "application",
                                  f"Moved to {b['status']}",
                                  f"{j['title']} @ {j['company']}")
                return self._send(200, envelope({"application": a2}))
            if method == "DELETE":
                _store.delete_application(aid)
                _store.audit_event(uid, "application.delete", None, {"applicationId": aid})
                return self._send(200, envelope({"deleted": True}))

        # ---------------- resume & consent -------------------------------
        if head == "resume" and n == 1:
            uid = self._require_auth()
            if not uid:
                return
            res = _store.latest_resume(uid)
            if method == "GET":
                return self._send(200, envelope({"resume": res}))
            if method == "POST":
                b = self._body()
                if not isinstance(b, dict) or not b.get("consent"):
                    return self._send(400, envelope(None, error={
                        "code": "CONSENT_REQUIRED",
                        "message": "Explicit consent is required to process a resume."}))
                text = str(b.get("text") or "").strip()
                if not (50 <= len(text) <= 100000):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_TEXT",
                        "message": "Paste your resume text (50–100,000 characters). "
                                   "PDF parsing tip: copy the text out of the PDF first."}))
                if not rate_ok(f"resume:{uid}", 10):
                    return self._send(429, envelope(None, error={
                        "code": "RATE_LIMITED", "message": "Too many resume uploads"}))
                res = _store.save_resume(uid, str(b.get("file_name") or "resume.txt")[:200],
                                         "text/plain", text, True)
                _store.audit_event(uid, "resume.upload", None, {"length": len(text)})
                _store.track("resume_analyze", uid, None)
                return self._send(200, envelope({"resume": res}))
            if method == "PATCH":
                b = self._body()
                if not isinstance(b, dict) or not res:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_REQUEST", "message": "Nothing to correct yet"}))
                res = _store.update_resume_parsed(res["id"], b.get("parsed") or {})
                _store.audit_event(uid, "resume.correct", None, {})
                return self._send(200, envelope({"resume": res}))
            if method == "DELETE":
                _store.delete_resume(uid)
                _store.audit_event(uid, "resume.delete", None, {})
                return self._send(200, envelope({"deleted": True}))

        # ---------------- explainable matching -----------------------------
        if head == "match" and n == 1 and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            jid = q.get("job_id", [None])[0]
            j = _store.get_job(jid) if jid else None
            if not j:
                return self._send(404, envelope(None, error={
                    "code": "NOT_FOUND", "message": "job_id is required and must exist"}))
            res = _store.latest_resume(uid)
            parsed = (res or {}).get("parsed") or {}
            return self._send(200, envelope({
                "match": _career.match_job(parsed, j),
                "job": job_public(j),
                "hasResume": bool(res)}))

        if head == "match" and n == 2 and r[1] == "recommended" and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            res = _store.latest_resume(uid)
            parsed = (res or {}).get("parsed") or {}
            jobs, _t = _store.query_jobs(None, None, None, None, None, None, None, 1, 100)
            scored = []
            for j in jobs:
                m = _career.match_job(parsed, j)
                scored.append((m["score"], job_public(j), m))
            scored.sort(key=lambda t: -t[0])
            return self._send(200, envelope({
                "recommended": [{"job": jp, "match": m} for _s, jp, m in scored[:10]],
                "hasResume": bool(res)}))

        # ---------------- career roadmap ------------------------------------
        if head == "roadmap" and n == 1 and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            role = (q.get("role", [""])[0] or "").strip()
            if not role:
                return self._send(200, envelope({"roles": sorted(_career.ROLE_LIBRARY)}))
            res = _store.latest_resume(uid)
            parsed = (res or {}).get("parsed") or {}
            rd = _career.roadmap_for(parsed, role)
            if "error" in rd:
                return self._send(400, envelope(None, error={
                    "code": "UNKNOWN_ROLE", "message": "Pick a role from the list",
                    "roles": rd["roles"]}))
            prog = _store.progress_of(uid, role)
            for item in rd["skills_gap"]:
                item["status"] = prog.get(item["skill"], "todo")
            return self._send(200, envelope({"roadmap": rd, "hasResume": bool(res)}))

        if head == "roadmap" and n == 2 and r[1] == "progress" and method == "POST":
            uid = self._require_auth()
            if not uid:
                return
            b = self._body() or {}
            skill = str(b.get("skill") or "").strip()[:80]
            role = str(b.get("role") or "").strip()[:80]
            status = str(b.get("status") or "").strip()
            if not skill or role not in _career.ROLE_LIBRARY:
                return self._send(400, envelope(None, error={
                    "code": "BAD_REQUEST", "message": "skill and a valid role are required"}))
            out = _store.set_progress(uid, role, skill, status)
            if not out:
                return self._send(400, envelope(None, error={
                    "code": "BAD_STATUS",
                    "message": "status must be todo | in_progress | done"}))
            return self._send(200, envelope({"progress": out}))

        # ---------------- ingestion transparency ---------------------------
        if head == "ingestion" and n == 1 and method == "GET":
            return self._send(200, envelope({"runs": _store.ingestion_runs(20)}))

        # ---------------- application messaging ------------------------------
        if head == "messages" and n == 1:
            uid = self._require_auth()
            if not uid:
                return
            if method == "POST":
                b = self._body() or {}
                aid = str(b.get("application_id") or "")
                body = str(b.get("body") or "").strip()[:4000]
                if not body:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_BODY", "message": "Message body is required"}))
                m, err = _store.send_message(aid, uid, body)
                if err == "NOT_FOUND":
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Application not found"}))
                if err == "FORBIDDEN":
                    return self._send(403, envelope(None, error={
                        "code": "FORBIDDEN",
                        "message": "Only the applicant and the employer of this "
                                   "job can message on this application"}))
                a, _j, _e = _store.app_parties(aid)
                _store.notify(m["recipient_id"], "message",
                               "New message on an application", body[:120])
                _store.audit_event(uid, "message.send", None, {"applicationId": aid})
                return self._send(200, envelope({"message": m}))
            if method == "GET":
                aid = q.get("application_id", [None])[0]
                if not aid:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_REQUEST", "message": "application_id is required"}))
                msgs, err = _store.messages_of(aid, uid)
                if err == "NOT_FOUND":
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Application not found"}))
                if err == "FORBIDDEN":
                    return self._send(403, envelope(None, error={
                        "code": "FORBIDDEN",
                        "message": "Only the applicant and the employer of this job "
                                   "can read this thread"}))
                return self._send(200, envelope({"messages": msgs}))

        # ---------------- employer portal ------------------------------------
        if head == "employers" and n == 1:
            uid = self._require_auth()
            if not uid:
                return
            if method == "POST":
                b = self._body() or {}
                name = str(b.get("company_name") or "").strip()[:120]
                if not name:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_REQUEST", "message": "company_name is required"}))
                website = str(b.get("website") or "").strip()[:300]
                if website and not URL_RE.match(website):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_URL", "message": "website must be a valid http(s) URL"}))
                e, created = _store.register_employer(uid, name, website or None,
                                                     str(b.get("about") or "")[:4000] or None,
                                                     str(b.get("logo_url") or "").strip()[:500] or None)
                _store.audit_event(uid, "employer.register" if created else "employer.exists",
                                   None, {"employerId": e["id"]})
                return self._send(200, envelope({
                    "employer": e, "created": created,
                    "note": "Verification is manual. Your company shows a "
                            "'pending verification' state until a human reviews it — "
                            "no badges are shown before that."}))
            if method == "GET":
                e = _store.employer_by_user(uid)
                return self._send(200, envelope({"employer": e}))

        if head == "employers" and n == 2 and r[1] == "me" and method == "PATCH":
            uid = self._require_auth()
            if not uid:
                return
            e = _store.employer_by_user(uid)
            if not e:
                return self._send(404, envelope(None, error={
                    "code": "NOT_EMPLOYER", "message": "Register as an employer first"}))
            b = self._body() or {}
            updates = {}
            if "about" in b:
                updates["about"] = str(b["about"])[:4000] or None
            if "website" in b:
                w = str(b["website"]).strip()[:300]
                if w and not URL_RE.match(w):
                    return self._send(400, envelope(None, error={
                        "code": "BAD_URL", "message": "website must be a valid http(s) URL"}))
                updates["website"] = w or None
            if "logo_url" in b:
                updates["logo_url"] = str(b["logo_url"]).strip()[:500] or None
            for k, v in updates.items():
                _store.patch_employer(e["id"], k, v)
            return self._send(200, envelope({"employer": _store.employer_by_user(uid)}))

        if head == "employers" and n == 2 and r[1] == "jobs":
            uid = self._require_auth()
            if not uid:
                return
            e = _store.employer_by_user(uid)
            if not e:
                return self._send(404, envelope(None, error={
                    "code": "NOT_EMPLOYER", "message": "Register as an employer first"}))
            if method == "POST":
                if not rate_ok(f"postjob:{uid}", 10):
                    return self._send(429, envelope(None, error={
                        "code": "RATE_LIMITED", "message": "Too many posts, slow down"}))
                try:
                    j = _store.post_job(e["id"], self._body() or {})
                except ValueError as exc:
                    return self._send(400, envelope(None, error={
                        "code": "BAD_JOB", "message": str(exc)}))
                _store.log_ingestion("employer-direct", "ok",
                                     {"imported": 1, "employer": e["company_name"]})
                _store.audit_event(uid, "employer.job.post", None, {"jobId": j["id"]})
                return self._send(200, envelope({
                    "job": job_public(j),
                    "note": "Your post is live immediately but shows as unverified "
                            "until an admin verifies it. Verification never happens "
                            "automatically."}))
            if method == "GET":
                return self._send(200, envelope(
                    {"jobs": [job_public(j) for j in _store.employer_jobs(e["id"])],
                     "employer": e}))

        if head == "employers" and n == 2 and r[1] == "applicants" and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            e = _store.employer_by_user(uid)
            if not e:
                return self._send(404, envelope(None, error={
                    "code": "NOT_EMPLOYER", "message": "Register as an employer first"}))
            page, limit = self._page(q)
            apps, total = _store.employer_applications(e["id"], page, limit)
            out = []
            for a in apps:
                cand = _store.get_user(a["user_id"]) or {}
                job = _store.get_job(a["job_id"]) or {}
                out.append({
                    "application": a,
                    "candidate": {"display_name": cand.get("display_name"),
                                   "email": cand.get("email"),
                                   "avatar_url": cand.get("avatar_url"),
                                   "headline": cand.get("headline")},
                    "job": {"id": job.get("id"), "title": job.get("title")}})
            return self._send(200, envelope(
                {"applicants": out, "employer": e}, self._meta(page, limit, total)))

        if head == "employers" and n == 3 and r[1] == "applicants" and method == "PATCH":
            uid = self._require_auth()
            if not uid:
                return
            e = _store.employer_by_user(uid)
            if not e:
                return self._send(404, envelope(None, error={
                    "code": "NOT_EMPLOYER", "message": "Register as an employer first"}))
            a, _j, emp = _store.app_parties(r[2])
            if not a or not emp or emp["user_id"] != uid:
                return self._send(403, envelope(None, error={
                    "code": "FORBIDDEN",
                    "message": "You can only update applications to your own postings"}))
            b = self._body() or {}
            patch = {}
            if "status" in b:
                patch["status"] = str(b["status"]).upper()
            if "next_action" in b:
                patch["next_action"] = str(b["next_action"])[:400]
            a2, err = _store.update_application(a["id"], patch)
            if err == "INVALID_TRANSITION":
                return self._send(422, envelope(None, error={
                    "code": "INVALID_TRANSITION",
                    "message": f"Cannot move {a['status']} -> {patch.get('status')}"}))
            if err:
                return self._send(404, envelope(None, error={
                    "code": "NOT_FOUND", "message": "Application not found"}))
            _store.audit_event(uid, "employer.application.update", None,
                               {"applicationId": a["id"], "status": patch.get("status")})
            if patch.get("status"):
                _store.notify(a["user_id"], "application",
                              f"Application moved to {patch['status']}",
                              f"{_store.get_job(a['job_id'])['title']} — status updated "
                              f"by the employer.")
            return self._send(200, envelope({"application": a2}))

        # ---------------- privacy: account export ------------------------------
        if head == "privacy" and n == 2 and r[1] == "export" and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            apps, _t = _store.applications(uid, None, 1, 500)
            export = {
                "exportedAt": time.time(),
                "user": _store.get_user(uid),
                "consents": _store.consents_of(uid),
                "resume": _store.latest_resume(uid),
                "applications": [{**a, "job": job_public(_store.get_job(a["job_id"]) or {})}
                                 for a in apps],
                "saved_jobs": [job_public(_store.get_job(s) or {}) for s in _store.saved_jobs(uid)],
                "notifications": _store.notifications(uid),
                "employer": _store.employer_by_user(uid),
            }
            _store.audit_event(uid, "account.export", None, {})
            return self._send(200, envelope({"export": export}))

        # ---------------- admin (manual verification) ---------------------------
        if head == "admin" and n == 3 and method == "POST":
            token = os.environ.get("ADMIN_TOKEN")
            auth = self.headers.get("Authorization") or ""
            if not token:
                return self._send(501, envelope(None, error={
                    "code": "NOT_CONFIGURED",
                    "message": "ADMIN_TOKEN is not configured; verification is manual "
                               "until it is set"}))
            if auth != f"Bearer {token}":
                return self._send(401, envelope(None, error={
                    "code": "UNAUTHORIZED", "message": "Invalid admin token"}))
            b = self._body() or {}
            status = str(b.get("status") or "").lower()
            if status not in ("verified", "rejected", "unverified"):
                return self._send(400, envelope(None, error={
                    "code": "BAD_STATUS",
                    "message": "status must be verified | rejected | unverified"}))
            if r[1] == "employers":
                e = _store.verify_employer(r[2], status)
                if not e:
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Employer not found"}))
                if status == "verified" and e.get("user_id"):
                    _store.notify(e["user_id"], "account", "Company verified",
                                  f"{e['company_name']} is now verified. Your postings "
                                  f"can earn the verified badge.")
                _store.audit_event(None, "admin.employer.verify", None,
                                   {"employerId": r[2], "status": status})
                return self._send(200, envelope({"employer": e}))
            if r[1] == "jobs":
                j = _store.set_job_verification(r[2], status, "admin", "manual review")
                if not j:
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Job not found"}))
                _store.audit_event(None, "admin.job.verify", None,
                                   {"jobId": r[2], "status": status})
                return self._send(200, envelope({"job": job_public(j)}))

        # ---------------- application detail (own only) -----------------------
        if head == "applications" and n == 2 and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            rows = _store.applications(uid, None, 1, 500)[0]
            a = next((x for x in rows if x["id"] == r[1]), None)
            if not a:
                return self._send(404, envelope(None, error={
                    "code": "NOT_FOUND", "message": "Application not found"}))
            return self._send(200, envelope({"application": a}))

        # ---------------- per-job preparation workspace ------------------------
        if head == "jobs" and n == 3 and r[2] == "prep" and method == "GET":
            uid = self._require_auth()
            if not uid:
                return
            j = _store.get_job(r[1])
            if not j:
                return self._send(404, envelope(None, error={
                    "code": "NOT_FOUND", "message": "Job not found"}))
            res = _store.latest_resume(uid)
            parsed = (res or {}).get("parsed") or {}
            u = _store.get_user(uid) or {}
            m = _career.match_job(parsed, j)
            _store.track("prep_open", uid, r[1])
            return self._send(200, envelope({
                "prep": _career.prep_workspace(parsed, j, u),
                "match": m, "hasResume": bool(res)}))

        # ---------------- career copilot ---------------------------------------
        if head == "jobs" and n == 3 and r[2] == "copilot" and method == "POST":
            uid = self._require_auth()
            if not uid:
                return
            if not rate_ok(f"copilot:{uid}", 20):
                return self._send(429, envelope(None, error={
                    "code": "RATE_LIMITED", "message": "Copilot limit reached, try later"}))
            b = self._body() or {}
            question = str(b.get("question") or "").strip()[:500]
            if len(question) < 4:
                return self._send(400, envelope(None, error={
                    "code": "BAD_QUESTION", "message": "Ask a question first"}))
            j = _store.get_job(r[1])
            if not j:
                return self._send(404, envelope(None, error={
                    "code": "NOT_FOUND", "message": "Job not found"}))
            res = _store.latest_resume(uid)
            parsed = (res or {}).get("parsed") or {}
            m = _career.match_job(parsed, j)
            llm = _career.llm_copilot(question, j, parsed, m)
            if llm:
                out = {"answer": llm, "engine": "llm", "intent": "freeform"}
            else:
                out = _career.copilot_answer(question, j, parsed, m)
                out["engine"] = "rules"
            out["disclaimer"] = ("AI-generated guidance based only on this listing and your "
                                 "resume. Not an employer statement or a hiring prediction — "
                                 "verify against the official source.")
            _store.track("copilot_use", uid, r[1])
            return self._send(200, envelope({"copilot": out}))

        # ---------------- admin: real usage analytics ---------------------------
        if head == "admin" and n == 2 and r[1] == "analytics" and method == "GET":
            token = os.environ.get("ADMIN_TOKEN")
            auth = self.headers.get("Authorization") or ""
            if not token:
                return self._send(501, envelope(None, error={
                    "code": "NOT_CONFIGURED",
                    "message": "ADMIN_TOKEN is not configured"}))
            if auth != f"Bearer {token}":
                return self._send(401, envelope(None, error={
                    "code": "UNAUTHORIZED", "message": "Invalid admin token"}))
            days = 30
            try:
                days = max(1, min(365, int(q.get("days", ["30"])[0])))
            except ValueError:
                pass
            return self._send(200, envelope(
                {"analytics": _store.analytics_summary(days),
                 "ingestionRuns": _store.ingestion_runs(10)}))

        return self._send(404, envelope(None, error={
            "code": "NOT_FOUND", "message": f"Unknown route '{'/' + '/'.join(r)}'"}))

    def _abs_redirect(self) -> str:
        """Absolute OAuth redirect URI matching the one registered with the
        provider (GOOGLE_REDIRECT may be a relative path by default)."""
        if GOOGLE_REDIRECT.startswith("http"):
            return GOOGLE_REDIRECT
        proto = self.headers.get("x-forwarded-proto") or \
            ("https" if APP_ORIGIN.startswith("https") else "http")
        host = self.headers.get("host") or urlparse(APP_ORIGIN).netloc \
            or "aws-event-media-platform.vercel.app"
        return f"{proto}://{host}{GOOGLE_REDIRECT}"

    def _session_cookie(self, token):
        secure = " Secure;" if APP_ORIGIN.startswith("https") else ""
        if token:
            v = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax;{secure} Max-Age={SESSION_TTL}"
        else:
            v = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax;{secure} Max-Age=0"
        return {"Set-Cookie": v}


# Vercel entrypoint: on this runtime every /api/* request reaches this file,
# so the marketplace handler subclasses the media handler and routes by prefix.
handler = MFHandler
