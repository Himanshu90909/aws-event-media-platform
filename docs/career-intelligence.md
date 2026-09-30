# MediaFlow Jobs — Career Intelligence Platform

Upgrade of the MediaFlow Jobs marketplace into an AI-powered career intelligence
platform. Built incrementally on the existing codebase (single Python entrypoint
`api/index.py`, Postgres persistence, hash-router SPA at `/careers`).

## Design principles (enforced in code)

1. **Never fabricate.** No invented jobs, salaries, endorsements, badges, or
   match explanations. Employer posts are `unverified` until a human approves
   them; fraud checks only add caution flags for human review.
2. **Explainable AI.** The matcher is a deterministic coverage score with the
   weights published in every response. No hidden model, no mystery percentage.
3. **Consent first.** Resume processing requires an explicit consent flag; the
   consent is logged, and deleting the resume logs consent withdrawal.
4. **Honest uncertainty.** Match and roadmap responses state what they are and
   are not (not an application decision, not an eligibility promise).

## New capabilities

| Area | What it does | Where |
|---|---|---|
| Resume AI | Best-effort deterministic extraction of skills, education, graduation year, experience, projects — user-correctable | `POST/PATCH/GET/DELETE /api/mf/resume`, view `#/resume` |
| Explainable matching | 60% mandatory + 25% preferred skill coverage + 15% eligibility; evidence lines + missing skills + uncertainty | `GET /api/mf/match?job_id=`, `GET /api/mf/match/recommended`, job-detail panel |
| Career roadmap | Skill-gap report per target role with real free resources, user-tracked progress | `GET /api/mf/roadmap[?role=]`, `POST /api/mf/roadmap/progress`, view `#/roadmap` |
| Pipeline | New `ASSESSMENT` stage; notes, next actions | `PATCH /api/mf/applications/:id`, Kanban `#/applications` |
| Employer portal | Company registration (manual verification, no auto-badges), job posting, applicant management, candidate messaging | `#/employer`, `POST /api/mf/employers…` |
| Messaging | Party-authorized threads per application (candidate ↔ employer only) | `POST/GET /api/mf/messages` |
| Trust | Listing reports (human-reviewed), verification history, caution heuristics, ingestion-run transparency | `POST /api/mf/jobs/:id/report`, `GET /api/mf/jobs/:id/verification`, `GET /api/mf/ingestion` |
| Privacy | Full-account export, resume deletion, consent log | `GET /api/mf/privacy/export`, `#/profile` |
| Admin | Manual employer/job verification via `ADMIN_TOKEN` | `POST /api/mf/admin/employers/:id`, `POST /api/mf/admin/jobs/:id` |

## Data model additions

`resumes`, `consents`, `ingestion_runs`, `verification_events`, `job_reports`,
`employers`, `messages`, `learning_progress` (all idempotent `CREATE TABLE IF NOT
EXISTS` in `db/schema.py`). `jobs` gains `last_verified_at`, `skills_required`,
`skills_preferred`, `posted_by_employer`, `sponsored`.

## The matching engine (api/career.py)

- ~90-skill taxonomy with aliases; word-boundary matching.
- Mandatory vs preferred requirements: employer posts can define both; for
  aggregated jobs the documented skill list is split (first five = core).
- Score = `0.60·required_coverage + 0.25·preferred_coverage + 0.15·eligibility`,
  capped at 1.0, rounded to 3 decimals. Every response includes the method
  string, per-skill matched/missing lists, and an uncertainty note.
- Resume extraction is regex/keyword based and always includes an
  `extraction_note` inviting corrections. Corrections overwrite the parsed
  profile and matching uses the corrected values.

## Privacy & responsible AI

- Resume processing only with explicit consent; resumes are stored per-user and
  never exposed to employers or other users through any endpoint.
- Employer sees an applicant's name/email/avatar/headline only after that
  person applied to the employer's own posting.
- Reports and verification events are human-reviewed; nothing auto-removes a
  listing or stamps a badge.
- Account deletion cascades resumes, consents, messages; export returns
  everything stored about the user as JSON.

## Deployment

- Same Vercel + Neon Postgres runtime as before; migrations are idempotent and
  run at boot (`PgStore._migrate`). `ADMIN_TOKEN` env var (optional) unlocks the
  admin verification endpoints — 501 until set.
- Env vars: `DATABASE_URL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `SESSION_SECRET`, `ADMIN_TOKEN` (optional).
- Rollback: redeploy any previous commit; schema additions are additive and
  safe to leave in place.

## AWS migration path (documented, not required)

The current Vercel + Neon stack is production-adequate. If/when this moves to
AWS, map as follows (all components already have equivalents in this repo's
`template.yaml`):

| Current | AWS target | Note |
|---|---|---|
| Vercel Python function | API Gateway + Lambda | `api/index.py` is stdlib-only, drops into Lambda |
| Neon Postgres | Aurora Serverless Postgres (or DynamoDB) | `db/schema.py` carries the DDL |
| Google OAuth + HMAC sessions | Cognito user pools or keep OIDC | sessions already server-side |
| Ingestion (runtime fetch) | EventBridge Scheduler + SQS + Lambda | `ingestion_runs` table already logs runs |
| Local file metadata | S3 | resume text goes in Postgres today |
| — | SES/SNS | notifications are in-DB; email hooks later |
| — | CloudWatch alarms | start with 5xx rate + ingestion failure alarms |

## Monetization (planned, no payments implemented)

- Free forever: discovery, pipeline, matching, roadmap — the student core.
- Premium (later, disclosed clearly): advanced analytics, extra roadmap depth.
- College placement dashboards (later): aggregate, consented, privacy-reviewed.
- Recruiter subscriptions (later): employer portal advanced features.
- Sponsored opportunities: `jobs.sponsored` column exists; UI disclosure label
  required before any sponsored slot is sold — not active today.

## Upgrade 2 — premium job-detail experience (30 Sep 2026)

- **9-stage pipeline**: SAVED → PREPARING → APPLIED → ONLINE ASSESSMENT →
  SCREENING → INTERVIEW → OFFER / REJECTED / WITHDRAWN, with applied-date,
  assessment-deadline tracking, notes and an activity timeline
  (`GET /api/mf/applications/:id`, tracker card on every job page).
- **Structured job detail**: description deterministically reorganized into
  About / Responsibilities / Required / Preferred / Benefits / Instructions
  (`career.split_description`) — only reorganizes text that exists.
- **Preparation workspace** (`GET /api/mf/jobs/:id/prep`): honest resume tips,
  a truthful cover-letter draft (only verified user fields), categorized
  interview PRACTICE questions (clearly labeled as NOT the employer's
  questions), application checklist. All editable client-side.
- **Career copilot** (`POST /api/mf/jobs/:id/copilot`): answers grounded in the
  actual listing + parsed resume + match. LLM layer (strict prompt, 12s
  timeout) with a guaranteed deterministic rules fallback; every response is
  labeled and disclaimed. Salary answers never guess.
- **In-browser PDF/DOCX extraction**: pdf.js + JSZip extract resume text in
  the browser; the file never leaves the user's device — only reviewed text
  is sent, after explicit consent.
- **Analytics** (privacy-conscious, real counts only): `analytics_events`
  table tracks job views, saves, apply starts, resume analyses, prep opens,
  copilot uses. Internal admin dashboard at `#/admin` (ADMIN_TOKEN-gated) shows
  real event counts, data quality, ingestion runs. No fabricated metrics.
- **Design**: re-themed to the professional navy / white / blue palette
  (light, accessible), responsive two-column job page (content + AI sidebar)
  collapsing to a single column under 1000px.

## Remaining limitations (honest list)

1. Resume intake is paste-your-text or in-browser PDF/DOCX text extraction
   (scanned-image PDFs still need manual paste; the UI says so).
2. Aggregated sources are fetched at request time (Remotive, Arbeitnow,
   RemoteOK, MLH) with a curated static catalog as fallback; there is no
   scheduled crawler yet — the AWS EventBridge path above is the plan.
3. Employer verification is manual via `ADMIN_TOKEN`; no self-serve domain
   verification flow yet.
4. Microsoft OAuth is scaffolded but unconfigured (optional).
5. No payment processing (deliberate, per product phase).
6. Matching uses documented skill fields only; it cannot read free-text
   requirements yet.
