-- 0001 initial schema (docs/design/schedule-data-model.md section 5.1).
-- Do not edit after release: the runner records a checksum. Change via a new migration.

CREATE TABLE events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id       INTEGER NOT NULL,
  kind           TEXT NOT NULL DEFAULT 'event' CHECK (kind IN ('event','reminder')),
  title          TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  note           TEXT CHECK (note IS NULL OR length(note) <= 2000),
  place          TEXT CHECK (place IS NULL OR length(place) <= 200),
  all_day        INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0,1)),
  dtstart_local  TEXT NOT NULL CHECK (length(dtstart_local) = 19),
  duration_min   INTEGER CHECK (duration_min IS NULL OR duration_min > 0),
  tz             TEXT NOT NULL DEFAULT 'Asia/Tokyo',
  rrule          TEXT,
  skip_holidays  INTEGER NOT NULL DEFAULT 0 CHECK (skip_holidays IN (0,1)),
  series_id      INTEGER REFERENCES events(id),
  start_utc      TEXT NOT NULL CHECK (length(start_utc) = 20),
  until_utc      TEXT CHECK (until_utc IS NULL OR length(until_utc) = 20),
  source         TEXT NOT NULL DEFAULT 'manual',
  external_uid   TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  deleted_at_utc TEXT,
  CHECK (rrule IS NULL OR kind = 'event')
);
CREATE INDEX ix_events_range ON events(owner_id, start_utc) WHERE deleted_at_utc IS NULL;
CREATE INDEX ix_events_series ON events(series_id) WHERE series_id IS NOT NULL;
CREATE UNIQUE INDEX ux_events_external
  ON events(owner_id, source, external_uid) WHERE external_uid IS NOT NULL;

CREATE TABLE todos (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id       INTEGER NOT NULL,
  list_name      TEXT NOT NULL DEFAULT 'todo',
  title          TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  note           TEXT CHECK (note IS NULL OR length(note) <= 2000),
  priority       INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 3),
  due_local      TEXT CHECK (due_local IS NULL OR length(due_local) = 19),
  due_all_day    INTEGER NOT NULL DEFAULT 0 CHECK (due_all_day IN (0,1)),
  tz             TEXT NOT NULL DEFAULT 'Asia/Tokyo',
  due_utc        TEXT CHECK (due_utc IS NULL OR length(due_utc) = 20),
  done_at_utc    TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  deleted_at_utc TEXT,
  CHECK ((due_local IS NULL) = (due_utc IS NULL)),
  CHECK (due_all_day = 0 OR due_local IS NOT NULL)
);
CREATE INDEX ix_todos_open ON todos(owner_id, due_utc)
  WHERE done_at_utc IS NULL AND deleted_at_utc IS NULL;

CREATE TABLE reminder_rules (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       INTEGER REFERENCES events(id) ON DELETE CASCADE,
  todo_id        INTEGER REFERENCES todos(id)  ON DELETE CASCADE,
  offset_min     INTEGER CHECK (offset_min IS NULL OR offset_min >= 0),
  at_local       TEXT CHECK (at_local IS NULL OR length(at_local) = 5),
  day_offset     INTEGER NOT NULL DEFAULT 0 CHECK (day_offset >= 0),
  created_at_utc TEXT NOT NULL,
  CHECK ((event_id IS NULL) <> (todo_id IS NULL)),
  CHECK ((offset_min IS NULL) <> (at_local IS NULL))
);
CREATE UNIQUE INDEX ux_rules_event ON reminder_rules
  (event_id, COALESCE(offset_min, -1), COALESCE(at_local, ''), day_offset) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX ux_rules_todo ON reminder_rules
  (todo_id, COALESCE(offset_min, -1), COALESCE(at_local, ''), day_offset) WHERE todo_id IS NOT NULL;

CREATE TABLE settings (
  owner_id       INTEGER NOT NULL,
  key            TEXT NOT NULL,
  value          TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (owner_id, key)
) WITHOUT ROWID;

CREATE TABLE job_runs (
  job             TEXT NOT NULL,
  run_date        TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('started','done','failed','skipped')),
  attempts        INTEGER NOT NULL DEFAULT 1,
  detail          TEXT,
  started_at_utc  TEXT NOT NULL,
  finished_at_utc TEXT,
  PRIMARY KEY (job, run_date)
) WITHOUT ROWID;

CREATE TABLE instance_lock (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  holder         TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  heartbeat_utc  TEXT NOT NULL
);
