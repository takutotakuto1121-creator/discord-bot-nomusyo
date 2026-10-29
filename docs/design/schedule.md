# スケジュール管理 設計

状態: 設計(未実装)。前提は `resident-bot.md`(常駐 Bot 化)。タスクは `tasks-v2.md`、他機能は `roadmap.md`。
外部ライブラリ・Discord API の細部で確信が持てない点は「要確認」と書く。実装前に公式ドキュメントで確認すること。

## 1. 目的と「完全に」の定義(スコープ)

利用者は自分1人。「スケジュール管理をこの Bot で完全に行える」を、次の機能の集合と定義する。

| # | 機能 | 区分 | 備考 |
|---|---|---|---|
| S1 | 予定の登録/編集/削除/一覧 | 必須 | 単発・終日・時間指定。場所/メモ付き |
| S2 | 繰り返し予定 | 必須 | 毎日/毎週/毎月/平日/第N曜日など。例外(この回だけ削除・変更)は段階導入 |
| S3 | 事前リマインド(複数) | 必須 | 予定ごとに「10分前・1時間前・前日20:00」など複数 |
| S4 | TODO/期限 | 必須 | 期限なし/あり、完了、期限前リマインド、期限切れ表示 |
| S5 | 今日・今週の予定表示 | 必須 | `/today` `/week`。TODO も併記 |
| S6 | 毎朝の予定ダイジェスト | 必須 | 07:00 台。ニュースとの関係は 6.3 |
| S7 | 自然言語での入力 | 必須(後フェーズ) | 「来週火曜15時に歯医者」。必ず確認ボタン経由 |
| S8 | タイムゾーン | 必須 | **Asia/Tokyo(JST)** 固定。設定で変更可能な構造だけ残す |
| S9 | 祝日の扱い | 必須(後フェーズ) | 祝日表示、「平日のみ」繰り返しで祝日を除く/除かないを選択 |
| S10 | 完了/スヌーズ(ボタン) | 必須 | 通知メッセージに「完了」「10分後」「1時間後」「明日」 |
| S11 | ダウンタイム中の取りこぼし再送 | 必須 | 再起動後に未送信を検出して「遅延」付きで送る |
| S12 | 外部カレンダー連携 | 任意 | 第7章。ユーザー判断 |
| S13 | 検索・履歴 | 任意 | `/schedule search`。後回し可 |
| S14 | 複数ユーザー/共有 | 対象外 | 単一ユーザー前提。ただしテーブルに `owner_id` は持たせておく(後の拡張を安価に) |

「完全に」の達成基準: S1〜S11 が動き、Discord 以外(スマホカレンダー等)を見なくても日常の予定・期限を管理できること。S12 は「見る側のカレンダーに出したい」場合のみ。

## 2. コマンド設計(スラッシュコマンド)

方針: コマンド名は英語(小文字)、説明・引数説明は日本語。応答は原則 ephemeral(自分だけに見える)。通知とダイジェストはチャンネルへ通常投稿。3秒以内に応答できない処理(DB 以外、Claude API 呼び出し)は `defer()` する。日本語のコマンド名が使えるかは **要確認**(英語で進める)。

| コマンド | 引数 | 応答 |
|---|---|---|
| `/add` | `when`(文字列)`title`、任意: `end`/`duration`、`place`、`note`、`remind`(例 `10m,1h,1d@20:00`)、`repeat`(選択式)| 登録内容の Embed + 「保存」「修正」「キャンセル」ボタン(プレビュー確認)。`when` は厳密形式(`2026-11-03 15:00`、`11/3 15:00`、`明日 9:00` 等の簡易形)のみ。自然言語は `/ask` |
| `/ask` | `text`(自由文) | 後フェーズ。自然言語を解釈し、解釈結果の Embed + 「登録」「修正(モーダル)」「キャンセル」。曖昧な箇所は候補ボタン |
| `/add_form` | なし | モーダル(タイトル/日時/場所/メモ/リマインド)を開く。引数が多い入力向け |
| `/list` | 任意: `range`(today/week/month/all)、`query` | 予定一覧 Embed(ページ送りボタン)。各行に短い ID |
| `/today` `/week` | なし | 今日/今週の予定+TODO+祝日。`/week` は日ごとにフィールド |
| `/edit` | `event`(オートコンプリート)| 現在値入りモーダル。繰り返し予定なら「この回のみ/以降すべて」選択(段階導入) |
| `/delete` | `event`(オートコンプリート)| 確認ボタン付き。繰り返しは「この回のみ/すべて」 |
| `/todo add` | `title`、任意: `due`、`remind`、`priority` | 登録確認 |
| `/todo list` | 任意: `status`(open/done/overdue)| 一覧 + 各行に「完了」ボタン(最大5行/メッセージ。要確認: ボタン数上限は ActionRow 5 × 5) |
| `/todo done` `/todo delete` | `todo`(オートコンプリート) | 完了/削除 |
| `/remind` | `when`、`text` | 予定を作らない一回きりリマインドのショートカット(内部では `kind=reminder` の予定) |
| `/snooze_default` 等の `/settings` | `digest_time`、`default_remind`、`notify_channel`、`dm` | 設定の表示/変更 |
| `/news` | なし | `resident-bot.md` 参照 |
| `/status` | なし | 稼働状況(最終ポーリング時刻、未送信件数、DB サイズ、バージョン)。死活確認用 |

ID: 内部は整数主キー。表示は主キーそのまま(1人利用なので十分)。選択はオートコンプリート(直近/未来の予定のタイトルで検索)を主とし、ID 直接入力は補助。

### 2.1 UI 部品

- Embed: 予定1件=1 Embed(タイトル、日時、場所、繰り返し、リマインド、ID を footer)。一覧は日付見出し+箇条書き。色は種別で固定(予定=青、TODO=緑、期限切れ=赤)。文字数上限は `discord_io/formatter.py` の方針(切り詰め・分割)を踏襲し、**共通ヘルパを再利用または並置**する。
- 通知メッセージのボタン: 「完了」「10分後」「1時間後」「明日の同時刻」「削除」。`custom_id` に `kind:id:occurrence` を埋め込み、再起動後も動く **永続 View**(`timeout=None`、起動時に登録)にする。可変 ID を埋め込む動的ボタンの扱い(`DynamicItem` 等)は **要確認**(discord.py のバージョンで差がある)。
- モーダル: 修正・フォーム入力用。モーダルのフィールド数上限(5)に注意(要確認)。
- 確認 View: 自然言語/`/add` の登録前プレビューは短命(timeout 約3分)。timeout したら何も保存しない(未保存のまま破棄)。
- メンション: 通知は本人のみメンション(`AllowedMentions(users=[owner])`、`@everyone`/`@here`/ロール無効)。

## 3. データモデル

### 3.1 保存先の選択

| 項目 | SQLite | PostgreSQL |
|---|---|---|
| 運用負荷 | ファイル1つ。サーバー不要 | サーバー/コンテナ必要。バックアップ・更新も運用対象 |
| 同時書き込み | 単一プロセス・単一ユーザーなら問題なし(WAL モード) | 余裕 |
| ホスティング制約 | 永続ボリュームが必要(Fly.io は volume、VPS/自宅はディスク) | マネージド(Neon/Supabase 等)を使えばボリューム不要 |
| バックアップ | ファイルコピー(`sqlite3` の backup API/`VACUUM INTO`) | pg_dump |
| 移行容易性 | SQLAlchemy 等を使わないと SQL 方言依存 | - |

推奨: **SQLite(WAL)**。単一ユーザー・単一プロセス前提で運用最小。複数インスタンス化や PaaS でボリューム不可の場合のみ PostgreSQL を検討(その場合の選択肢を未決事項に記載)。
アクセス層: 標準 `sqlite3` を `asyncio.to_thread` で包むか `aiosqlite`(要確認: 最新版の互換性)。ORM は入れず、薄い Repository クラス+ SQL ファイルのマイグレーション(`schema_version` テーブル、連番 SQL)で十分。リポジトリを Protocol 化し、テストではインメモリ SQLite を使う。

### 3.2 時刻の持ち方(重要)

- 単発予定: `start_utc`(UTC の ISO8601 文字列または epoch 秒)+ `tz`(IANA 名、既定 `Asia/Tokyo`)。
- 繰り返し予定: **壁時計時刻**(ローカル日時 `dtstart_local` と `tz`)+ RRULE 文字列で保持し、展開時に `zoneinfo` で UTC 化する。UTC 固定にすると夏時間のある地域で時刻がずれる(JST は夏時間なしだが、将来のため構造は保つ)。
- 終日予定: `all_day=1`、日付(`start_date`)のみ。通知時刻は設定の `all_day_notify_time`(既定 08:00)で解決する。
- 全て aware datetime で扱い、naive datetime をコード上で禁止する(lint/テストで検出)。

### 3.3 テーブル定義(案)

```sql
-- 予定(単発・繰り返し・一回きりリマインドを同一テーブルで)
CREATE TABLE events (
  id            INTEGER PRIMARY KEY,
  owner_id      INTEGER NOT NULL,            -- Discord ユーザー ID
  kind          TEXT NOT NULL DEFAULT 'event',  -- event | reminder
  title         TEXT NOT NULL,
  note          TEXT,
  place         TEXT,
  all_day       INTEGER NOT NULL DEFAULT 0,
  dtstart_local TEXT NOT NULL,               -- 'YYYY-MM-DDTHH:MM:SS' (壁時計)。終日は日付のみでも可
  duration_min  INTEGER,                     -- NULL=開始のみ
  tz            TEXT NOT NULL DEFAULT 'Asia/Tokyo',
  rrule         TEXT,                        -- NULL=単発。例 'FREQ=WEEKLY;BYDAY=MO,WE'
  rrule_until   TEXT,                        -- 終了日(RRULE の UNTIL と二重管理しない: どちらかに統一。要確認)
  skip_holidays INTEGER NOT NULL DEFAULT 0,  -- 祝日の回をスキップ
  source        TEXT NOT NULL DEFAULT 'manual', -- manual | nl | ical:<id>
  external_uid  TEXT,                        -- iCal UID(連携時)
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  deleted_at    TEXT                         -- 論理削除(誤削除の復元用)
);

-- 繰り返し予定の例外(この回だけ削除/変更)
CREATE TABLE event_exceptions (
  event_id         INTEGER NOT NULL REFERENCES events(id),
  occurrence_local TEXT NOT NULL,            -- 元の発生時刻(壁時計)
  action           TEXT NOT NULL,            -- skip | override
  override_start_local TEXT, override_title TEXT, override_place TEXT,
  PRIMARY KEY (event_id, occurrence_local)
);

-- リマインド規則(予定ごとに複数)
CREATE TABLE reminder_rules (
  id          INTEGER PRIMARY KEY,
  event_id    INTEGER NOT NULL REFERENCES events(id),
  offset_min  INTEGER,                       -- 開始の N 分前。NULL なら at_local を使う
  at_local    TEXT,                          -- 'HH:MM' かつ day_offset(前日20:00 など)
  day_offset  INTEGER NOT NULL DEFAULT 0
);

-- 通知の実体(発生ごと×ルールごとに1行)。重複防止と再送の核
CREATE TABLE notifications (
  id            INTEGER PRIMARY KEY,
  event_id      INTEGER NOT NULL REFERENCES events(id),
  rule_id       INTEGER,                     -- NULL=開始時刻ちょうど/スヌーズ
  occurrence_local TEXT NOT NULL,            -- どの発生に対する通知か
  fire_at_utc   TEXT NOT NULL,               -- 送信予定時刻(UTC)
  status        TEXT NOT NULL DEFAULT 'pending', -- pending | sending | sent | skipped | failed
  attempts      INTEGER NOT NULL DEFAULT 0,
  sent_at_utc   TEXT,
  discord_message_id INTEGER,
  late          INTEGER NOT NULL DEFAULT 0,  -- 遅延送信フラグ
  snooze_of     INTEGER REFERENCES notifications(id),
  UNIQUE (event_id, occurrence_local, rule_id, snooze_of)  -- 冪等性キー(NULL の扱いは要確認: SQLite は NULL を重複扱いしない。COALESCE した生成列で対処)
);
CREATE INDEX idx_notif_due ON notifications(status, fire_at_utc);

-- TODO
CREATE TABLE todos (
  id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
  title TEXT NOT NULL, note TEXT, priority INTEGER NOT NULL DEFAULT 0,
  due_local TEXT, tz TEXT NOT NULL DEFAULT 'Asia/Tokyo',
  done_at_utc TEXT, created_at TEXT NOT NULL, deleted_at TEXT
);
-- TODO のリマインドは notifications に todo_id を持たせるか、別表にするかは実装時に決定(要検討)。

-- 設定(1行 or key-value)
CREATE TABLE settings (owner_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (owner_id, key));

-- 定期ジョブの実行記録(ニュース/ダイジェストの二重投稿防止、全般の冪等性)
CREATE TABLE job_runs (
  job TEXT NOT NULL,                         -- 'daily_news' | 'daily_digest'
  run_date TEXT NOT NULL,                    -- JST の日付
  status TEXT NOT NULL,                      -- started | done | failed
  started_at_utc TEXT NOT NULL, finished_at_utc TEXT,
  PRIMARY KEY (job, run_date)
);

CREATE TABLE schema_version (version INTEGER NOT NULL);
```

### 3.4 繰り返し展開とマテリアライズ

- RRULE は `python-dateutil` の `rrule`/`rrulestr` で展開する(要確認: `rrulestr` に `dtstart` と tz-aware を渡す際の `UNTIL` の tz 要件。UNTIL は aware 指定が必要という制約があるため、UNTIL は UTC で保存する案が安全)。
- `notifications` は **向こう N 日分(既定 35 日)を先行生成**する「ローリング・マテリアライズ」方式。理由: (1) 取りこぼし検出が `status='pending' AND fire_at_utc <= now` の単純クエリで済む、(2) 冪等性が UNIQUE 制約で担保される、(3) スケジューラが RRULE を毎回評価しなくてよい。日次ジョブ(と起動時)で先行生成を補充する。予定の編集/削除時は該当 `pending` を再生成/`skipped` にする。
- 無期限の繰り返しは展開窓の範囲のみ生成。月次の「毎月31日」等は dateutil の仕様(存在しない日はスキップ)になる点をユーザーに説明する(要確認)。

### 3.5 祝日

- 候補: Python `holidays` パッケージ(`holidays.Japan`。年次更新が必要、最新の祝日・振替休日に追随しているか **要確認**)/ 内閣府「国民の祝日」CSV(公式、URL・文字コード・形式は **要確認**、取得失敗時のキャッシュが必要)。
- 推奨: `holidays` パッケージ(依存追加のみで足りる)。精度に不安があれば内閣府 CSV を週次で取得してキャッシュするバックアップ経路を後から足す。
- 使い方: (1) `/today` `/week` とダイジェストに祝日名を表示、(2) `skip_holidays=1` の繰り返しで該当回を `skipped`、(3) 祝日は DB に持たず、都度ライブラリで判定(単純化)。

## 4. スケジューラ設計

### 4.1 方式の比較

| 観点 | `discord.ext.tasks` のポーリング | APScheduler | 自前 asyncio ループ(DB 駆動) |
|---|---|---|---|
| 依存 | discord.py 同梱 | 追加依存(要確認: v3 と v4 で API が大きく違う。v4 の安定度を確認) | なし |
| 永続化・再起動復旧 | 自分で DB に持つ | ジョブストアで可能だが二重管理になりがち | DB が唯一の真実(自然) |
| 時刻精度 | ポーリング間隔(例 20〜30 秒)に依存 | 秒単位 | 次の fire_at まで sleep、最大 30 秒で再評価 |
| 繰り返し | RRULE は自前 | cron/interval は強いが RRULE 非対応 | 自前(dateutil) |
| テスト容易性 | `tasks.loop` はモックしにくい | スケジューラ内部に依存 | 純粋関数(`now` 注入)にでき最も容易 |
| 障害時の挙動 | `tasks.loop` は例外で止まることがある(`error` ハンドラ/再開が必要。要確認) | ジョブ失敗の扱いが設定次第 | 自前で try/except・再開を設計 |

推奨: **DB 駆動のポーリング**。ループ駆動は `discord.ext.tasks.loop(seconds=30)` を薄いトリガとして使い、中身は「`now` を引数に取る純粋なロジック」(`dispatch_due(now)`)に分離する。APScheduler は予定の真実を二重管理するため採用しない。1人利用で通知数も少ない(1日数十件)ため、30秒ポーリングは SQLite への負荷として無視できる。精度は最大 30 秒の遅れを許容(分単位の予定に十分)。

### 4.2 ディスパッチ手順(冪等性)

1. `now = utcnow()`。`SELECT ... WHERE status='pending' AND fire_at_utc <= now ORDER BY fire_at_utc LIMIT 50`。
2. 各行を **`UPDATE ... SET status='sending', attempts=attempts+1 WHERE id=? AND status='pending'`** でクレームし、影響行数 1 の場合のみ送信(複数プロセス/多重起動でも二重送信しない)。
3. Discord へ送信。成功なら `status='sent'`、`discord_message_id` 保存。失敗(429/5xx/ネットワーク)は指数バックオフで `pending` に戻し `fire_at_utc` を後ろへ、`attempts` が上限(例 5)で `failed` にして管理者に通知。永続エラー(チャンネル削除・権限不足)は `failed` + ログ。
4. 「送信した後、`sent` に更新する前にクラッシュ」の窓は残る(at-least-once)。許容し、再起動時に `sending` のまま残った行は「再送(重複の可能性あり)」と明記して再送する。厳密な exactly-once は不可能なので、**二重通知より取りこぼしを避ける**方針とする。
5. 同一時刻の複数通知(同じ予定の複数ルールが重なる等)は 1 メッセージにまとめる(まとめキー: owner + 分単位)。

### 4.3 再起動時の復旧と取りこぼし再送(S11)

- 起動時(`setup_hook`)に: (1) `sending` を `pending` に戻す、(2) 先行生成の補充、(3) `pending AND fire_at_utc <= now` を処理。
- 遅延の扱い: 遅延 ≤ 猶予(既定 10 分)は通常通知。それ以上は `late=1` として「遅延(本来 HH:MM)」を付与。さらに **予定の開始時刻を過ぎた通知は「事前リマインド」としては意味が薄い**ため、まとめて「不在中に過ぎた予定」1 通のサマリーにする(個別通知を大量に送らない)。TODO の期限切れも同様。
- 長期停止(数日)の後は、過去 N 日分(既定 3 日)より古い未送信は `skipped` に倒す。

### 4.4 毎朝ダイジェストの発火

定期ジョブ(ニュース/ダイジェスト)も `job_runs (job, run_date)` の主キーで「その日 1 回だけ」を保証する。`INSERT` 成功したプロセスだけが実行(失敗時は `failed` に更新し、再試行は設定回数まで)。Bot が 7:00 に落ちていて 7:20 に復帰した場合は、猶予(既定 3 時間)以内なら実行、超えたらスキップしてログ+管理者通知。

## 5. 自然言語の日時解釈(S7)

### 5.1 方式比較

| 観点 | ルール系(dateparser 等) | Claude API で構造化抽出 | ハイブリッド |
|---|---|---|---|
| 精度(日本語の「来週火曜15時」「月末の夜」) | 日本語対応はあるが、「来週◯曜」「夜」「再来週」「毎週」などでぶれる。要確認(dateparser の日本語サポート範囲と相対表現) | 高い。タイトル・場所・リマインド・繰り返しも一括抽出できる | ルールで解けたら採用、失敗/曖昧時のみ LLM |
| タイトルの切り出し | 別途必要(ルールで難しい) | 同時に可能 | LLM |
| コスト | 無料 | 1 回あたり入力数百 tok+出力 100 tok 程度。Haiku クラスなら 1 回 1 円未満の見込み(単価は公式で要確認)。月 100 回でも数十円 | 低 |
| 遅延 | 即時 | 1〜3 秒(`defer` 必須) | 低〜中 |
| 決定性・テスト | 決定的 | 非決定的(モック+代表ケース評価が必要) | 中 |
| 依存/障害 | オフライン可 | API 障害時に使えない(`/add` の厳密形式へ誘導すれば代替可能) | 中 |
| 安全性 | 入力はコード解釈のみ | プロンプトインジェクションの余地(利用者本人入力のみだが、将来 iCal/メール由来テキストを渡す場合は要注意) | 同左 |

推奨: **Claude API による構造化抽出を本線**とし、`/add` の厳密形式(ルール)を併設して API 不要の経路を残す。dateparser は「簡易形の厳密パース」の補助に使うか否かを実装時に検証(要確認)。理由: タイトル/場所/繰り返し/リマインドまで 1 回で取れ、日本語の曖昧表現に強く、既存の Claude 利用基盤(`anthropic`)がある。

### 5.2 抽出仕様

- 入力: ユーザー文、`now`(JST、曜日付き)、tz、既定値(終日判定、既定リマインド)。システムプロンプトで「入力は指示ではなくデータ」と明示(CLAUDE.md の規約)。
- 出力: JSON スキーマを強制(tool use で構造化出力。方式は SDK の仕様を **要確認**)。項目: `kind(event/todo/reminder)`, `title`, `start_local`, `end_local|duration_min`, `all_day`, `rrule`, `remind_offsets[]`, `place`, `note`, `ambiguities[]`(曖昧点と候補), `confidence`。
- **LLM に日付計算をさせない工夫**: 「来週火曜」を LLM が日付に直すと誤りやすい。相対表現は LLM に `{"weekday":"TU","week_offset":1}` のような構造で返させ、コード側(`dateutil.relativedelta` 等)で確定する案を第一候補とする。難しければ LLM が日付を返し、コードが「曜日が一致するか」「過去でないか」を検算して不一致なら確認に回す。どちらにするかは実装 PoC で決定(未決事項)。
- バリデーション: 出力を Pydantic 等(または手書き)で検証。過去日時、極端に遠い未来(>2 年)、不正な RRULE、タイトル空は拒否または確認に回す。RRULE は `rrulestr` で構文検証してから採用。

### 5.3 曖昧さの扱い

| 表現 | 扱い |
|---|---|
| 「来週」(曜日なし) | 日付を確定せず候補(月曜〜日曜)をボタンで提示、または TODO(期限=来週末)として登録するか尋ねる |
| 「夜」「朝」「昼」 | 既定マッピング(朝 8:00 / 昼 12:00 / 夕方 17:00 / 夜 19:00)を設定で持ち、プレビューに明示。確定は確認ボタン |
| 「15時」午前午後 | 日本語は 24 時間表記が一般的なので 15 時=15:00。「3時」は午前/午後の候補を提示 |
| 「来週火曜」 | 発話日が週のどこでも「次の週の火曜」。日曜始まり/月曜始まり問題は設定(既定: 月曜始まり)。プレビューに具体日付(11/10(火))を必ず出す |
| 「月末」「第3金曜」 | RRULE/相対計算で表現可能かを検証。不可なら確認 |
| 期間不明 | 既定 1 時間(設定可)。終日判定は時刻の有無 |

### 5.4 誤登録防止

- **自然言語経路は常に「プレビュー → 確認ボタン」**。解釈結果を Embed(具体的な年月日・曜日・時刻・繰り返し・リマインド)で表示し、「登録」を押すまで保存しない。
- 「修正」はモーダルで項目別に編集。
- 登録後 5 分以内は「元に戻す(削除)」ボタン。
- `confidence` が低い/`ambiguities` あり → 登録ボタンを無効化し候補選択を先に要求。
- 1 日あたりの LLM 呼び出し上限(既定 100)をかけ、暴走・コストを防ぐ。

### 5.5 コスト見積り

月 100〜300 回の入力として、入力 約 600 tok(システム+日時コンテキスト+文)、出力 約 150 tok。月の合計は入力 〜0.2M、出力 〜0.05M tok で、ニュース要約と同オーダー(数十〜数百円以内の見込み。単価は公式料金表で要確認)。

## 6. ダイジェストとニュースの統合

### 6.1 毎朝ダイジェストの内容

今日の予定(時刻順)、終日予定、期限が今日/超過の TODO、祝日名、今日これから通知されるリマインド数。空の日は「予定なし」を短く出す(出さない設定も可)。

### 6.2 選択肢

| 案 | 内容 | 長所 | 短所 |
|---|---|---|---|
| A 別投稿(推奨) | ダイジェストを先に(6:55 前後または 7:00)、ニュースを 7:00 に別メッセージで投稿 | 失敗が独立(ニュースの API 障害が予定に影響しない)、実装が単純、既存ニュース機能を壊さない、ダイジェスト時刻を個別に設定可 | 通知が 2 件 |
| B 統合 1 投稿 | 1 メッセージの先頭に予定、後続にニュース | 通知 1 件 | 文字数/Embed 数上限(ニュース 10 件で既にほぼ上限)に干渉、障害が連動 |
| C ニュースはスレッド | ダイジェスト投稿のスレッドにニュース | 整理される | スレッド権限・Intents 追加、複雑 |

推奨: A。順序は「ダイジェスト → ニュース」(予定の方が重要度が高い)。時刻は設定可能(既定ダイジェスト 06:55、ニュース 07:00)。ニュース生成は数十秒〜数分かかり得るため、投稿時刻を 07:00 ちょうどにしたければ事前に生成して待機する案もあるが、現行も cron 遅延があり厳密さは求められていないので「07:00 に開始」で十分。

## 7. 外部カレンダー連携(S12)

| 案 | 内容 | 認証/セキュリティ | 実装コスト | 価値 |
|---|---|---|---|---|
| 連携しない(推奨の出発点) | Bot が唯一の真実 | なし | 0 | スマホのカレンダーアプリには出ない |
| iCal 取り込み(読み取り) | Google カレンダー等の「非公開 iCal URL(秘密のアドレス)」を定期取得し、予定として取り込み/通知対象にする | URL が秘密(知っている人は誰でも閲覧可)。Secrets 扱い、ログ出力禁止、HTTPS のみ。OAuth 不要 | 小〜中(`icalendar` 等の要確認、RRULE/EXDATE/タイムゾーンの取り込みが論点) | 他人が作る招待予定や既存予定の通知を一元化 |
| iCal 配信(書き出し) | Bot の予定を `.ics` として配信しスマホカレンダーが購読 | 公開 URL にトークン付き。常駐 Bot に HTTP サーバーが必要(攻撃面増) | 中 | スマホで閲覧可 |
| Google カレンダー双方向同期 | OAuth(リフレッシュトークン保管)、変更の競合解決、同期トークン | リフレッシュトークンという強い秘密を保管。スコープ最小化・暗号化・失効手順が必要。Google 側の審査/テストユーザー制約(個人利用の「テスト」状態ではトークン期限が短い等、要確認) | 大 | 完全な相互運用 |

推奨: **第 1 段階は連携しない。必要になったら「iCal 取り込み(読み取り)」まで**。双方向同期は個人 1 人の用途では費用対効果が低く、秘密情報の保管リスクが増えるため推奨しない。ユーザーの判断事項(未決事項 U2)。取り込み方針: 取り込み予定は `source='ical:<id>'` + `external_uid` で冪等に upsert し、取り込み元の削除は論理削除、Bot 側の編集は取り込み予定には許可しない(読み取り専用)。

## 8. セキュリティ

- **許可ユーザー限定**: `OWNER_USER_ID`(環境変数、複数可なら `ALLOWED_USER_IDS`)。全スラッシュコマンドとボタン/モーダルのコールバックで `interaction.user.id` を検証(`app_commands.check` / `interaction_check` を Cog 基底で一括適用)。不許可は ephemeral で拒否+警告ログ(メッセージ内容は記録しない)。通知ボタンも他人が押せないよう検証する(サーバーに他人がいる場合に備える)。
- **コマンドの可視範囲**: `default_permissions` と `guild_only`、サーバー単位(guild)コマンド登録で他サーバーに出さない。Bot を他サーバーに招待できないよう「Public Bot」をオフ(Developer Portal の設定。名称・手順は要確認)。
- **トークン**: `DISCORD_BOT_TOKEN` は環境変数/Secrets のみ。コード・ログ・例外・`/status` 出力に出さない(Config の `repr=False` 方針を踏襲)。漏洩時はポータルで Reset Token。discord.py のログに URL/トークンが出ない設定を確認(要確認)。
- **Intents**: 特権 Intents は不要(Message Content / Members / Presence を使わない)。スラッシュコマンドとインタラクションのみ。自然言語も `/ask` の引数で受け取り、チャンネルの発言を監視しない。
- **個人情報**: 予定・TODO は DB に平文保存。ディスク暗号化(ホスト側)とファイル権限 600。バックアップも同様に扱う。LLM へは予定の本文を送らない(`/ask` で本人が入力した文のみ。ダイジェスト生成やニュース要約に予定内容を混ぜない)。Anthropic への送信範囲を README に明記。ログには予定タイトルを出さない(ID のみ)。
- **入力の扱い**: LLM 出力は信頼せずスキーマ検証。Embed に入れる文字列はメンション無効化(`allowed_mentions`)とマークダウン/リンクのエスケープ。SQL はパラメータ化のみ。
- **レート/コスト保護**: `/ask` に日次上限。コマンドごとのクールダウン。
- **依存**: Dependabot 継続。`discord.py`・`python-dateutil`・`holidays`・(必要なら)`icalendar` を追加。固定範囲で指定。
- **通知先**: 個人サーバーの専用チャンネル(他人に閲覧されない権限設定を推奨)か DM。DM は Bot からの DM 開始に制約があり得る(要確認)。

## 9. バックアップ

- 日次: SQLite の backup API(または `VACUUM INTO`)で整合的なコピーを作り、世代管理(日次 14 世代+月次 6 世代)。単純な `cp` は WAL 中に不整合になり得るため使わない。
- オフサイト: ホスティングに応じて (a) S3 互換ストレージ/B2 へ暗号化アップロード、(b) 別 PC へ rsync、(c) 非公開 GitHub リポジトリへの暗号化コミット(非推奨: 履歴に残る)。推奨は (a) か (b)。
- エクスポート: `/export` コマンドで JSON/ICS を ephemeral 添付で出力(最終手段のバックアップ兼ロックイン回避)。
- 復元手順を `docs/operations.md`(docs エージェントが更新)に書き、復元テストを品質ゲートに含める。
- 自己監視: 最終バックアップ成功時刻を `/status` に表示。48 時間失敗が続いたら管理者通知。

## 10. テスト戦略

- **時刻**: 全ロジックが `now`(aware)と `tz` を引数/依存注入で受ける(`Clock` Protocol)。`datetime.now()` 直呼びを禁止(ruff のカスタム規則は無いので grep テストまたはレビューで担保)。`freezegun` / `time-machine` の利用は任意(要確認、導入は不要かもしれない)。
- **スケジューラ**: `dispatch_due(now, repo, sender)` を同期/async の純関数的に分け、FakeSender で検証: (1) 期限到来のみ送る、(2) 二重実行しても 1 回、(3) `sending` 残骸の復旧、(4) 遅延閾値による `late`/サマリー化、(5) 失敗時のバックオフと `failed` 遷移、(6) ジョブ記録の一意性。ループ(`tasks.loop`)自体は薄いのでスモークのみ。
- **繰り返し**: プロパティ的に境界を網羅: 月末、うるう日、年跨ぎ、DST のあるタイムゾーン(`America/New_York`)での壁時計維持、`UNTIL`、`EXDATE` 相当の例外、祝日スキップ。
- **自然言語**: LLM は Fake クライアントでモックし、(a) JSON 検証、(b) 曖昧時に登録ボタン無効、(c) 検算(曜日不一致→確認)、(d) インジェクション文を含む入力でも出力検証を通ること。代表 30〜50 文の評価セット(期待値の日時を固定 `now` で比較)を用意し、実 API での評価は手動・任意(CI では回さない)。
- **Discord**: コマンドのロジックは Cog から分離したサービス層に置き、サービス層を単体テスト。Cog は薄いので `discord.Interaction` を `MagicMock`/`AsyncMock` で代用した最小のハンドラテストのみ。`dpytest` 等の利用は **要確認**(メンテ状況)。Embed 生成(純関数)は文字数上限含めてテスト。
- **DB**: インメモリ SQLite でマイグレーション適用テスト、UNIQUE による重複防止、論理削除。
- **品質ゲート**: ruff + pytest を CI で継続。カバレッジ目標は設けないが、スケジューラ/繰り返し/冪等性は必須網羅。

## 未決事項

| # | 論点 | 選択肢とトレードオフ | 推奨 |
|---|---|---|---|
| **U2** | **外部カレンダー連携の範囲(ユーザー判断)** | 連携しない / iCal 読み取り / iCal 配信 / Google 双方向 | まず連携しない。必要なら iCal 読み取り。双方向は不推奨 |
| **U3** | **通知の宛先(ユーザー判断)** | 専用チャンネル+本人メンション / DM / 両方 | 専用チャンネル(履歴が残る、ボタンが使える)。DM はオプション |
| S1 | 保存先 | SQLite / PostgreSQL | SQLite(WAL) |
| S2 | スケジューラ | tasks.loop+DB ポーリング / APScheduler | DB 駆動ポーリング(30 秒) |
| S3 | 自然言語 | Claude 構造化抽出 / dateparser / ハイブリッド | Claude 本線+厳密形式の `/add` を併設 |
| S4 | 相対日付の計算主体 | LLM が日付を返し検算 / 構造で返しコードが確定 | 構造で返しコードが確定(PoC で検証) |
| S5 | ダイジェストとニュース | 別投稿 / 統合 | 別投稿(ダイジェスト → ニュース) |
| S6 | 祝日データ | `holidays` / 内閣府 CSV | `holidays`(精度を要確認) |
| S7 | 繰り返しの例外(この回のみ) | 初期から / 後フェーズ | 後フェーズ(まず「すべて」のみ) |
| S8 | 週の開始曜日・時間帯語の定義 | 月曜始まり / 日曜始まり。朝8/昼12/夕17/夜19 | 月曜始まり、上記マッピング、設定可 |
| S9 | 遅延通知の猶予 | 10 分 / 30 分 | 10 分(設定可) |
| S10 | 日本語コマンド名 | 英語名 / 日本語名 | 英語名(日本語の可否は要確認) |
| S11 | TODO のリマインド格納 | `notifications` に統合 / 別表 | 実装時に決定(統合を第一候補) |
| S12 | `rrule_until` と UNTIL の二重管理 | RRULE に統一 / 別カラム | RRULE に統一(UTC) |
