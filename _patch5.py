"""Patch 5: index.py — stages (PREPARING/WITHDRAWN), analytics, app detail,
prep workspace route, copilot route, admin analytics, detail enhancements."""
src = open('api/index.py').read()

# ---- 1. pipeline transitions
old = '''TRANSITIONS = {
    "SAVED":     {"APPLIED", "REJECTED"},
    "APPLIED":   {"ASSESSMENT", "SCREENING", "INTERVIEW", "REJECTED"},
    "ASSESSMENT": {"INTERVIEW", "REJECTED"},
    "SCREENING": {"INTERVIEW", "REJECTED"},
    "INTERVIEW": {"OFFER", "REJECTED"},
    "OFFER":     set(),
    "REJECTED":  set(),
}'''
new = '''TRANSITIONS = {
    "SAVED":      {"PREPARING", "APPLIED", "REJECTED"},
    "PREPARING":  {"APPLIED", "REJECTED", "WITHDRAWN"},
    "APPLIED":    {"ASSESSMENT", "SCREENING", "INTERVIEW", "REJECTED", "WITHDRAWN"},
    "ASSESSMENT": {"INTERVIEW", "REJECTED", "WITHDRAWN"},
    "SCREENING":  {"INTERVIEW", "REJECTED", "WITHDRAWN"},
    "INTERVIEW":  {"OFFER", "REJECTED", "WITHDRAWN"},
    "OFFER":      set(),
    "REJECTED":   set(),
    "WITHDRAWN":  set(),
}'''
assert old in src, "1"; src = src.replace(old, new, 1)

# ---- 2. MemStore: application_for + analytics methods (insert before save_resume)
old = '''    # ---------------- career intelligence -------------------------------
    def save_resume(self, uid, file_name, content_type, text, consent):'''
new = '''    # ---------------- career intelligence -------------------------------
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

    def save_resume(self, uid, file_name, content_type, text, consent):'''
assert old in src, "2"; src = src.replace(old, new, 1)

# ---- 3. PgStore: application_for + analytics methods
old = '''    # ---------------- career intelligence (SQL) ------------------------------
    def save_resume(self, uid, file_name, content_type, text, consent):'''
new = '''    # ---------------- career intelligence (SQL) ------------------------------
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

    def save_resume(self, uid, file_name, content_type, text, consent):'''
assert old in src, "3"; src = src.replace(old, new, 1)

# ---- 4. MemStore needs an events dict: add to __init__
import re as _re
m = _re.search(r'class MemStore:\n(    def __init__\(self\):\n(?:        .*\n)+)', src)
assert m, "4a"
init = m.group(1)
if "self.events" not in init:
    src = src.replace(init, init.rstrip("\n") + "\n        self.events = {}\n", 1)

# ---- 5. detail route: sections + user's application + view tracking
old = '''            if n == 2 and method == "GET":
                j = _store.get_job(r[1])
                if not j:
                    return self._send(404, envelope(None, error={
                        "code": "NOT_FOUND", "message": "Job not found"}))
                uid = self._uid()
                return self._send(200, envelope({
                    "job": job_public(j, _store.is_saved(uid, r[1]) if uid else False),
                    "similar": [job_public(s) for s in _store.similar_jobs(r[1])],
                    "verificationHistory": _store.verifications_of(r[1]),
                    "cautions": _career.fraud_signals(j.get("description") or "")}))'''
new = '''            if n == 2 and method == "GET":
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
                    "application": (_store.application_for(uid, r[1]) if uid else None)}))'''
assert old in src, "5"; src = src.replace(old, new, 1)

# ---- 6. track save/apply/apply-click events
old = '''                    _store.save_job(uid, r[1], note)
                    _store.audit_event(uid, "job.save", None, {"jobId": r[1]})
                    return self._send(200, envelope({"saved": True}))'''
new = '''                    _store.save_job(uid, r[1], note)
                    _store.audit_event(uid, "job.save", None, {"jobId": r[1]})
                    _store.track("job_save", uid, r[1])
                    return self._send(200, envelope({"saved": True}))'''
assert old in src, "6a"; src = src.replace(old, new, 1)

old = '''                a, created = _store.create_application(uid, r[1])
                _store.audit_event(uid, "application.apply" if created else "application.apply.dup",
                                  None, {"jobId": r[1]})'''
new = '''                a, created = _store.create_application(uid, r[1])
                _store.audit_event(uid, "application.apply" if created else "application.apply.dup",
                                  None, {"jobId": r[1]})
                _store.track("apply_start", uid, r[1])'''
assert old in src, "6b"; src = src.replace(old, new, 1)

old = '''                res = _store.save_resume(uid, str(b.get("file_name") or "resume.txt")[:200],
                                         "text/plain", text, True)
                _store.audit_event(uid, "resume.upload", None, {"length": len(text)})'''
new = '''                res = _store.save_resume(uid, str(b.get("file_name") or "resume.txt")[:200],
                                         "text/plain", text, True)
                _store.audit_event(uid, "resume.upload", None, {"length": len(text)})
                _store.track("resume_analyze", uid, None)'''
assert old in src, "6c"; src = src.replace(old, new, 1)

# ---- 7. new routes (insert before the final unknown-route 404)
anchor = '''        return self._send(404, envelope(None, error={
            "code": "NOT_FOUND", "message": f"Unknown route '{'/' + '/'.join(r)}'"}))'''
routes = '''        # ---------------- application detail (own only) -----------------------
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

''' + anchor
assert anchor in src, "7"; src = src.replace(anchor, routes, 1)

open('api/index.py', 'w').write(src)
print("index.py patched")
