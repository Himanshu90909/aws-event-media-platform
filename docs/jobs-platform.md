# MediaFlow Jobs Platform — Implementation Notes

Implementation of the **MediaFlow Jobs Platform Implementation Specification** on top of
the existing event-driven media platform. The media workflow (`POST /api/jobs`,
DynamoDB/SQS/S3 worker contract, webhooks) is **fully preserved**; the jobs marketplace
runs beside it.

## Route prefix

The spec's API contract uses `/api/jobs` for the marketplace, but that path is already
the live media-processing API (documented as an open integration API with active
callers). To preserve it, marketplace endpoints are mounted under **`/api/mf/*`**
(MediaFlow) with identical shapes and semantics:

| Spec route | Implemented as |
|---|---|
| `/api/auth/*` | `/api/mf/auth/*` |
| `/api/me*` | `/api/mf/me*` |
| `/api/jobs*` | `/api/mf/jobs*` |
| `/api/applications/*` | `/api/mf/applications/*` |
| `/api/health`, `/api/overview` | `/api/mf/health`, `/api/mf/overview` |

`vercel.json` rewrites `/api/mf/(.*)` to the `api/marketplace.py` serverless function
with the original path in `__route`. All other rewrites (media API, radar) unchanged.

## Frontend

Single-file SPA at `public/jobs.html` (zero dependencies, inline CSS/JS), served at
**`/careers`** via rewrite. Views: Discover, Jobs, Challenges, Internships,
My applications (Kanban), Notifications, Profile — matching the spec's navigation.
Dark, gradient-accent UI; responsive down to mobile.

## Authentication

* **Live mode**: when `GOOGLE_CLIENT_ID/SECRET` or `MICROSOFT_CLIENT_ID/SECRET` are
  set, `/api/mf/auth/login/{provider}` redirects to the provider (signed `state`,
  10-min validity, CSRF + replay protection). The callback exchanges the code
  **server-side**, verifies the identity token, and stores only provider subject,
  email, name, avatar. Access tokens are never stored.
* **Demo mode** (honest fallback per spec §13): with no credentials configured,
  `POST /api/mf/auth/demo` creates a clearly-flagged demo session. `/api/mf/health`
  and the UI footer state plainly that live OAuth is not active.
* Sessions: HMAC-signed, HTTP-only, SameSite=Lax cookies, rotated on every login,
  7-day TTL, revoked on logout/account deletion. Rate limits on login/demo/sensitive
  routes (per-instance).

## Persistence

Two adapters behind one interface:

* **`PgStore`** (production): activated by `DATABASE_URL` + `psycopg2-binary`.
  Schema in `db/schema.py` — the spec's core data model (users, identities, jobs,
  saved_jobs, applications, files, notifications, audit_events) with
  `unique(user_id, job_id)` duplicate-application protection. Migration runs
  idempotently on boot; first boot seeds the job catalog.
* **`MemStore`** (dev fallback): explicit in-memory store; `/api/mf/health` reports
  `persistence: "ephemeral"`. Never claimed as durable.

Go-live: provision Postgres (Supabase/Neon), set `DATABASE_URL`, done — code path is
identical.

## Application pipeline

`SAVED → APPLIED → SCREENING → INTERVIEW → OFFER`, with `REJECTED` reachable from
SAVED/APPLIED/SCREENING/INTERVIEW. Invalid transitions are rejected with
`INVALID_TRANSITION` (422). Every change appends to `status_history` and writes an
audit event. Row-level ownership is enforced on every application/file/notification
route (cross-user access → 404, not 403, to avoid existence leaks).

## Files / media integration

`POST /api/mf/me/files/presign` validates name/Content-Type allowlist/size (25 MB),
creates a private object key `private/{userId}/{uuid}/{fileName}`, and hands the client
the **existing MediaFlow pipeline** (`POST /api/jobs` → async worker) for processing.
The client links the media job and tracks status through
`PATCH /api/mf/me/files/{fileId}` (`QUEUED/PROCESSING/COMPLETED/FAILED`).

## Notifications

Deduplicated within 24 h per (user, type, title); unread badge in the nav;
mark-as-read endpoint per spec.

## Env vars (production)

```env
DATABASE_URL=            # activates durable Postgres adapter
AUTH_SESSION_SECRET=     # HMAC key for sessions/OAuth state
GOOGLE_CLIENT_ID=        # live Google OAuth
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=     # e.g. https://<domain>/api/mf/auth/callback/google
MICROSOFT_CLIENT_ID=     # live Microsoft OAuth
MICROSOFT_CLIENT_SECRET=
MICROSOFT_TENANT=common
MICROSOFT_REDIRECT_URI= # e.g. https://<domain>/api/mf/auth/callback/microsoft
APP_ORIGIN=             # e.g. https://aws-event-media-platform.vercel.app (Secure cookies)
```

No real values are committed; add them in Vercel → Settings → Environment Variables.

## Verification

Local end-to-end suite (30 checks) covering: demo login, session, search/filters,
pagination, save/unsave, idempotent apply, transition guards, status history,
notes/next-action, notifications + read, presign allowlist/size limits, job detail +
similar, **cross-user 404**, profile update, overview, account deletion, session
revocation, health. `tests/test_jobs_platform.py` mirrors the core cases for CI.
