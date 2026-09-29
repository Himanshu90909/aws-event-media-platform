"""CI tests for the MediaFlow Jobs Platform (merged into api/index.py).

Covers the spec's definition-of-done core: application transitions,
duplicate protection, ownership isolation, notifications dedup,
presign validation, and store behavior. Runs against the explicit
in-memory DEV store (no DATABASE_URL in CI).
"""
import importlib.util
import json
import os
import uuid

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the platform is merged into api/index.py (single-function Vercel runtime)
SPEC = importlib.util.spec_from_file_location("app", os.path.join(REPO, "api", "index.py"))
mp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mp)


@pytest.fixture()
def store():
    s = mp.MemStore()
    return s


@pytest.fixture()
def user(store):
    uid, _u, _rest = store.upsert_user_identity("google", "sub-1", "a@x.dev", "A", None)
    return uid


@pytest.fixture()
def job(store):
    return next(iter(store.jobs.values()))["id"]


# ---------------------------------------------------------------- transitions
def test_valid_transition_chain(store, user, job):
    a, created = store.create_application(user, job)
    assert created
    for st in ("APPLIED", "SCREENING", "INTERVIEW", "OFFER"):
        a, err = store.update_application(a["id"], {"status": st})
        assert err is None and a["status"] == st
    assert [h["status"] for h in a["status_history"]] == \
        ["SAVED", "APPLIED", "SCREENING", "INTERVIEW", "OFFER"]


def test_invalid_transition_rejected(store, user, job):
    a, _ = store.create_application(user, job)
    a2, err = store.update_application(a["id"], {"status": "OFFER"})
    assert err == "INVALID_TRANSITION" and a2 is None


def test_rejected_from_saved(store, user, job):
    a, _ = store.create_application(user, job)
    a2, err = store.update_application(a["id"], {"status": "REJECTED"})
    assert err is None and a2["status"] == "REJECTED"


def test_duplicate_application_protection(store, user, job):
    a1, c1 = store.create_application(user, job)
    a2, c2 = store.create_application(user, job)
    assert c1 is True and c2 is False and a1["id"] == a2["id"]


# ------------------------------------------------------------------ ownership
def test_row_level_isolation(store, job):
    u1, _, _ = store.upsert_user_identity("google", "s1", "a@x.dev", "A", None)
    u2, _, _ = store.upsert_user_identity("google", "s2", "b@x.dev", "B", None)
    a, _ = store.create_application(u1, job)
    assert store.get_application(a["id"])["user_id"] == u1
    # u2 must not surface u1's application in their board
    assert all(x["user_id"] == u2 for x in store.applications(u2)[0]) is True or store.applications(u2)[1] == 0
    # get_file enforces ownership
    f = store.create_file(u1, None, "k", "r.pdf", "application/pdf", 1)
    assert store.get_file(u2, f["id"]) is None


# -------------------------------------------------------------- notifications
def test_notification_dedup_24h(store, user):
    n1 = store.notify(user, "application", "T", "B")
    n2 = store.notify(user, "application", "T", "B")
    assert n1 is not None and n2 is None
    assert len(store.notifications(user)) == 1


# --------------------------------------------------------------------- files
def test_presign_constants():
    assert mp.MAX_FILE_BYTES == 25 * 1024 * 1024
    assert "application/x-msdownload" not in mp.FILE_TYPES
    assert "application/pdf" in mp.FILE_TYPES


# -------------------------------------------------------------------- search
def test_job_search_and_expiry(store):
    items, total = store.query_jobs(None, "AI Engineer", None, None, None, None, None, 1, 20)
    assert total >= 1 and all(j["category"] == "AI Engineer" for j in items)
    # expired jobs are hidden
    j = next(iter(store.jobs.values()))
    j["expires_at"] = 0
    assert j not in store.query_jobs(None, None, None, None, None, None, None, 1, 200)[0]


# ------------------------------------------------------------------- session
def test_session_sign_roundtrip():
    tok = mp.make_session(uuid.uuid4().hex)
    assert mp.read_session(tok) is not None
    assert mp.read_session(tok + "tamper") is None
    st = mp.oauth_state("/jobs")
    assert mp.verify_state(st) is not None
    assert mp.verify_state("garbage") is None


def test_account_deletion_cascades(store, user, job):
    store.create_application(user, job)
    store.notify(user, "application", "T", "B")
    store.delete_user(user)
    assert store.get_user(user) is None
    assert store.applications(user, page=1, limit=100)[1] == 0
    assert store.notifications(user) == []


# ------------------------------------------------------------------ types
def test_catalog_file_valid():
    p = os.path.join(REPO, "api", "opportunities.json")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"]
    assert len(items) >= 25
    for it in items:
        assert it["type"] in mp.JTYPES, it.get("title")
        assert it["title"] and it["company"] and it["category"]
        assert it["apply_url"].startswith("https://"), it["title"]
        assert it["description"].strip()


def test_types_present_after_seed():
    s = mp.MemStore()
    types = {j.get("type") for j in s.jobs.values()}
    for needed in ("INTERNSHIP", "HACKATHON", "EVENT", "RESEARCH",
                   "FELLOWSHIP", "INNOVATION_LAB"):
        assert needed in types, needed


def test_type_filter(store, job):
    s = mp.MemStore()
    items, total = s.query_jobs(None, None, None, None, None, None, None, 1, 200,
                                jtype="INTERNSHIP")
    assert total >= 1
    assert all(j["type"] == "INTERNSHIP" for j in items)
    # the pre-existing seed rows default to JOB
    items, total = s.query_jobs(None, None, None, None, None, None, None, 1, 200,
                                jtype="JOB")
    assert total >= 1 and all(j["type"] == "JOB" for j in items)


def test_mk_live_shape():
    it = mp._mk_live("test", "HACKATHON", "x1", "Test Hack", "Acme",
                    "Online", "Remote", "https://acme.dev", ["python"],
                    posted=1700000000, description="d")
    pub = mp.job_public(it)
    assert pub["type"] == "HACKATHON" and pub["live"] is True
    assert pub["applyUrl"] == "https://acme.dev"
