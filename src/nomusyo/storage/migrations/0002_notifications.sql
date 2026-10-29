-- 0002 notifications (docs/design/schedule-data-model.md section 5.2).
-- Do not edit after release: the runner records a checksum. No data is changed here;
-- the application backfills via schedule.planner.plan_all.

CREATE TABLE notifications (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id           INTEGER NOT NULL,
  target_type        TEXT NOT NULL CHECK (target_type IN ('event','todo')),
  event_id           INTEGER REFERENCES events(id) ON DELETE CASCADE,
  todo_id            INTEGER REFERENCES todos(id)  ON DELETE CASCADE,
  rule_id            INTEGER REFERENCES reminder_rules(id) ON DELETE SET NULL,
  kind               TEXT NOT NULL CHECK (kind IN ('remind','start','due','snooze')),
  occurrence_local   TEXT NOT NULL CHECK (length(occurrence_local) = 19),
                     -- 予定: 元の発生時刻。TODO: 生成時点の due_local
  dedupe_key         TEXT NOT NULL UNIQUE,            -- 4.2
  target_utc         TEXT NOT NULL,   -- 予定の開始 / TODO の期限(override 反映後。reconcile が更新)
  scheduled_utc      TEXT NOT NULL,   -- 本来の送信時刻(遅延判定の基準。バックオフで変えない)
  fire_at_utc        TEXT NOT NULL,   -- 現在の送信予定(バックオフ・スヌーズで後ろへ)
  status             TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','sending','sent','skipped','failed')),
  skip_reason        TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  claimed_at_utc     TEXT,
  sent_at_utc        TEXT,
  channel_id         INTEGER,
  discord_message_id INTEGER,
  late               INTEGER NOT NULL DEFAULT 0 CHECK (late IN (0,1)),
  recovered          INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0,1)),
  last_error         TEXT,
  snooze_of          INTEGER REFERENCES notifications(id) ON DELETE SET NULL,
  acked_at_utc       TEXT,
  created_at_utc     TEXT NOT NULL,
  updated_at_utc     TEXT NOT NULL,
  CHECK ((target_type = 'event' AND event_id IS NOT NULL AND todo_id IS NULL)
      OR (target_type = 'todo'  AND todo_id  IS NOT NULL AND event_id IS NULL)),
  CHECK (status <> 'skipped' OR skip_reason IS NOT NULL)
);
CREATE INDEX ix_notif_due     ON notifications(fire_at_utc) WHERE status = 'pending';
CREATE INDEX ix_notif_sending ON notifications(claimed_at_utc) WHERE status = 'sending';
CREATE INDEX ix_notif_event   ON notifications(event_id, occurrence_local) WHERE event_id IS NOT NULL;
CREATE INDEX ix_notif_todo    ON notifications(todo_id) WHERE todo_id IS NOT NULL;
CREATE INDEX ix_notif_purge   ON notifications(status, updated_at_utc);
