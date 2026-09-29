"""MediaFlow Jobs Platform — PostgreSQL schema (spec: core data model).

Used by PgStore._migrate() when DATABASE_URL is configured. Idempotent:
safe to run on every boot (CREATE TABLE IF NOT EXISTS).
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY,
  email text UNIQUE NOT NULL,
  display_name text,
  avatar_url text,
  headline text,
  location text,
  bio text,
  profile_visibility text DEFAULT 'private',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identities (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  provider text NOT NULL,            -- google | microsoft
  provider_subject text NOT NULL,
  provider_email text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_subject)
);

CREATE TABLE IF NOT EXISTS jobs (
  id uuid PRIMARY KEY,
  external_id text,
  source text NOT NULL,
  title text NOT NULL,
  company text NOT NULL,
  company_logo_url text,
  description text NOT NULL,
  apply_url text,
  employment_type text,
  experience_level text,
  location text,
  remote_type text,
  salary_min numeric,
  salary_max numeric,
  salary_currency text,
  category text NOT NULL,
  skills jsonb DEFAULT '[]',
  posted_at timestamptz,
  expires_at timestamptz,
  is_verified boolean DEFAULT false,
  raw_source_payload jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_live_idx ON jobs (expires_at, category, posted_at DESC);
CREATE INDEX IF NOT EXISTS jobs_search_idx ON jobs USING gin (to_tsvector('simple', title || ' ' || company));

CREATE TABLE IF NOT EXISTS saved_jobs (
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, job_id)
);

CREATE TABLE IF NOT EXISTS applications (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
  status text NOT NULL,              -- SAVED/APPLIED/SCREENING/INTERVIEW/OFFER/REJECTED
  applied_at timestamptz,
  next_action text,
  next_action_at timestamptz,
  private_notes text,
  status_history jsonb DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, job_id)          -- duplicate application protection
);

CREATE TABLE IF NOT EXISTS files (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  application_id uuid REFERENCES applications(id) ON DELETE SET NULL,
  object_key text NOT NULL,
  file_name text NOT NULL,
  content_type text NOT NULL,
  size_bytes bigint NOT NULL,
  checksum text,
  processing_status text NOT NULL,   -- QUEUED/PROCESSING/COMPLETED/FAILED
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  type text NOT NULL,
  title text NOT NULL,
  body text NOT NULL,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_events (
  id uuid PRIMARY KEY,
  user_id uuid,
  event_type text NOT NULL,
  request_id text,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""
