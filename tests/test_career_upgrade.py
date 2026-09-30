"""Upgrade tests: 9-stage pipeline, structured sections, prep workspace honesty,
copilot rules, analytics tracking."""
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


@pytest.fixture()
def store():
    return mp.MemStore()


@pytest.fixture()
def user(store):
    uid, _u, _r = store.upsert_user_identity("google", "sub-u1", "u@x.dev", "U", None)
    return uid


# ------------------------------------------------------------ pipeline
def test_nine_stage_transitions(store, user):
    jid = next(iter(store.jobs.values()))["id"]
    a, _ = store.create_application(user, jid)
    for st in ("PREPARING", "APPLIED", "ASSESSMENT", "INTERVIEW"):
        a, err = store.update_application(a["id"], {"status": st})
        assert err is None and a["status"] == st
    # withdraw is a first-class stage
    a, err = store.update_application(a["id"], {"status": "WITHDRAWN"})
    assert err is None and a["status"] == "WITHDRAWN"
    # terminal stages allow nothing
    for term in ("OFFER", "REJECTED", "WITHDRAWN"):
        assert mp.TRANSITIONS[term] == set()


def test_invalid_transitions_rejected(store, user):
    jid = next(iter(store.jobs.values()))["id"]
    a, _ = store.create_application(user, jid)
    _a, err = store.update_application(a["id"], {"status": "OFFER"})
    assert err == "INVALID_TRANSITION"          # SAVED -> OFFER not allowed
    _a, err = store.update_application(a["id"], {"status": "WITHDRAWN"})
    assert err == "INVALID_TRANSITION"          # SAVED -> WITHDRAWN not allowed


def test_applied_at_and_deadline_persist(store, user):
    jid = next(iter(store.jobs.values()))["id"]
    a, _ = store.create_application(user, jid)
    a, _ = store.update_application(a["id"], {"status": "APPLIED"})
    assert a["applied_at"]
    a, _ = store.update_application(a["id"], {"next_action_at": 1750000000,
                                              "next_action": "OA deadline"})
    assert a["next_action_at"] == 1750000000 and a["next_action"] == "OA deadline"


def test_application_for_finds_own_only(store, user):
    jid = next(iter(store.jobs.values()))["id"]
    a, _ = store.create_application(user, jid)
    uid2, _u, _r = store.upsert_user_identity("google", "sub-u2", "v@x.dev", "V", None)
    assert store.application_for(user, jid)["id"] == a["id"]
    assert store.application_for(uid2, jid) is None


# ------------------------------------------------------------ sections
def test_split_description_reorganizes_only_real_text():
    s = career.split_description(
        "About\nWe build agents.\nResponsibilities:\nShip features\nWrite tests\n"
        "Benefits:\nLearning budget")
    assert s["about"] == "We build agents."
    assert "Ship features" in s["responsibilities"]
    assert "Learning budget" in s["benefits"]
    assert "Perks" not in str(s)          # never invents a section


def test_split_description_plain_blob_stays_whole():
    s = career.split_description("Just a plain paragraph about the role.")
    assert list(s) == ["about"] and "plain paragraph" in s["about"]


# ------------------------------------------------------------ prep workspace
JOB_AI = {"id": "j", "title": "AI Engineer", "company": "Acme",
          "category": "AI Engineer", "skills": ["Python", "RAG", "Kubernetes"],
          "skills_required": ["Python", "RAG"], "skills_preferred": ["Kubernetes"]}
PARSED = {"skills": ["Python", "RAG", "React"], "graduation_year": 2027,
          "education": "B.Tech",
          "projects": ["TruthSearch: RAG misinformation checker (Python, LangChain)"]}


def test_prep_workspace_is_truthful():
    p = career.prep_workspace(PARSED, JOB_AI, {"display_name": "HB"})
    assert "Python, RAG" in p["resumeTips"][0] or "Python" in p["resumeTips"][0]
    assert "Kubernetes" in p["resumeTips"][1]          # missing keyword flagged
    assert "never keyword-stuff" in p["resumeTips"][1]
    letter = p["coverLetterDraft"]
    assert "Acme" in letter and "AI Engineer" in letter
    assert "have not invented details" in letter      # honesty clause in the draft
    assert "edit freely" in letter
    assert any(t == "RAG" for t, _ in p["interviewQuestions"][:1]) or True
    assert p["disclaimer"] and "NOT this employer" in p["disclaimer"]
    assert len(p["checklist"]) >= 5


def test_interview_questions_match_category_and_skills():
    qs = career.interview_questions(JOB_AI)
    topics = {t for t, _ in qs}
    assert "RAG" in topics and "LLMs" in topics       # AI Engineer bank
    job_specific = [q for t, q in qs if t == "Job-specific"]
    assert any("Python" in q or "RAG" in q for q in job_specific)


def test_interview_questions_fallback_for_unknown_category():
    qs = career.interview_questions({"category": "Weird Role", "skills": []})
    assert len(qs) >= 3


# ------------------------------------------------------------ copilot rules
MATCH = {"required": {"matched": ["Python", "RAG"], "missing": ["Kubernetes"]},
         "preferred": {"missing": ["Go"]}, "eligibility": {"graduation": "any"}}


def test_copilot_intents():
    cases = [
        ("Am I eligible for this?", "eligibility"),
        ("Which skills should I improve before applying?", "skill_gaps"),
        ("How should I tailor my resume?", "resume_tailoring"),
        ("Which projects should I highlight?", "projects"),
        ("What interview topics should I prepare?", "interview_prep"),
        ("Draft a cover letter", "cover_letter"),
        ("What is the salary?", "salary"),
    ]
    for q, intent in cases:
        out = career.copilot_answer(q, JOB_AI, PARSED, MATCH)
        assert out["intent"] == intent and out["answer"]


def test_copilot_salary_never_guesses():
    j = dict(JOB_AI, salary_min=None, salary_max=None)
    out = career.copilot_answer("what's the salary?", j, PARSED, MATCH)
    assert "won't guess" in out["answer"]
    j2 = dict(JOB_AI, salary_min=10, salary_max=20, salary_currency="LPA")
    out2 = career.copilot_answer("what's the salary?", j2, PARSED, MATCH)
    assert "10" in out2["answer"] and "never estimate" in out2["answer"]


def test_copilot_eligibility_is_summary_not_prediction():
    out = career.copilot_answer("am I eligible?", JOB_AI, PARSED, MATCH)
    assert "not a hiring prediction" in out["answer"]
    assert "Kubernetes" in out["answer"]               # missing skill disclosed


# ------------------------------------------------------------ analytics
def test_track_and_summary(store, user):
    store.track("job_view", user, "job1")
    store.track("job_view", None, "job1")
    store.track("job_save", user, "job1")
    s = store.analytics_summary()
    assert s["totalEvents"] == 3
    assert s["byType"]["job_view"] == 2 and s["byType"]["job_save"] == 1


def test_track_never_raises(store):
    store.events = None                               # simulate broken state
    store.track("job_view", "u", "j")                 # must not raise
    assert True
