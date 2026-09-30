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
