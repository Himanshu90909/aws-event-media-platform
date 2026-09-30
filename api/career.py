"""Career intelligence engine — deterministic, explainable, no fabrication.

Design rules:
- parse_resume() is a best-effort keyword/structure extractor over plain text.
  It never invents experience; everything it returns is flagged as derived
  from the uploaded text and is user-correctable.
- match_job() scores a parsed profile against a job's *documented* fields only.
  The breakdown exposes every contributing factor (evidence), what matched,
  what is missing, and the residual uncertainty. No black-box number.
- roadmap_for() computes skill gaps against a target-role skill set and pairs
  each missing skill with curated, real, free learning resources. It does not
  promise eligibility or outcomes.
"""

from __future__ import annotations

import json
import re

# --------------------------------------------------------------------------
# Skill taxonomy: canonical skill -> alias list. Matched case-insensitively
# on word boundaries against resume text and job skill fields.
# --------------------------------------------------------------------------
SKILL_TAXONOMY = {
    "Python": ["python", "py3"],
    "Java": ["java", "core java"],
    "JavaScript": ["javascript", "js", "es6"],
    "TypeScript": ["typescript", "ts"],
    "C++": ["c++", "cpp"],
    "C": ["c language", "c programming"],
    "C#": ["c#", "csharp", "c sharp"],
    "Go": ["golang", "go lang"],
    "Rust": ["rust"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "SQL": ["sql", "mysql", "postgresql", "postgres", "sqlite", "t-sql"],
    "NoSQL": ["nosql", "mongodb", "dynamodb", "cassandra", "redis"],
    "React": ["react", "react.js", "reactjs"],
    "Next.js": ["next.js", "nextjs"],
    "Vue": ["vue", "vue.js", "vuejs"],
    "Angular": ["angular", "angularjs"],
    "Node.js": ["node", "node.js", "nodejs"],
    "Express": ["express", "express.js", "expressjs"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi", "fast api"],
    "Spring Boot": ["spring boot", "spring"],
    "REST APIs": ["rest api", "rest apis", "restful"],
    "GraphQL": ["graphql"],
    "HTML": ["html", "html5"],
    "CSS": ["css", "css3", "flexbox"],
    "Tailwind CSS": ["tailwind", "tailwindcss"],
    "Machine Learning": ["machine learning", "ml", "scikit-learn", "sklearn"],
    "Deep Learning": ["deep learning", "neural networks", "pytorch", "tensorflow", "keras"],
    "NLP": ["nlp", "natural language processing", "spacy", "transformers", "huggingface"],
    "Computer Vision": ["computer vision", "opencv", "cnn", "image processing"],
    "LLMs": ["llm", "llms", "large language models", "gpt", "prompt engineering", "rag",
             "retrieval augmented generation"],
    "Multi-agent Systems": ["crewai", "langchain", "langgraph", "autogen", "multi-agent",
                            "ai agents", "agent systems"],
    "Data Analysis": ["data analysis", "data analytics", "pandas", "numpy", "eda"],
    "Data Visualization": ["data visualization", "matplotlib", "seaborn", "plotly", "tableau"],
    "Statistics": ["statistics", "statistical", "hypothesis testing", "a/b testing"],
    "Spark": ["spark", "pyspark", "apache spark"],
    "Kafka": ["kafka", "apache kafka"],
    "Airflow": ["airflow", "apache airflow"],
    "ETL": ["etl", "elt", "data pipelines", "data pipeline"],
    "Docker": ["docker", "containers", "containerization"],
    "Kubernetes": ["kubernetes", "k8s", "eks"],
    "AWS": ["aws", "amazon web services", "ec2", "s3", "lambda", "sagemaker"],
    "Azure": ["azure"],
    "GCP": ["gcp", "google cloud"],
    "CI/CD": ["ci/cd", "cicd", "continuous integration", "continuous deployment",
              "jenkins", "github actions"],
    "Git": ["git", "github", "version control"],
    "Linux": ["linux", "unix", "bash", "shell scripting"],
    "System Design": ["system design", "distributed systems", "scalability",
                      "microservices", "high availability"],
    "Data Structures & Algorithms": ["dsa", "data structures", "algorithms",
                                     "problem solving", "competitive programming"],
    "Testing": ["unit testing", "pytest", "jest", "test automation", "qa", "selenium"],
    "Security": ["security", "cybersecurity", "owasp", "penetration testing", "auth"],
    "MongoDB": ["mongodb"],
    "PostgreSQL": ["postgresql", "postgres"],
    "Redis": ["redis"],
    "Elasticsearch": ["elasticsearch", "opensearch"],
    "MLOps": ["mlops", "model deployment", "mlflow", "model monitoring", "sagemaker"],
    "Android": ["android", "android development"],
    "iOS": ["ios development", "ios"],
    "React Native": ["react native"],
    "Flutter": ["flutter", "dart"],
    "Figma": ["figma", "ui design", "ux design", "wireframing"],
    "Product Management": ["product management", "roadmapping", "user stories", "agile",
                           "scrum", "jira"],
    "Technical Writing": ["technical writing", "documentation", "blogging"],
    "Excel": ["excel", "spreadsheet", "vlookup", "pivot tables"],
    "Power BI": ["power bi", "powerbi"],
    "Hadoop": ["hadoop", "hive", "mapreduce"],
    "Web Scraping": ["web scraping", "beautifulsoup", "scrapy", "selenium"],
    "Generative AI": ["generative ai", "genai", "diffusion", "stable diffusion",
                      "image generation"],
    "Fine-tuning": ["fine-tuning", "finetuning", "lora", "peft", "quantization"],
    "RAG": ["rag", "vector database", "vector db", "pinecone", "chroma", "embeddings",
            "semantic search"],
    "n8n": ["n8n", "workflow automation"],
    "Zapier": ["zapier"],
    "Salesforce": ["salesforce", "apex", "lwc"],
    "SAP": ["sap abap", "sap"],
    "Networking": ["networking", "tcp/ip", "dns", "routing"],
    "Embedded C": ["embedded c", "firmware", "microcontroller", "arduino", "rtos"],
    "IoT": ["iot", "internet of things", "mqtt", "sensors"],
    "AR/VR": ["ar/vr", "unity", "unreal", "augmented reality", "virtual reality"],
    "Blockchain": ["blockchain", "solidity", "web3", "smart contracts"],
    "Ruby": ["ruby", "rails", "ruby on rails"],
    "PHP": ["php", "laravel"],
    "Scala": ["scala"],
    "Bash": ["bash", "shell", "zsh"],
}

_ALIAS_TO_SKILL = {}
for _skill, _aliases in SKILL_TAXONOMY.items():
    _ALIAS_TO_SKILL[_skill.lower()] = _skill
    for _a in _aliases:
        _ALIAS_TO_SKILL[_a.lower()] = _skill


def extract_skills(text: str) -> list[str]:
    """Skills actually mentioned in the text (canonical names, sorted)."""
    found = set()
    low = f" {text.lower()} "
    for skill, aliases in SKILL_TAXONOMY.items():
        candidates = [skill.lower()] + [a.lower() for a in aliases]
        for a in candidates:
            if re.search(r"(?<![a-z0-9+#.])" + re.escape(a) + r"(?![a-z0-9+#])", low):
                found.add(skill)
                break
    return sorted(found)


# --------------------------------------------------------------------------
# Resume parsing (best-effort, deterministic)
# --------------------------------------------------------------------------
_DEGREES = {
    "PhD": ["phd", "ph.d", "doctorate", "doctoral"],
    "M.Tech": ["m.tech", "mtech", "m. tech", "master of technology"],
    "MCA": ["mca", "master of computer applications"],
    "MSc": ["m.sc", "msc", "master of science", "ms "],
    "MBA": ["mba", "master of business administration"],
    "M.Tech/Masters": ["master's", "masters degree", "postgraduate"],
    "B.Tech": ["b.tech", "btech", "b. tech", "bachelor of technology", "b.e.",
               "be degree", "bachelor of engineering"],
    "BCA": ["bca", "bachelor of computer applications"],
    "BSc": ["b.sc", "bsc", "bachelor of science", "bs "],
    "B.Tech/Bachelors": ["bachelor's", "bachelors degree", "undergraduate degree"],
    "Diploma": ["diploma"],
}

_GRAD_YEAR_RE = re.compile(
    r"(?:graduat(?:ing|ed|ion)|class of|expected|passing\s+out|complete[ds]?)\D{0,20}"
    r"(19|20)(\d{2})", re.I)
_YEAR_SPAN_RE = re.compile(r"(20[0-2]\d)\s*[-–to]{1,4}\s*((?:20[0-2]\d)|present|current)", re.I)
_SECTION_PROJECTS = re.compile(r"(?:^|\n)\s*(?:projects?|personal projects?|portfolio)\s*[:\n]", re.I)
_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:[-•*]|\d+[.)])\s+(.+)")

DEFAULT_EXPERIENCE_LEVELS = ["internship", "entry", "junior", "mid", "senior", "lead"]


def _highest_degree(text: str) -> str | None:
    low = text.lower()
    best = None
    for level, names in _DEGREES.items():
        for n in names:
            if n in low:
                best = level
                break
        if best:
            break
    return best


def _estimate_experience_years(text: str) -> float:
    """Rough estimate from explicit year spans / 'N years of experience' lines."""
    m = re.search(r"(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:professional\s*)?"
                  r"(?:work\s*)?experience", text, re.I)
    if m:
        return min(float(m.group(1)), 40.0)
    total = 0.0
    now_year = 2026
    for m in _YEAR_SPAN_RE.finditer(text):
        s = int(m.group(1))
        e = m.group(2).lower()
        ey = now_year if e in ("present", "current") else int(m.group(2))
        if 1990 <= s <= now_year and s <= ey <= now_year:
            total = max(total, ey - s)
    return round(total, 1)


def _graduation_year(text: str) -> int | None:
    m = _GRAD_YEAR_RE.search(text)
    if m:
        y = int(m.group(1) + m.group(2))
        if 1990 <= y <= 2035:
            return y
    m = re.search(r"(?:20[0-2]\d)\s*[-–]\s*(20[0-2]\d)", text)
    if m:  # education date range "2022 – 2026"
        y = int(m.group(2))
        if 2020 <= y <= 2035:
            return y
    return None


def _extract_projects(text: str) -> list[str]:
    """Project bullets from an explicit Projects section (cap 12, 200 chars each)."""
    m = _SECTION_PROJECTS.search(text)
    if not m:
        return []
    tail = text[m.end():]
    bullets = []
    for b in _BULLET_RE.finditer(tail):
        t = b.group(1).strip()
        if t and len(t) >= 8:
            bullets.append(t[:200])
        if len(bullets) >= 12:
            break
    return bullets


def parse_resume(text: str) -> dict:
    """Deterministic best-effort extraction. Everything here is user-correctable
    downstream; nothing is inferred beyond the literal resume text."""
    text = (text or "").strip()
    if not text:
        return {"skills": [], "education": None, "graduation_year": None,
                "experience_years": 0.0, "projects": [], "extraction_note":
                "No text found in resume."}
    return {
        "skills": extract_skills(text),
        "education": _highest_degree(text),
        "graduation_year": _graduation_year(text),
        "experience_years": _estimate_experience_years(text),
        "projects": _extract_projects(text),
        "extraction_note": ("Extracted mechanically from your resume text. "
                            "Review and correct anything that looks wrong — "
                            "matching uses your corrected profile."),
    }


# --------------------------------------------------------------------------
# Explainable matching
# --------------------------------------------------------------------------
def _job_skills(job: dict) -> tuple[list[str], list[str]]:
    """(mandatory, preferred) skill lists for a job. Employer posts may define
    them explicitly; for aggregated jobs the first skills are treated as core
    requirements and the rest as preferred. Documented fields only."""
    req = job.get("skills_required") or []
    pref = job.get("skills_preferred") or []
    if not req and not pref:
        skills = [s for s in (job.get("skills") or []) if s]
        req = [s for s in skills if s in SKILL_TAXONOMY][:5] or skills[:5]
        pref = [s for s in skills if s not in req]
    return req, pref


def _norm_skill(s: str) -> str | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    return _ALIAS_TO_SKILL.get(s)


def _canonical(skills: list[str]) -> list[str]:
    out = []
    for s in skills:
        c = _norm_skill(s)
        if c and c not in out:
            out.append(c)
        elif not c and s and s not in out:
            out.append(s.title() if s.islower() else s)
    return out


def _eligibility(parsed: dict, job: dict) -> dict:
    checks = []
    level = (job.get("experience_level") or "").strip().lower()
    if level:
        years = float(parsed.get("experience_years") or 0)
        if level in ("internship", "entry", "fresher"):
            ok = years >= 0
            note = "Open to candidates starting out"
        elif level in ("junior", "associate"):
            ok = years >= 0.5
            note = f"Estimated experience {years}g (junior usually ~1+)"
        elif level in ("mid", "mid-level"):
            ok = years >= 2
            note = f"Estimated experience {years}y (mid usually ~2-5y)"
        elif level in ("senior", "lead", "staff", "principal"):
            ok = years >= 5
            note = f"Estimated experience {years}y (senior usually 5y+)"
        else:
            ok = True
            note = "Level not documented; not checked"
        checks.append({"check": "experience_level", "passed": bool(ok),
                       "evidence": f"{level or 'n/a'}: {note}"})
    exp = (job.get("expires_at") or 0)
    if exp and exp < __import__("time").time():
        checks.append({"check": "deadline", "passed": False,
                       "evidence": "Listing has expired"})
    return {"checks": checks, "passed": all(c["passed"] for c in checks)}


def match_job(parsed: dict, job: dict) -> dict:
    """Explainable match of a (user-corrected) parsed profile to a job.

    Score is transparent: 60% mandatory skill coverage + 25% preferred
    coverage + 15% eligibility checks. The breakdown carries the evidence.
    """
    user_skills = _canonical(parsed.get("skills") or [])
    user_set = {s.lower() for s in user_skills}
    req_raw, pref_raw = _job_skills(job)
    req, pref = _canonical(req_raw), _canonical(pref_raw)

    def hit(skill):
        return skill.lower() in user_set

    matched_req = [s for s in req if hit(s)]
    missing_req = [s for s in req if not hit(s)]
    matched_pref = [s for s in pref if hit(s)]
    missing_pref = [s for s in pref if not hit(s)]

    elig = _eligibility(parsed, job)
    req_cov = (len(matched_req) / len(req)) if req else None
    pref_cov = (len(matched_pref) / len(pref)) if pref else None

    score = 0.0
    if req_cov is not None:
        score += 0.60 * req_cov
    elif pref_cov is not None:
        score += 0.60 * pref_cov
    if pref_cov is not None:
        score += 0.25 * pref_cov
    score += 0.15 if elig["passed"] else 0.0
    score = round(min(score, 1.0), 3)

    evidence = []
    if matched_req:
        evidence.append(f"Your profile lists the required skills: {', '.join(matched_req)}")
    if missing_req:
        evidence.append(f"Job lists requirements not found in your profile: "
                        f"{', '.join(missing_req)}")
    if matched_pref:
        evidence.append(f"You also match preferred skills: {', '.join(matched_pref)}")
    for c in elig["checks"]:
        evidence.append(c["evidence"])

    if not parsed.get("skills"):
        uncertainty = ("No resume on file — this is a job-requirements summary only. "
                       "Upload a resume for a personalized match.")
    else:
        uncertainty = ("Based on your resume text and any corrections you made. "
                       "A match score is not an application decision; employers "
                       "review full applications themselves.")
    return {
        "score": score,
        "required": {"matched": matched_req, "missing": missing_req},
        "preferred": {"matched": matched_pref, "missing": missing_pref},
        "eligibility": elig,
        "evidence": evidence,
        "uncertainty": uncertainty,
        "method": "Deterministic coverage score: 60% mandatory + 25% preferred "
                  "+ 15% eligibility. Explainable, no hidden model.",
    }


# --------------------------------------------------------------------------
# Career roadmap
# --------------------------------------------------------------------------
ROLE_LIBRARY = {
    "AI Engineer": ["Python", "LLMs", "RAG", "Multi-agent Systems", "REST APIs",
                    "Docker", "Machine Learning", "System Design"],
    "Machine Learning Engineer": ["Python", "Machine Learning", "Deep Learning",
                                  "MLOps", "Docker", "SQL", "Statistics"],
    "Data Scientist": ["Python", "Data Analysis", "Statistics", "SQL",
                       "Machine Learning", "Data Visualization"],
    "Data Engineer": ["Python", "SQL", "ETL", "Spark", "Kafka", "AWS", "Docker"],
    "Backend Engineer": ["Node.js", "Python", "REST APIs", "SQL", "Docker",
                        "System Design", "Data Structures & Algorithms"],
    "Frontend Engineer": ["JavaScript", "React", "HTML", "CSS", "TypeScript"],
    "Full-Stack Engineer": ["JavaScript", "React", "Node.js", "REST APIs", "SQL",
                            "TypeScript", "Docker"],
    "Mobile Engineer": ["Android", "iOS", "React Native", "Flutter"],
    "DevOps Engineer": ["Linux", "Docker", "Kubernetes", "CI/CD", "AWS", "Bash"],
    "SDE (Software Development Engineer)": ["Data Structures & Algorithms",
                                            "System Design", "Java", "SQL",
                                            "Git", "REST APIs"],
    "Security Engineer": ["Security", "Linux", "Networking", "Python", "Docker"],
    "Product Manager": ["Product Management", "Data Analysis", "SQL",
                        "Technical Writing", "Figma"],
}

SKILL_RESOURCES = {
    "Python": [("Official tutorial (docs.python.org)", "https://docs.python.org/3/tutorial/")],
    "SQL": [("SQLBolt interactive lessons", "https://sqlbolt.com/")],
    "Data Structures & Algorithms": [("NeetCode roadmap", "https://neetcode.io/roadmap"),
                                     ("LeetCode top interview 150", "https://leetcode.com/studyplan/top-interview-150/")],
    "System Design": [("System Design Primer (GitHub)", "https://github.com/donnemartin/system-design-primer")],
    "Machine Learning": [("Google ML crash course", "https://developers.google.com/machine-learning/crash-course"),
                         ("scikit-learn tutorials", "https://scikit-learn.org/stable/tutorial/index.html")],
    "Deep Learning": [("fast.ai course", "https://course.fast.ai/"),
                      ("PyTorch tutorials", "https://pytorch.org/tutorials/")],
    "LLMs": [("Hugging Face LLM course", "https://huggingface.co/learn/llm-course")],
    "RAG": [("LangChain RAG guide", "https://python.langchain.com/docs/tutorials/rag/")],
    "Docker": [("Docker getting started", "https://docs.docker.com/get-started/")],
    "Kubernetes": [("Kubernetes basics (k8s.io)", "https://kubernetes.io/docs/tutorials/kubernetes-basics/")],
    "AWS": [("AWS Skill Builder free tier", "https://skillbuilder.aws/")],
    "JavaScript": [("MDN JavaScript guide", "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide")],
    "React": [("react.dev learn", "https://react.dev/learn")],
    "Node.js": [("Node.js official guides", "https://nodejs.org/en/learn")],
    "Git": [("Pro Git book", "https://git-scm.com/book/en/v2")],
    "Linux": [("Linux Journey", "https://linuxjourney.com/")],
    "REST APIs": [("RESTful API design guide", "https://restfulapi.net/")],
    "Spark": [("Spark quickstart", "https://spark.apache.org/docs/latest/quick-start.html")],
    "Kafka": [("Kafka official quickstart", "https://kafka.apache.org/quickstart")],
    "ETL": [("Airflow tutorial", "https://airflow.apache.org/docs/apache-airflow/stable/tutorial/index.html")],
    "MLOps": [("MLflow tutorials", "https://mlflow.org/docs/latest/getting-started/")],
    "Multi-agent Systems": [("CrewAI docs", "https://docs.crewai.com/")],
    "Statistics": [("Khan Academy statistics", "https://www.khanacademy.org/math/statistics-probability")],
    "Data Analysis": [("pandas getting started", "https://pandas.pydata.org/docs/getting_started/index.html")],
    "Data Visualization": [("Matplotlib tutorials", "https://matplotlib.org/stable/tutorials/index.html")],
    "Product Management": [("Atlassian agile coaching", "https://www.atlassian.com/agile")],
    "TypeScript": [("TypeScript handbook", "https://www.typescriptlang.org/docs/handbook/intro.html")],
    "Testing": [("pytest getting started", "https://docs.pytest.org/en/stable/getting-started.html")],
    "CSS": [("MDN CSS learn", "https://developer.mozilla.org/en-US/docs/Learn/CSS")],
    "HTML": [("MDN HTML learn", "https://developer.mozilla.org/en-US/docs/Learn/HTML")],
    "Figma": [("Figma learn (free)", "https://help.figma.com/hc/en-us/categories/360002051613")],
}

_GENERIC_RESOURCE = ("Search the skill's official documentation first; pick one "
                     "free course and one small portfolio project.", None)


def roadmap_for(parsed: dict, role: str) -> dict:
    """Skill-gap report + learning plan for a target role. Honest: 'learning a
    skill does not guarantee eligibility' is stated up front."""
    role = role.strip()
    if role not in ROLE_LIBRARY:
        return {"error": "UNKNOWN_ROLE", "roles": sorted(ROLE_LIBRARY)}
    required = ROLE_LIBRARY[role]
    user_set = {s.lower() for s in _canonical(parsed.get("skills") or [])}
    have, gap = [], []
    for s in required:
        (have if s.lower() in user_set else gap).append(s)
    plan = []
    for s in gap:
        res = SKILL_RESOURCES.get(s) or [_GENERIC_RESOURCE]
        plan.append({
            "skill": s,
            "status": "todo",        # todo | in_progress | done (user-controlled)
            "suggested_project": f"Build a small portfolio project that uses {s} "
                                 f"and write a short README about what you built.",
            "resources": [{"title": t, "url": u} for t, u in res if u],
        })
    return {
        "role": role,
        "skills_have": have,
        "skills_gap": plan,
        "coverage": round(len(have) / len(required), 3) if required else 1.0,
        "note": "A roadmap is a study guide, not a promise. Employers make their "
                "own eligibility decisions; always read the original posting.",
        "interview_prep": [
            "Review the role's fundamentals and practice explaining them aloud.",
            "Do 2-3 mock interviews with a friend or record yourself.",
            "Prepare 2 stories for each past project: what you built and what broke.",
            f"Re-read live {role} postings here and note repeated requirements.",
        ],
    }


# --------------------------------------------------------------------------
# Opportunity hygiene helpers (duplicates, expiry, fraud signals)
# --------------------------------------------------------------------------
_FRAUD_SIGNALS = [
    (r"registration\s+fee|processing\s+fee|pay\s+.{0,20}(?:fee|deposit)", "upfront fee"),
    (r"whats\s?app\s+.{0,30}(?:hire|hiring|join)", "whatsapp-only hiring"),
    (r"guaranteed\s+(?:job|placement|income)", "guaranteed outcome"),
    (r"(?:salary|earn)\s+(?:of\s+)?(?:₹|rs\.?|inr)?\s*\d+\s*(?:l|lakh)",
     "salary without verifiable source"),  # heuristic only when unverified
    (r"telegram\s+(?:channel|group)", "telegram-only contact"),
]

_EXPIRED_GRACE_DAYS = 3


def fraud_signals(description: str) -> list[str]:
    """Heuristic caution flags. These never auto-remove or label a listing as
    fraudulent — they only add 'review' markers for humans to check."""
    if not description:
        return []
    hits = []
    for pat, label in _FRAUD_SIGNALS:
        if re.search(pat, description, re.I):
            hits.append(label)
    return hits


def is_stale(job: dict, now: float) -> bool:
    """Expired more than the grace window ago → stale/hidden."""
    exp = job.get("expires_at")
    return bool(exp and exp + _EXPIRED_GRACE_DAYS * 86400 < now)


def duplicate_key(source: str, title: str, company: str) -> str:
    """Canonical dedup key: normalized source+title+company."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(source)}|{norm(title)}|{norm(company)}"


def normalize_job(raw: dict, source: str) -> dict:
    """Normalize an external opportunity payload into the jobs row shape.
    Missing values stay None/empty — never invented."""
    def _s(v):
        v = (v or "").strip()
        return v or None

    def _num(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    title = _s(raw.get("title"))
    company = _s(raw.get("company"))
    if not title or not company:
        raise ValueError("title and company are required")
    skills = [s.strip() for s in (raw.get("skills") or []) if s.strip()]
    return {
        "source": source,
        "external_id": _s(raw.get("external_id")) or duplicate_key(source, title, company),
        "title": title,
        "company": company,
        "company_logo_url": _s(raw.get("company_logo_url")),
        "description": _s(raw.get("description")) or "",
        "apply_url": _s(raw.get("apply_url")),
        "employment_type": _s(raw.get("employment_type")),
        "experience_level": _s(raw.get("experience_level")),
        "location": _s(raw.get("location")),
        "remote_type": _s(raw.get("remote_type")),
        "salary_min": _num(raw.get("salary_min")),
        "salary_max": _num(raw.get("salary_max")),
        "salary_currency": _s(raw.get("salary_currency")) or "INR",
        "category": _s(raw.get("category")) or "Jobs",
        "type": _s(raw.get("type")),
        "skills": skills,
        "skills_required": [s.strip() for s in (raw.get("skills_required") or []) if s.strip()],
        "skills_preferred": [s.strip() for s in (raw.get("skills_preferred") or []) if s.strip()],
        "posted_at": raw.get("posted_at"),
        "expires_at": raw.get("expires_at"),
        "is_verified": bool(raw.get("is_verified")),
    }


# =============================================================================
# Job-detail intelligence: description sections, prep workspace, copilot rules
# =============================================================================

SECTION_HEADINGS = [
    ("about",          ["about", "overview", "the role", "role summary", "about the role", "job description"]),
    ("responsibilities", ["responsibilities", "what you'll do", "what you will do", "your impact", "key responsibilities", "duties"]),
    ("qualifications", ["required qualifications", "requirements", "qualifications", "who you are", "must have", "minimum qualifications", "you have", "what you need"]),
    ("preferred",      ["preferred qualifications", "preferred", "nice to have", "bonus", "good to have", "plus"]),
    ("benefits",      ["benefits", "perks", "what we offer", "why join", "we offer"]),
    ("instructions",  ["how to apply", "application instructions", "apply now", "to apply", "application process", "apply instructions"]),
]


def split_description(desc: str) -> dict:
    """Deterministically split a description blob into labeled sections.
    Only reorganizes text that is actually present — never invents content."""
    out = {key: [] for key, _ in SECTION_HEADINGS}
    out["about"] = []
    lines = [l.strip() for l in (desc or "").splitlines()]
    current = "about"
    for line in lines:
        if not line:
            continue
        low = line.lower().strip()
        matched = None
        if len(line) < 70 and (low.endswith(":") or low == line.lower()):
            for key, heads in SECTION_HEADINGS:
                if low.rstrip(":") in heads:
                    matched = key
                    break
        if matched:
            current = matched
            continue
        out[current].append(line)
    # text before any heading stays in 'about'
    if not any(out.values()):
        out["about"] = lines
    return {k: "\n".join(v) for k, v in out.items() if v}


# ---------------------------------------------------------------------------
# Application preparation workspace (per job)
# ---------------------------------------------------------------------------

INTERVIEW_BANK = {
    "SDE / SDE I / SDE II / SDE III": [
        ("DSA", "Solve: detect a cycle in a linked list, and explain the O(1) space approach."),
        ("DSA", "Solve: longest substring without repeating characters. Discuss sliding-window tradeoffs."),
        ("Programming", "Explain memory management / garbage collection in your primary language."),
        ("System design", "Design a URL shortener with analytics. Justify your storage choice."),
        ("System design", "Design a rate limiter for a public API."),
        ("Behavioral", "Tell me about a project that failed and what you changed afterwards."),
    ],
    "Backend Engineer": [
        ("DSA", "Solve: merge overlapping intervals."),
        ("System design", "Design an idempotent payment API with retries."),
        ("Backend", "How would you handle N+1 query problems? Give a real example."),
        ("Backend", "Explain indexes, and when an index hurts performance."),
        ("Behavioral", "Describe a production bug you diagnosed end-to-end."),
    ],
    "Frontend Engineer": [
        ("Frontend", "Explain the browser render pipeline and how you prevent layout thrash."),
        ("Frontend", "Debounce vs throttle: implement one and pick a use case."),
        ("Frontend", "How do you make a complex form accessible and keyboard-navigable?"),
        ("Performance", "A page loads slowly. Walk through your diagnosis steps."),
        ("Behavioral", "Show a UI you built and one tradeoff you would revisit."),
    ],
    "AI Engineer": [
        ("RAG", "Design a RAG pipeline for a 100k-document corpus. How do you evaluate it?"),
        ("RAG", "Chunking strategies: what would you pick for tabular data vs prose?"),
        ("LLMs", "Explain hallucination, and three mitigations you have actually used."),
        ("Agents", "When do you NOT use a multi-agent design?"),
        ("ML fundamentals", "Overfitting: how do you detect and fix it?"),
        ("Behavioral", "Describe an AI feature you shipped and how you measured quality."),
    ],
    "Machine Learning Engineer": [
        ("ML fundamentals", "Bias–variance tradeoff with a concrete example."),
        ("ML fundamentals", "How would you build a baseline before any deep model?"),
        ("Data", "Your training data is imbalanced. What are your options, honestly ranked?"),
        ("Deployment", "How do you monitor model drift in production?"),
        ("Behavioral", "Walk me through a model you trained end-to-end."),
    ],
    "Data Scientist": [
        ("Statistics", "Explain p-values to a product manager in two sentences."),
        ("Statistics", "When does correlation genuinely mislead?"),
        ("SQL", "Write a query: monthly retention cohort of active users."),
        ("Data", "How do you handle missing data without inventing it?"),
        ("Behavioral", "Tell me about an analysis that changed a decision."),
    ],
    "Data Analyst": [
        ("SQL", "Window functions: compute a 7-day rolling average."),
        ("SQL", "Find duplicate rows and explain your dedup criteria."),
        ("Statistics", "A/B test: what can go wrong with peeking?"),
        ("Visualization", "How do you pick chart types honestly?"),
        ("Behavioral", "Describe a dashboard you built and who used it."),
    ],
    "Product Manager": [
        ("Product", "How would you prioritize 20 feature requests with 2 engineers?"),
        ("Product", "Define success metrics for a job-matching feature."),
        ("Product", "How would you run a customer interview without leading questions?"),
        ("Behavioral", "Tell me about a launch that flopped and what you learned."),
    ],
}
DEFAULT_QUESTIONS = [
    ("Fundamentals", "Pick one required skill from this listing and explain it to a beginner."),
    ("Fundamentals", "Explain a project on your resume and the hardest bug in it."),
    ("Behavioral", "Why this company and this role, specifically?"),
]


def interview_questions(job) -> list:
    """Role-appropriate PRACTICE questions. These are generated study aids —
    they are NOT actual employer interview questions."""
    cat = (job.get("category") or "").strip()
    qs = list(INTERVIEW_BANK.get(cat, DEFAULT_QUESTIONS))
    for s in (job.get("skills_required") or job.get("skills") or [])[:4]:
        qs.append(("Job-specific", f"Be ready to discuss '{s}' in depth: a project you used it in, "
                                   f"a mistake you made with it, and how you'd verify your work."))
    return qs


def prep_workspace(parsed, job, user) -> dict:
    """Deterministic, honest application-prep material built ONLY from the
    actual job record and the user's (corrected) parsed resume."""
    req = (job.get("skills_required") or job.get("skills") or [])
    pref = (job.get("skills_preferred") or [])
    seen = {s.lower() for s in req}
    jskills = req + [s for s in pref if s.lower() not in seen]
    matched = [s for s in jskills if s.lower() in {x.lower() for x in (parsed.get("skills") or [])}]
    projects = parsed.get("projects") or []

    tips = []
    if matched:
        tips.append(f"Highlight these skills you already have: {', '.join(matched[:6])}. "
                    "Point at the specific project where you used each one.")
    missing = [s for s in jskills if s.lower() not in {x.lower() for x in (parsed.get("skills") or [])}]
    if missing:
        tips.append(f"Missing keywords from the listing (required + preferred): {', '.join(missing[:6])}. Only add them "
                    "if you genuinely have the experience — never keyword-stuff. If a project "
                    "used one, name the project explicitly.")
    if projects:
        tips.append("Lead with the project most similar to this role, and quantify what you "
                    "actually measured (users, accuracy, latency). No invented metrics.")
    if parsed.get("graduation_year"):
        tips.append(f"Your profile says graduation {parsed['graduation_year']} — check the "
                    "listing's eligibility window before applying.")

    # Cover letter draft: only real, attributable facts
    name = (user or {}).get("display_name") or "there"
    role, company = job.get("title") or "this role", job.get("company") or "your company"
    skill_phrase = ", ".join(matched[:3]) if matched else "the core skills in your posting"
    proj = projects[0].split(":")[0].strip() if projects else None
    proj_sentence = (f"Most relevant to this role, I built {proj} — I can walk through the "
                     "decisions and tradeoffs." if proj else
                     "I would be glad to walk through my project work in detail.")
    grad = (f"I am completing my {parsed.get('education') or 'degree'} in {parsed['graduation_year']}."
            if parsed.get("graduation_year") else
            "My education details are in my resume.")
    letter = (
        f"Dear {company} hiring team,\n\n"
        f"I am applying for the {role} position. Reading your posting, the emphasis on "
        f"{skill_phrase} matches what I have been building.\n\n"
        f"{proj_sentence}\n\n"
        f"{grad}\n"
        f"Here is what I know about {company}: only what is in your own posting — I have not "
        f"invented details. If the role is still open, I would welcome the chance to discuss "
        f"how my work maps to your requirements.\n\n"
        f"Thank you for your time,\n{name}\n\n"
        f"(Draft generated from your verified resume fields — edit freely, keep it truthful.)"
    )
    return {
        "resumeTips": tips or ["Add your resume in Resume AI first — tips are generated "
                               "from your actual profile."],
        "coverLetterDraft": letter,
        "interviewQuestions": interview_questions(job),
        "checklist": [
            "Read the full official listing at the source link",
            "Confirm eligibility (graduation year, experience, location)",
            "Tailor resume bullets to the required skills above",
            "Draft your cover letter, then edit it",
            "Apply on the official employer URL",
            "Record the application date in the tracker below",
        ],
        "disclaimer": "All material is generated deterministically from the listing and your "
                      "resume. Practice questions are study aids, NOT this employer's actual "
                      "questions. Nothing here is a prediction of hiring outcomes.",
    }


# ---------------------------------------------------------------------------
# Career copilot — deterministic fallback answers (LLM layer sits on top)
# ---------------------------------------------------------------------------

def copilot_answer(question, job, parsed, match) -> dict:
    """Intent-routed answers using ONLY the actual job + parsed resume + match.
    Used as the guaranteed-honest fallback when no LLM is available."""
    q = (question or "").lower()
    matched = match.get("required", {}).get("matched", [])
    missing_req = match.get("required", {}).get("missing", [])
    missing_pref = match.get("preferred", {}).get("missing", [])
    elig = match.get("eligibility", {})
    projects = parsed.get("projects") or []

    def s(list_):
        return ", ".join(list_) if list_ else "none listed"

    if any(w in q for w in ("eligib", "qualif", "can i apply", "am i")):
        return {"answer":
            f"Based only on the posted requirements: you match {len(matched)} of the listed "
            f"required skills ({s(matched)}); missing required skills: {s(missing_req)}. "
            f"Eligibility flags from the listing itself: {json.dumps(elig)}. "
            "This is a requirements summary, not a hiring prediction — the only way to know "
            "is the official listing and the employer's own screening. Verify deadlines and "
            "eligibility rules on the source page before applying.",
            "intent": "eligibility"}
    if any(w in q for w in ("improve", "learn", "skill", "gap")):
        return {"answer":
            f"Skill gaps for this specific role: required — {s(missing_req)}; preferred — "
            f"{s(missing_pref)}. Prioritize required skills first. You can generate a full "
            "learning plan with free resources in the Roadmap view for this role family.",
            "intent": "skill_gaps"}
    if any(w in q for w in ("tailor", "resume", "optimize", "keywords")):
        tips = prep_workspace(parsed, job, {"display_name": ""})["resumeTips"]
        return {"answer": " ".join(tips) +
                " Regenerate the full workspace (cover letter, tips, checklist) in the "
                "Preparation tab of this page.",
            "intent": "resume_tailoring"}
    if "project" in q or "highlight" in q:
        proj = "\n".join(f"· {p}" for p in projects[:4]) or "No projects found in your resume yet."
        return {"answer":
            f"From your parsed resume, the projects on file are:\n{proj}\n"
            f"Prioritize the one that overlaps most with: {s((job.get('skills_required') or job.get('skills') or [])[:5])}. "
            "Be ready to explain tradeoffs and what you would redo.",
            "intent": "projects"}
    if any(w in q for w in ("interview", "prepare", "topic", "question")):
        qs = interview_questions(job)[:5]
        listed = "\n".join(f"· [{t}] {q}" for t, q in qs)
        return {"answer":
            f"Practice topics generated from this listing's requirements:\n{listed}\n"
            "These are study aids based on the job's own skill list — NOT this employer's "
            "actual interview questions. Track your prep progress in the Roadmap view.",
            "intent": "interview_prep"}
    if "cover letter" in q or "letter" in q:
        return {"answer":
            "The Preparation tab generates a cover-letter draft using only your verified "
            "resume fields and this listing's posted requirements — open it, edit it, and "
            "keep every claim true. I never invent work history or metrics.",
            "intent": "cover_letter"}
    if "roadmap" in q:
        return {"answer":
            "Open the Roadmap view, pick this role's target role, and you'll get a skill-gap "
            "plan with real free resources (docs, courses, practice sites) plus a progress "
            "tracker. It's a study guide, not an eligibility promise.",
            "intent": "roadmap"}
    if any(w in q for w in ("salary", "pay", "ctc")):
        lo, hi = job.get("salary_min"), job.get("salary_max")
        return {"answer":
            (f"The listing itself states {job.get('salary_currency') or ''} {lo}–{hi}. "
             "I only report what the posting states — I never estimate or predict offers."
             if lo or hi else
             "This listing does not state a salary, and I won't guess one. Check the official "
             "source or ask the employer directly."),
            "intent": "salary"}
    return {"answer":
        f"I can answer from this listing and your resume about: eligibility, skill gaps, "
        f"resume tailoring, projects to highlight, interview practice topics, cover-letter "
        f"strategy, and learning roadmaps. This role is '{job.get('title')}' at "
        f"{job.get('company')} with required skills: {s((job.get('skills_required') or job.get('skills') or [])[:6])}. "
        "Ask about any of those and I'll ground the answer in the actual data.",
        "intent": "fallback"}


def llm_copilot(question, job, parsed, match) -> str | None:
    """Optional LLM layer. Returns free text grounded in strict context, or
    None (caller falls back to copilot_answer). Never speaks for the employer."""
    import urllib.parse, urllib.request
    facts = {
        "job": {k: job.get(k) for k in ("title", "company", "location", "remote_type",
                                        "employment_type", "experience_level", "category",
                                        "description", "skills_required", "skills_preferred",
                                        "skills", "apply_url")},
        "user_parsed": parsed,
        "match_summary": match,
        "question": question[:600],
    }
    prompt = (
        "You are a career advisor inside a jobs platform. Answer the student's question using "
        "ONLY the JSON context provided. Hard rules: never invent qualifications, policies, "
        "salaries, interview questions, or hiring outcomes; clearly mark anything uncertain as "
        "uncertain; do not claim the application was submitted; be concise (under 220 words); "
        "end with one short actionable next step. Context:\n"
        + json.dumps(facts)[:6000]
    )
    try:
        url = "https://text.pollinations.ai/" + urllib.parse.quote(prompt)
        req = urllib.request.Request(url, headers={"User-Agent": "mediaflow-copilot/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            text = r.read().decode("utf-8", "replace").strip()
        return text if 20 <= len(text) <= 4000 else None
    except Exception:
        return None
