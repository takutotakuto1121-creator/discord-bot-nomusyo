# 常駐 Bot 化アーキテクチャ

状態: 設計(未実装)。`overview.md` の「将来の常駐 Bot 化の方針」を具体化する。機能は `schedule.md` / `roadmap.md`、タスクは `tasks-v2.md`。
discord.py・Discord・各ホスティングの仕様で確信が持てない点は「要確認」。

## 1. 方針

- 既存の `core/`(Discord 非依存)と `discord_io/formatter.py` は変更せず再利用する。`/news` と定時ニュースは `core.pipeline.run_pipeline(config)` を呼ぶだけ。
- 追加するもの: `bot/`(discord.py の Cog 群)、`schedule/`(スケジュール機能のドメイン層。Discord 非依存)、`storage/`(SQLite)。依存方向は `bot → schedule/core/discord_io/storage`、`schedule/core → discord を import しない`。
- 常駐プロセスは 1 つ(単一インスタンス)。複数起動は設計上禁止(5.3 の単一インスタンス保護)。

## 2. 構成

```
src/nomusyo/
├── core/               # 既存。変更しない(ニュース取得〜要約)
├── discord_io/         # 既存。formatter(Embed 整形)・webhook(Actions 用)
├── config.py           # 既存。Bot 用設定は別クラス BotConfig を追加(既存 Config は触らない)
├── main.py             # 既存。Actions 用エントリ(移行完了までそのまま)
├── schedule/           # 新規。Discord 非依存のドメイン層
│   ├── models.py       # Event, Todo, ReminderRule, Notification
│   ├── recurrence.py   # RRULE 展開・祝日・例外
│   ├── planner.py      # 通知の先行生成(materialize)
│   ├── dispatcher.py   # dispatch_due(now, repo, sender)
│   ├── nlp.py          # 自然言語 → 構造化(LLM はインターフェース越し)
│   └── clock.py        # Clock Protocol
├── storage/            # 新規。SQLite リポジトリ+マイグレーション(migrations/*.sql)
├── bot/                # 新規。discord.py 依存はここだけ(+ discord_io)
│   ├── __main__.py     # python -m nomusyo.bot
│   ├── app.py          # Bot サブクラス、setup_hook、Cog 読み込み、コマンド同期
│   ├── checks.py       # 許可ユーザー検証
│   ├── views.py        # 永続 View(完了/スヌーズ)、確認 View
│   ├── embeds.py       # 予定・TODO の Embed 生成
│   └── cogs/
│       ├── news.py       # /news、定時ニュース
│       ├── schedule.py   # /add /list /today /week /edit /delete
│       ├── todo.py       # /todo ...
│       ├── digest.py     # 朝のダイジェスト
│       ├── notifier.py   # 通知ディスパッチループ(tasks.loop)
│       ├── settings.py   # /settings
│       └── admin.py      # /status /export
└── ...
```

### 2.1 Cog 設計の要点

- 1 Cog = 1 責務。Cog は薄く、ロジックはサービス層(`schedule/`、`core/`)に置く。
- `NewsCog`: `/news` は `await interaction.response.defer()` → `await asyncio.to_thread(run_pipeline, config)` → `format_payloads(articles)` が返す Embed の dict を `discord.Embed.from_dict` で変換して `followup.send`(Embed 10 個/6000 字の分割は `format_payloads` が既にメッセージ単位に分けているので、メッセージごとに送る。`from_dict` の互換性は要確認)。`NoArticlesError` は ephemeral でエラー表示。同時実行は `asyncio.Lock` で 1 件に制限、手動 `/news` の連打にはクールダウン(例 5 分)と日次上限。
- 定時ニュース: `NewsCog` 内の `tasks.loop(time=...)`(JST 07:00 の tz-aware `datetime.time`。タイムゾーン付き `time` の扱いは **要確認**)。実行前に `job_runs` で `daily_news` を取得(INSERT 成功時のみ実行)。`summary_error` 属性(`articles.summary_error`)がある場合は管理者通知(現行 `main.py` の exit 1 相当)。
- `to_thread` は GIL 下でも IO 待ちが主なので十分。長時間の同期処理で heartbeat が途絶えないよう、イベントループ上でブロッキング呼び出しをしない(Bot 切断の原因)。
- 起動時エラー(設定不備)は起動を即失敗させる(`BotConfig` のバリデーション、既存 `config.py` の方式を踏襲)。

### 2.2 設定クラス

既存 `Config`(Webhook 用)は変更せず、`BotConfig` を別に用意し、ニュース部分は既存 `Config` を生成して `run_pipeline` に渡す。`DISCORD_WEBHOOK_URL` は Bot 経由投稿では不要になるが、`Config` 側の必須チェックとの整合に注意(`load_config` は dry_run でない限り Webhook を必須とするため、Bot 起動時は専用の組み立て関数 `build_news_config()` を作るか、`load_config` に `require_webhook` 引数を追加する。**既存挙動を変えない形で**行う。developer が判断、レビュー対象)。

## 3. 設定 / Secrets

| 変数 | 必須 | 説明 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | 必須 | Bot トークン(秘匿、ログ出力禁止、`repr=False`) |
| `DISCORD_GUILD_ID` | 必須 | コマンドを登録するサーバー ID(ギルドコマンドは反映が速い) |
| `OWNER_USER_ID` / `ALLOWED_USER_IDS` | 必須 | 操作を許可するユーザー ID |
| `NOTIFY_CHANNEL_ID` | 必須 | 通知・ダイジェスト・ニュースの投稿先 |
| `ADMIN_CHANNEL_ID` または `ADMIN_DM` | 任意 | 障害通知先(既定: 所有者 DM) |
| `ANTHROPIC_API_KEY`、`SUMMARY_MODEL`、`NEWS_*` | 既存 | ニュース/自然言語に使用 |
| `NL_MODEL` | 任意 | 自然言語抽出モデル(既定は `SUMMARY_MODEL` と同じ) |
| `DB_PATH` | 任意 | 既定 `/data/nomusyo.db`(コンテナ内のボリューム) |
| `TZ_DEFAULT` | 任意 | 既定 `Asia/Tokyo` |
| `NEWS_POST_ENABLED` | 任意 | 定時ニュースの Bot 投稿を有効化(移行用、既定 `false`) |
| `NEWS_POST_TIME`、`DIGEST_TIME` | 任意 | 既定 `07:00` / `06:55`(`settings` テーブルで上書き可) |
| `HEALTHCHECK_URL` | 任意 | 死活監視 ping 先(秘匿扱い) |
| `LOG_LEVEL` | 任意 | 既存 |

- 保管: 自宅/VPS は `.env`(権限 600、コミット禁止)または systemd の `EnvironmentFile` / Docker secrets。Fly.io は `fly secrets set`(要確認)。GitHub Secrets は Actions 側のみ(移行後は Actions を止めるため `DISCORD_WEBHOOK_URL` を残すかは 4 章)。
- `.env.example`(値は空)を用意しコミットする。
- ログに環境変数ダンプを出さない。discord.py のロガー設定で HTTP 詳細を DEBUG に落とし、トークンが出ないことをテストで確認する。

## 4. ニュース投稿の移行計画(二重投稿防止)

現状: GitHub Actions cron(22:00 UTC)→ Webhook。Bot でも定時投稿を行うと二重投稿になるため、段階移行する。

| 段階 | 状態 | Actions cron | Bot の定時ニュース | 確認 |
|---|---|---|---|---|
| M0 | 現状 | 有効 | なし | - |
| M1 | Bot デプロイ(`/news` のみ、テスト用チャンネル) | 有効 | 無効(`NEWS_POST_ENABLED=false`) | `/news` が動く、定時は Actions のまま |
| M2 | Bot の定時ニュースを **dry-run / テストチャンネル**で数日試験 | 有効 | テスト用チャンネルへ(本番チャンネルには出さない) | 7:00 台に発火、`job_runs` 記録、`late` なし |
| M3 | **切替日**: Actions の `schedule:` を削除(または `workflow_dispatch` のみに)して push → Bot の `NEWS_POST_ENABLED=true`、本番チャンネルへ | 停止(手動のみ) | 有効 | 翌朝 1 通だけ届く |
| M4 | 安定運用 1〜2 週間後、Webhook Secret の削除可否を判断 | 手動フォールバックとして残す | 有効 | - |

- 切替の順序: **先に Actions の cron を止める → その後に Bot の定時を有効化**(逆だと切替当日に二重投稿)。切替は前日夜に行い、当朝は Bot のみが投稿する状態にしておく。cron は main ブランチ上のワークフローのみ有効、という既知の挙動に注意(運用文書 `docs/operations.md`)。
- 冪等性: Bot 内は `job_runs (daily_news, 日付)`。Bot と Actions 間は DB を共有できないので、上記の「手順で排他」とする。補助策として、Bot が当日の投稿前にチャンネルの直近メッセージを確認し、同日のニュース投稿(Webhook 由来を判別するフッター識別子)があればスキップする案がある。Message Content Intent や履歴読み取り権限が必要になるため(要確認)、**採用しない**(特権 Intent を避ける方針を優先)。
- ロールバック: Bot 障害時は `NEWS_POST_ENABLED=false` にして Actions の `schedule:` を復活させる(ワークフローの schedule 行を Git で戻すだけ)。切替 PR をそのまま revert できるように、切替は単独コミットにする。
- 投稿経路: Bot 経由は `channel.send(embeds=...)`。Webhook 投稿と見た目を揃えるため同じ `format_payloads` を使う。ユーザー名/アイコン(Webhook では指定可)が Bot 名義になる差異あり。
- 失敗時: Actions は exit 1 で GitHub 通知が来た。Bot では障害通知を管理者チャンネルまたは DM に送る(6 章)。
- 併存案(切替しない): Actions を完全に残し Bot は予定専用にする選択肢もある。メリット: ニュースは Bot 障害の影響を受けない。デメリット: `/news` と定時の実装が分離し続ける(ただし `core` は共通)。未決事項 U4 参照。

## 5. ホスティング

### 5.1 比較

| 選択肢 | 初期/月額コスト(目安・要確認) | 運用負荷 | 可用性 | 備考 |
|---|---|---|---|---|
| 自宅 PC(常時稼働 Mac 等) | 電気代のみ | 中(スリープ/OS 更新/再起動で停止) | 低〜中(停電・スリープ・外出時に不安) | 開発機を兼ねると事故りやすい。launchd で自動起動 |
| Raspberry Pi(自宅) | 本体 5〜1 万円+電気代わずか | 中(SD カード故障対策、バックアップ必須) | 中(家のネット/停電依存) | SD 劣化対策に SSD。SQLite 運用に適する |
| VPS(国内/海外の小型) | 月 500〜1,500 円程度 | 低〜中(OS 更新、SSH 管理、ファイアウォール) | 高 | Docker Compose+systemd。固定費。構成が単純で SQLite もそのまま |
| Fly.io | 小さな VM+Volume で月数百円〜(無料枠の条件は変動。要確認) | 低(Dockerfile+`fly deploy`) | 高(ただし Volume は単一リージョン/単一ホストに紐づく。要確認) | SQLite は Volume 必須。1 Machine に固定(複数台にしない) |
| Railway / Render 等 PaaS | 月数百円〜数千円(要確認) | 低 | 中〜高 | 永続ディスクの有無・課金体系を要確認。無料枠はスリープする場合あり |
| サーバーレス(Lambda 等) | - | - | - | 常駐 WebSocket(Gateway)が必要なため不向き。Interactions Webhook 方式なら可能だが設計が別物になる。対象外 |

### 5.2 推奨

優先順: **(1) 小型 VPS + Docker Compose**(予測可能な固定費、SQLite がそのまま、運用が単純)、**(2) Fly.io**(OS 管理を避けたい場合。Volume 1 つ+Machine 1 台に固定)、**(3) 既に Raspberry Pi があれば自宅運用**。自宅 PC は停止リスクが大きく、通知が命のスケジュール機能には非推奨。最終決定はユーザー(未決事項 U1)。決定前に実装を進められるよう、Docker 化と環境変数のみに依存する構成にし、ホスト固有コードを入れない。

### 5.3 単一インスタンス保護

同じトークンで 2 プロセスが動くと通知が重複する。(a) デプロイ手順で「旧停止 → 新起動」を徹底(Fly は 1 Machine・`--ha=false` 相当。要確認)、(b) SQLite に `instance_lock`(heartbeat 付き行)を持たせ、起動時に他インスタンスの生存を検出したら終了する、(c) 通知のクレーム処理(`UPDATE ... WHERE status='pending'`)で最悪でも二重送信を防ぐ(別 DB の複数インスタンスは防げない点に注意)。

## 6. ログ・監視

- ログ: 標準出力に構造化寄りのテキスト(時刻・レベル・ロガー)。Docker のログドライバ/journald でローテーション(`max-size` 設定)。予定タイトル等の個人情報は出さず ID のみ。
- 死活監視(Bot が黙って死ぬのが最大のリスクなので重視):
  - 外形監視: Healthchecks.io 等の「Dead man's switch」へ数分おきに ping(`tasks.loop` 内、失敗時のみ停止して検知させる)。サービス名・無料枠は要確認。URL は秘匿扱い。
  - Discord 内: 7:00 のダイジェストが届かなければ異常、という運用の目視。
  - プロセス再起動: Docker `restart: unless-stopped` / systemd `Restart=always`。
- 障害通知: (1) 通知送信が `failed` になった、(2) 定時ジョブが失敗/スキップした、(3) 未処理例外(`on_error` / `tree.on_error`)、(4) バックアップ失敗、(5) Gateway 再接続の頻発。送信先は管理者 DM または管理用チャンネル。Discord 自体が落ちている場合は届かないので、Healthchecks のメール/プッシュを併用。
- `/status`: 稼働時間、最終ポーリング時刻、`pending` 件数、直近のジョブ結果、DB サイズ、最終バックアップ時刻、バージョン。
- Gateway 切断: discord.py は自動再接続する。切断中の通知は復帰後に `pending AND fire_at <= now` で拾う(`schedule.md` 4.3)。`on_ready` は複数回呼ばれるため、初期化は `setup_hook` に置く(要確認)。
- メトリクス: 個人用途のため Prometheus 等は不要。

## 7. デプロイ

- Docker: 軽量ベース(`python:3.11-slim` 以降、バージョン固定+ダイジェスト固定は任意)。非 root ユーザー。`/data` を Volume にマウントして DB・バックアップを置く。依存は `pyproject.toml` から `pip install .`(ロックファイル導入は要検討)。
- 起動: `python -m nomusyo.bot`。`HEALTHCHECK` は Docker 組み込みではなく外部 ping を主とする。
- Compose 例(方針): `restart: unless-stopped`、`env_file: .env`、`volumes: ./data:/data`、`TZ=Asia/Tokyo`、ログ `max-size`。
- CI/CD: 既存 `ci.yml`(ruff+pytest)は維持。イメージのビルド確認ジョブを追加。デプロイは (a) 手動(SSH+`git pull && docker compose up -d --build`)から始め、(b) 慣れたら GitHub Actions からのデプロイ(SSH 鍵/flyctl トークンという新しい Secret が増える点に注意)。個人用途では (a) を推奨。
- マイグレーション: 起動時に未適用の SQL を自動適用(適用前に DB のバックアップを取る)。
- 更新時の取りこぼし: 再起動中の数十秒の通知は復帰後に遅延再送される(4.3)ので許容。

## 8. Intents と Bot 権限

- Gateway Intents: `Intents.default()` から特権 3 つ(Presence / Server Members / Message Content)を含まない構成。スラッシュコマンドとボタン/モーダルのインタラクションだけなら特権 Intents は不要。`guilds` 相当(default に含まれる)でチャンネル解決。DM 通知用に `dm_messages` 等は不要(Bot から送るだけ。要確認)。
- OAuth2 スコープ: `bot` と `applications.commands`。
- Bot 権限(最小): チャンネル内で View Channel / Send Messages / Embed Links / (スレッドを使うなら Send Messages in Threads)。Manage Messages、Administrator は付与しない。権限整数は招待 URL 生成時に確認(要確認)。
- チャンネル権限: 通知チャンネルは自分と Bot のみが見える設定を推奨。
- Public Bot をオフ、Requires OAuth2 Code Grant はオフ(Developer Portal の項目名は要確認)。
- コマンド登録: ギルド限定で `tree.sync(guild=...)`。同期は起動毎ではなく、コマンド定義が変わったときのみ(`/admin sync` か起動引数)。レート制限があるため(要確認)。

## 未決事項

| # | 論点 | 選択肢とトレードオフ | 推奨 |
|---|---|---|---|
| **U1** | **ホスティング(ユーザー判断)** | 自宅 PC / Raspberry Pi / VPS / Fly.io / 他 PaaS(5.1 の表) | 小型 VPS+Docker Compose。Pi を既に持っていれば自宅 Pi。次点 Fly.io |
| **U4** | **ニュースを Actions から Bot へ移すか併存か(ユーザー判断)** | 移行(コード一本化、Bot 障害時にニュースも止まる)/ 併存(ニュースは独立、`/news` のみ Bot)| 段階移行 M1〜M4。ただし Bot の安定を 1〜2 週間見るまで Actions を手動フォールバックとして残す |
| **U5** | **月額コストの許容(ユーザー判断)** | 0 円(自宅)/ 数百円(Fly.io)/ 500〜1,500 円(VPS) | 予算次第。通知の信頼性重視なら有料ホスティング |
| R1 | 障害通知先 | 所有者 DM / 管理チャンネル / メール(Healthchecks) | DM+Healthchecks のメール |
| R2 | 通知の投稿方式 | Bot の `channel.send` / Webhook 併用 | Bot の `channel.send`(ボタン付与のため必須) |
| R3 | `load_config` の扱い | 既存関数に引数追加 / Bot 専用の組み立て関数 | Bot 専用の組み立て関数(既存挙動に触れない) |
| R4 | コマンド同期のタイミング | 起動毎 / 変更時のみ | 変更時のみ(管理コマンドか起動フラグ) |
| R5 | イメージのバージョン固定 | タグのみ / ダイジェスト固定 | タグ固定+Dependabot(Docker)で更新 |
| R6 | ロックファイル導入(uv 等) | なし / `uv.lock` | 現状 `pyproject.toml` のまま。再現性が必要になれば導入 |
