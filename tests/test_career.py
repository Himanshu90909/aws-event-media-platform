"""Career-intelligence tests: parser, explainable matcher, roadmap, consent,
employer portal permissions, messaging authorization, reports, duplicates."""
import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("career", os.path.join(REPO, "api", "career.py"))
career = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(career)

SPEC2 = importlib.util.spec_from_file_location("app", os.path.join(REPO, "api", "index.py"))
mp = importlib.util.module_from_spec(SPEC2)
SPEC2.loader.exec_module(mp)

RESUME = """
Himanshu Suthar — B.Tech student, Lovely Professional University
Skills: Python, React, SQL, Docker, machine learning, RAG.
Education: B.Tech Computer Science, expected 2027.
Experience: internship 2025 (June–Aug 2025) — built n8n automations.
Projects:
  - TruthSearch: RAG-based misinformation checker (Python, LangChain)
  - HB Innovators community platform (JavaScript, Node.js)
2 years of experience with personal projects.
"""


@pytest.fixture()
def store():
    return mp.MemStore()


@pytest.fixture()
def user(store):
    uid, _u, _r = store.upsert_user_identity("google", "sub-c1", "c@x.dev", "C", None)
    return uid


# ------------------------------------------------------------- parser
def test_parse_resume_extracts_deterministic_facts():
    p = career.parse_resume(RESUME)
    assert "Python" in p["skills"]
    assert "React" in p["skills"]
    assert "RAG" in p["skills"]
    assert p["education"] == "B.Tech"
    assert p["graduation_year"] == 2027
    assert p["experience_years"] == 2.0
    assert any("TruthSearch" in proj for proj in p["projects"])
    assert "correct" in p["extraction_note"]


def test_parse_empty_resume_is_safe():
    p = career.parse_resume("")
    assert p["skills"] == [] and p["projects"] == []
    assert p["experience_years"] == 0.0


# ------------------------------------------------------------- matcher
def test_match_is_explainable_and_honest():
    parsed = career.parse_resume(RESUME)
    job = {"id": "j1", "title": "AI Engineer", "company": "X",
           "skills": ["Python", "RAG", "Kubernetes", "Java"],
           "experience_level": "entry", "expires_at": 9999999999.0}
    m = career.match_job(parsed, job)
    assert 0 <= m["score"] <= 1
    assert "Python" in m["required"]["matched"]
    assert "Kubernetes" in m["required"]["missing"]
    assert m["evidence"], "evidence must not be empty"
    assert "not an application decision" in m["uncertainty"]
    assert "coverage score" in m["method"]


def test_match_without_resume_is_summary_only():
    m = career.match_job({}, {"id": "j1", "title": "X", "company": "Y", "skills": ["Python"]})
    assert "No resume" in m["uncertainty"]
    assert m["eligibility"]["passed"] is True


def test_match_respects_explicit_required_preferred():
    job = {"id": "j", "title": "T", "company": "C",
           "skills_required": ["Python"], "skills_preferred": ["Go"]}
    m = career.match_job({"skills": ["Python"]}, job)
    assert m["required"]["missing"] == []
    assert m["preferred"]["matched"] == [] and m["preferred"]["missing"] == ["Go"]


# ------------------------------------------------------------- roadmap
def test_roadmap_gap_and_resources():
    rd = career.roadmap_for({"skills": ["Python", "LLMs"]}, "AI Engineer")
    assert rd["skills_have"] == ["Python", "LLMs"]
    gaps = [g["skill"] for g in rd["skills_gap"]]
    assert "RAG" in gaps and "Docker" in gaps
    rag = next(g for g in rd["skills_gap"] if g["skill"] == "RAG")
    assert rag["resources"], "gap items must link real resources"
    assert "not a promise" in rd["note"]


def test_roadmap_unknown_role_lists_roles():
    rd = career.roadmap_for({}, "Astronaut")
    assert rd["error"] == "UNKNOWN_ROLE" and rd["roles"]


# ------------------------------------------------------------- hygiene
def test_fraud_signals_and_stale():
    assert career.fraud_signals("Pay a registration fee of 500") == ["upfront fee"]
    assert career.fraud_signals("") == []
    assert career.fraud_signals("Normal job description") == []
    assert career.is_stale({"expires_at": 1.0}, 1.0 + 5 * 86400) is True
    assert career.is_stale({"expires_at": 1.0}, 1.0 + 86400) is False


def test_normalize_job_never_invents_values():
    j = career.normalize_job({"title": " Eng ", "company": " Acme "}, "s")
    assert j["title"] == "Eng" and j["company"] == "Acme"
    assert j["apply_url"] is None and j["salary_min"] is None
    with pytest.raises(ValueError):
        career.normalize_job({"company": "NoTitle"}, "s")


def test_duplicate_key_normalizes_case_and_punct():
    a = career.duplicate_key("Remotive", "Senior  Engineer!", "acme corp")
    b = career.duplicate_key("remotive", "senior engineer", "Acme Corp")
    assert a == b


# ------------------------------------------------- store: resume + consent
def test_resume_requires_consent_flow(store, user):
    res = store.save_resume(user, "r.txt", "text/plain", RESUME, consent=True)
    assert res["consent"] is True
    assert store.latest_resume(user)["parsed"]["skills"]
    cons = store.consents_of(user)
    assert cons[0]["kind"] == "resume_processing" and cons[0]["granted"] is True
    store.delete_resume(user)
    assert store.latest_resume(user) is None
    assert store.consents_of(user)[0]["granted"] is False


def test_resume_user_corrections_persist(store, user):
    res = store.save_resume(user, "r.txt", "text/plain", RESUME, True)
    out = store.update_resume_parsed(res["id"], {"skills": ["Python", "Chef"]})
    assert out["parsed"]["skills"] == ["Python", "Chef"]
    assert out["parsed"]["graduation_year"] == 2027  # untouched keys survive


# ------------------------------------------------- store: employer portal
def test_employer_lifecycle_and_badge_honesty(store, user):
    e, created = store.register_employer(user, "Acme Labs", "https://acme.dev", None, None)
    assert created and e["verification_status"] == "pending"
    e2, created2 = store.register_employer(user, "Acme Again", None, None, None)
    assert not created2 and e2["id"] == e["id"]       # one per user
    j = store.post_job(e["id"], {"title": "AI Engineer", "company": "Acme Labs",
                                 "description": "Build agents", "skills": ["Python"],
                                 "category": "Jobs"})
    assert j["is_verified"] is False                  # no badge without verification
    assert j["posted_by_employer"] == e["id"]
    store.verify_employer(e["id"], "verified")
    assert store.employer_by_user(user)["verification_status"] == "verified"


def test_post_job_rejects_missing_title(store, user):
    e, _ = store.register_employer(user, "Acme", None, None, None)
    with pytest.raises(ValueError):
        store.post_job(e["id"], {"company": "Acme"})


def test_employer_sees_only_own_applicants(store, user):
    e, _ = store.register_employer(user, "Acme", None, None, None)
    j = store.post_job(e["id"], {"title": "Dev", "company": "Acme", "description": "d"})
    uid2, _u, _r = store.upsert_user_identity("google", "sub-c2", "b@x.dev", "B", None)
    store.create_application(uid2, j["id"])
    apps, total = store.employer_applications(e["id"])
    assert total == 1 and apps[0]["user_id"] == uid2
    uid3, _u, _r = store.upsert_user_identity("google", "sub-c3", "c@x.dev", "C", None)
    other = next(iter(store.jobs.values()))["id"]      # seed job not owned by Acme
    store.create_application(uid3, other)
    assert store.employer_applications(e["id"])[1] == 1  # still only own postings


# ------------------------------------------------- messaging authorization
def test_messaging_only_between_application_parties(store, user):
    e, _ = store.register_employer(user, "Acme", None, None, None)
    j = store.post_job(e["id"], {"title": "Dev", "company": "Acme", "description": "d"})
    uid2, _u, _r = store.upsert_user_identity("google", "sub-c2", "b@x.dev", "B", None)
    a, _c = store.create_application(uid2, j["id"])
    m, err = store.send_message(a["id"], uid2, "Hello! Interested in this role.")
    assert err is None and m["recipient_id"] == user
    m2, err2 = store.send_message(a["id"], user, "Thanks — we'll review your profile.")
    assert err2 is None and m2["recipient_id"] == uid2
    # a third user cannot write or read the thread
    uid3, _u, _r = store.upsert_user_identity("google", "sub-c3", "c@x.dev", "C", None)
    _m3, err3 = store.send_message(a["id"], uid3, "hi")
    assert err3 == "FORBIDDEN"
    _msgs, err4 = store.messages_of(a["id"], uid3)
    assert err4 == "FORBIDDEN"
    msgs, _ = store.messages_of(a["id"], uid2)
    assert len(msgs) == 2


# ------------------------------------------------- reports & verification
def test_report_and_verification_events(store, user):
    jid = next(iter(store.jobs.values()))["id"]
    rep = store.report_job(user, jid, "fraud", "Asks for a fee")
    assert rep["status"] == "open"
    j = store.set_job_verification(jid, "verified", "admin", "checked apply URL")
    assert j["is_verified"] is True and j["last_verified_at"]
    hist = store.verifications_of(jid)
    assert hist[0]["status"] == "verified" and hist[0]["actor"] == "admin"


def test_learning_progress(store, user):
    assert store.set_progress(user, "AI Engineer", "RAG", "in_progress")
    assert store.progress_of(user, "AI Engineer") == {"RAG": "in_progress"}
    assert store.set_progress(user, "AI Engineer", "RAG", "bogus") is None


def test_assessment_transition_exists(store, user):
    jid = next(iter(store.jobs.values()))["id"]
    a, _ = store.create_application(user, jid)
    _a2, err = store.update_application(a["id"], {"status": "APPLIED"})
    assert err is None
    a3, err3 = store.update_application(a["id"], {"status": "ASSESSMENT"})
    assert err3 is None and a3["status"] == "ASSESSMENT"


def test_delete_user_purges_private_data(store, user):
    store.save_resume(user, "r.txt", "text/plain", RESUME, True)
    store.delete_user(user)
    assert store.latest_resume(user) is None
    assert store.consents_of(user) == []
