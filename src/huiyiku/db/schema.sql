PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT,
  color TEXT,
  is_system INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  aliases_json TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  source_type TEXT NOT NULL,
  original_filename TEXT,
  original_path TEXT,
  wav_path TEXT,
  upload_audio_path TEXT,
  waveform_path TEXT,
  file_size_bytes INTEGER,
  media_checksum TEXT,
  duration_ms INTEGER,
  language TEXT NOT NULL DEFAULT 'zh',
  status TEXT NOT NULL,
  error_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS meeting_participants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  person_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(meeting_id, person_id),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS speaker_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  speaker_label TEXT NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  confidence REAL,
  model TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS meeting_speakers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  speaker_label TEXT NOT NULL,
  display_name TEXT,
  person_id INTEGER,
  status TEXT NOT NULL,
  merged_into_id INTEGER,
  updated_at TEXT NOT NULL,
  UNIQUE(meeting_id, speaker_label),
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(person_id) REFERENCES persons(id)
);

CREATE TABLE IF NOT EXISTS transcript_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  speaker_id INTEGER,
  evidence_turn_ids_json TEXT,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  normalized_text TEXT NOT NULL,
  confidence REAL,
  asr_provider TEXT,
  asr_model TEXT,
  asr_model_version TEXT,
  review_status TEXT NOT NULL DEFAULT 'auto',
  updated_at TEXT NOT NULL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(speaker_id) REFERENCES meeting_speakers(id)
);

CREATE TABLE IF NOT EXISTS speaker_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  speaker_id INTEGER NOT NULL,
  speech_ms INTEGER NOT NULL,
  segment_count INTEGER NOT NULL,
  speech_ratio REAL NOT NULL,
  timeline_json TEXT,
  metric_version TEXT NOT NULL DEFAULT 'v1',
  computed_at TEXT NOT NULL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(speaker_id) REFERENCES meeting_speakers(id)
);

CREATE TABLE IF NOT EXISTS meeting_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL,
  speaker_id INTEGER,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  chunk_type TEXT NOT NULL,
  content TEXT NOT NULL,
  tokenized_content TEXT NOT NULL,
  embedding BLOB,
  embedding_model TEXT,
  embedding_dim INTEGER,
  source_report_version_id INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id),
  FOREIGN KEY(group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL UNIQUE,
  current_version_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS report_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  source TEXT NOT NULL,
  model TEXT,
  prompt_version TEXT,
  content_json TEXT NOT NULL,
  content_md TEXT,
  status TEXT NOT NULL,
  stale_reason TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(report_id) REFERENCES reports(id)
);

CREATE TABLE IF NOT EXISTS action_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  source_meeting_id INTEGER,
  source_report_version_id INTEGER,
  title TEXT NOT NULL,
  owner_person_id INTEGER,
  status TEXT NOT NULL,
  priority TEXT,
  due_at TEXT,
  evidence_json TEXT,
  merged_into_id INTEGER,
  origin TEXT NOT NULL,
  user_edited INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS action_item_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_item_id INTEGER NOT NULL,
  from_status TEXT,
  to_status TEXT,
  reason TEXT,
  meeting_id INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(action_item_id) REFERENCES action_items(id)
);

CREATE TABLE IF NOT EXISTS qa_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  scope_json TEXT,
  question TEXT NOT NULL,
  answer TEXT,
  citations_json TEXT,
  retrieval_json TEXT,
  model_json TEXT,
  usage_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT,
  progress_json TEXT,
  error_json TEXT,
  usage_json TEXT,
  idempotency_key TEXT UNIQUE,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  timeout_seconds INTEGER NOT NULL,
  heartbeat_at TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  FOREIGN KEY(meeting_id) REFERENCES meetings(id)
);

CREATE TABLE IF NOT EXISTS glossaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER,
  term TEXT NOT NULL,
  replacement TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY(group_id) REFERENCES groups(id)
);

CREATE TABLE IF NOT EXISTS model_registry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT NOT NULL,
  provider TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_version TEXT,
  source_url TEXT,
  display_name TEXT,
  execution TEXT NOT NULL,
  endpoint TEXT,
  secret_ref TEXT,
  embedding_dim INTEGER,
  license TEXT,
  is_gated INTEGER NOT NULL DEFAULT 0,
  terms_accepted INTEGER NOT NULL DEFAULT 0,
  usage_class TEXT,
  capabilities_json TEXT,
  review_status TEXT NOT NULL DEFAULT 'pending',
  status TEXT NOT NULL DEFAULT 'untested',
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(task, provider, model_name, model_version)
);

CREATE TABLE IF NOT EXISTS group_digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  period_start TEXT,
  period_end TEXT,
  content_json TEXT NOT NULL,
  content_md TEXT,
  source_meeting_ids_json TEXT,
  model TEXT,
  prompt_version TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(group_id) REFERENCES groups(id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS meeting_chunks_fts USING fts5(
  tokenized_content,
  content='meeting_chunks',
  content_rowid='id',
  tokenize='unicode61'
);

CREATE INDEX IF NOT EXISTS idx_meetings_group_occurred ON meetings(group_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);
CREATE INDEX IF NOT EXISTS idx_transcript_meeting_start ON transcript_segments(meeting_id, start_ms);
CREATE INDEX IF NOT EXISTS idx_chunks_meeting_active ON meeting_chunks(meeting_id, is_active);
CREATE INDEX IF NOT EXISTS idx_chunks_group_active ON meeting_chunks(group_id, is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_status_type ON jobs(status, type);
CREATE INDEX IF NOT EXISTS idx_jobs_meeting ON jobs(meeting_id);
CREATE INDEX IF NOT EXISTS idx_actions_group_status ON action_items(group_id, status);
CREATE INDEX IF NOT EXISTS idx_qa_session ON qa_messages(session_id);
