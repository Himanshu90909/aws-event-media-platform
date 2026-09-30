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
  type text,
  skills jsonb DEFAULT '[]',
  posted_at timestamptz,
  expires_at timestamptz,
  is_verified boolean DEFAULT false,
  raw_source_payload jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, external_id)
);
-- migration: type column added post-launch
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS type text;

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

-- ================= career-intelligence upgrade =================

CREATE TABLE IF NOT EXISTS resumes (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  file_name text NOT NULL,
  content_type text NOT NULL,
  text_content text NOT NULL,
  parsed jsonb NOT NULL,
  consent boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS resumes_user_idx ON resumes (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS consents (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  kind text NOT NULL,
  granted boolean NOT NULL,
  note text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
  id uuid PRIMARY KEY,
  source text NOT NULL,
  status text NOT NULL,
  stats jsonb DEFAULT '{}',
  error text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS verification_events (
  id uuid PRIMARY KEY,
  job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
  status text NOT NULL,
  note text,
  actor text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS verification_job_idx ON verification_events (job_id, created_at DESC);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_verified_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS skills_required jsonb DEFAULT '[]';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS skills_preferred jsonb DEFAULT '[]';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS posted_by_employer uuid;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sponsored boolean DEFAULT false;

CREATE TABLE IF NOT EXISTS job_reports (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  job_id uuid REFERENCES jobs(id) ON DELETE CASCADE,
  reason text NOT NULL,
  details text,
  status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employers (
  id uuid PRIMARY KEY,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  company_name text NOT NULL,
  website text,
  about text,
  logo_url text,
  verification_status text NOT NULL DEFAULT 'pending',
  verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id)
);

CREATE TABLE IF NOT EXISTS messages (
  id uuid PRIMARY KEY,
  application_id uuid REFERENCES applications(id) ON DELETE CASCADE,
  sender_id uuid REFERENCES users(id) ON DELETE CASCADE,
  recipient_id uuid REFERENCES users(id) ON DELETE CASCADE,
  body text NOT NULL,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_application_idx ON messages (application_id, created_at);

CREATE TABLE IF NOT EXISTS learning_progress (
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  role text NOT NULL,
  skill text NOT NULL,
  status text NOT NULL DEFAULT 'todo',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, role, skill)
);
"""
