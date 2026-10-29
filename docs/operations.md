# 運用手順

## ソースを追加・削除する

1. `src/nomusyo/core/sources.py` の `SOURCES` に `Source("表示名", "フィードURL", "general" または "tech")` を追加(削除は行を消す)。
2. 追加前に、そのURLが実際に取得できる RSS/Atom か確認する。
3. 並び順が優先順(重複時に先のものが残る)。
4. `pytest` と `ruff check .` を実行し、コミット・push する。

## モデルを変更する

1. 環境変数 `SUMMARY_MODEL` を変える。
   - Actions: `.github/workflows/daily-news.yml` の `Run` ステップの `env:` に `SUMMARY_MODEL: <モデルID>` を追記。
   - ローカル: 実行時に環境変数で渡す。
2. 変更後は `dry_run=true` で手動実行する(`ANTHROPIC_API_KEY` が設定されていれば実際に要約が走る)。モデル ID が不正だと恒久エラーとなり exit 1 になる。

既定値 `claude-haiku-5-5` の実在は初回の手動実行で確認してください。

## 実行タイミング

- cron は `0 22 * * *`(22:00 UTC = 07:00 JST)。GitHub の負荷で数分〜数十分遅れることがあり、7:00 ちょうどは保証されない。
- 公開リポジトリは60日間リポジトリの活動がないと、cron が自動で無効化される。止まっていたら Actions タブから再有効化するか、push / 手動実行(`workflow_dispatch`)で再開する。
- 実行が失敗しても自動再実行はされない。Actions から手動実行する。
- cron はデフォルトブランチ上のワークフローだけが対象。

## トラブルシュート

まず Actions のログを開き、失敗したステップのエラーを見る。

| 症状・ログ | 原因と対処 |
|---|---|
| `Configuration error: Missing required environment variable(s): ...` | 該当 Secret が未登録/名前違い。Settings → Secrets を確認 |
| `Configuration error: ... must be ...` | 環境変数の値が不正(数値/比率/真偽値)。値を直す |
| `no articles collected` | 全ソース取得失敗、または全記事が `NEWS_MAX_AGE_HOURS` より古い。各ソースの警告ログとフィードURLを確認 |
| `summary API permanent error (AuthenticationError ...)` | `ANTHROPIC_API_KEY` が無効。キーを作り直して Secret を更新 |
| `summary API permanent error (NotFoundError ...)` | モデル ID が存在しない。`SUMMARY_MODEL` を確認 |
| `PermissionDeniedError` / `BadRequestError` | キーの権限、アカウントの残高/課金設定、リクエスト内容を確認 |
| 投稿は届いたが「要約に失敗した」注記が付く | 一時的な API 障害など。次回実行で直ることが多い。続くなら上記のエラーを確認 |
| `Discord webhook rejected the request (HTTP 401/404)` | Webhook URL が削除/無効。作り直して Secret を更新 |
| `Discord webhook ... failed after retries` / `read timed out (not retried)` | Discord 側の障害や rate limit。読み取りタイムアウトは二重投稿防止のため再試行しない(届いていないか Discord を確認し、必要なら手動実行) |
| 朝に投稿が来ない | 遅延の可能性。Actions タブで実行を確認。60日無活動で cron が無効化されていないか確認 |

## 開発コマンド

```sh
pip install -e '.[dev]'
pytest
ruff check .
ruff format .   # 整形
python -m nomusyo.main --dry-run
```

## 常駐 Bot の運用

状態: フェーズ1(`/news`・`/status`・Docker 構成・死活監視 ping)と、フェーズ2(予定・TODO・`/today` `/week`・朝のダイジェスト・`/settings`)を実装済み。ただし `docker compose up` の実行、Developer Portal の画面、実 Discord への接続は**未確認**。定時ニュースの Bot 移行とリマインド通知は未実装のため、その手順は含まない。方針は `docs/design/resident-bot.md`、決定は `docs/design/decisions.md`、タスク進捗は `docs/design/tasks-v2.md`。上記の Actions 側(ニュース投稿)の手順は引き続き有効。

ユーザー作業: ホスティング契約、Developer Portal での Bot 作成(`docs/setup-discord-bot.md`)、Bot の招待、`.env` の作成、デプロイ、死活監視サービスの設定。

### Bot の招待

1. Developer Portal の「OAuth2」→「URL Generator」で、スコープに **`bot`** と **`applications.commands`** の両方を選ぶ。
2. Bot の権限は次の 3 つだけを選ぶ(他は付けない)。
   - View Channel(チャンネルを見る)
   - Send Messages(メッセージを送信)
   - Embed Links(埋め込みリンク)
3. 生成された URL を開き、対象サーバーを選んで招待する。
4. **管理者(Administrator)権限では招待しない。** 権限が広すぎると、Bot が乗っ取られた場合の被害が大きくなる。
5. 招待後、Bot が通知チャンネルに投稿できるかを確認する(`/settings` の通知チャンネル選択でも確認される)。

### 設定(環境変数)

`.env.example` と `src/nomusyo/bot/config.py` に基づく。`.env` は `docker-compose.yml` の `env_file` で読み込まれる。

| 変数 | 必須 | 既定値 | 内容 |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | 必須 | なし | Bot トークン(秘匿) |
| `DISCORD_GUILD_ID` | 必須 | なし | コマンドを登録するサーバー ID。ほかのサーバーに入ると Bot は自動退出する |
| `ALLOWED_USER_IDS` | 必須 | なし | 操作を許可するユーザー ID(カンマ区切り、1 件以上)。先頭のユーザーが owner |
| `NOTIFY_CHANNEL_ID` | 必須 | なし | ダイジェスト等の投稿先チャンネル ID(`/settings` で変更すると上書き) |
| `ANTHROPIC_API_KEY` | 必須 | なし | `/news` の要約用(秘匿) |
| `ADMIN_USER_ID` | 任意 | `ALLOWED_USER_IDS` の先頭 | 障害通知の DM 宛先 |
| `ADMIN_CHANNEL_ID` | 任意 | なし | 読み込みのみ。現状の障害通知は DM で送られ、このチャンネルには送られない(未使用) |
| `BOT_HEARTBEAT_URL` | 任意 | なし | 死活監視の ping 先。`https://` のみ(秘匿)。未設定なら ping しない |
| `DB_PATH` | 任意 | `/data/nomusyo.db` | SQLite の DB ファイル。親ディレクトリが必要(下記「DB とバックアップ」) |
| `TZ_DEFAULT` | 任意 | `Asia/Tokyo` | 予定・TODO・ダイジェストの既定タイムゾーン(IANA 名) |
| `DIGEST_TIME` | 任意 | `06:55` | 朝のダイジェストの時刻(`HH:MM`、`TZ_DEFAULT` 基準)。`/settings` で上書き可 |
| `NEWS_POST_TIME` | 任意 | `07:00` | 定時ニュースの時刻。読み込みのみで、投稿ループは未実装(V2-70) |
| `NEWS_POST_ENABLED` | 任意 | `false` | 定時ニュースを Bot から投稿するか。読み込みのみで、投稿ループは未実装(V2-70)。現状の定時ニュースは Actions の cron が投稿する |
| `SUMMARY_MODEL` / `NEWS_TOTAL_COUNT` / `NEWS_GENERAL_RATIO` / `NEWS_MAX_AGE_HOURS` | 任意 | 各既定値 | ニュース側と共通(`README.md` の設定表を参照) |
| `LOG_LEVEL` | 任意 | `INFO` | ログレベル |

`DISCORD_WEBHOOK_URL` は Bot では不要。

### 許可ユーザーの前提(個人用)

- この Bot は**個人用**です。`ALLOWED_USER_IDS` に複数人を入れると、全員が先頭ユーザー(owner)の予定・TODO・設定を操作できます(データは owner 単位で保存されるため)。
- 他人を入れる場合は、そのユーザーに自分の予定が見えることを理解したうえで設定してください。
- 不許可のユーザーには拒否メッセージだけが返り、ボタンやモーダルも同じ検査を通ります。

### コマンド

コマンド一覧と入力例は `README.md` の「常駐 Bot(Discord コマンド)」を参照してください。

- ギルド限定。`ALLOWED_USER_IDS` 以外のユーザーは拒否される。
- 応答はすべて本人にのみ見える(ephemeral)。
- `DISCORD_GUILD_ID` 以外のサーバーに入ると、Bot は自動退出する(実装済み。実機での確認は未)。

### DB とバックアップ

1. 既定の DB は `/data/nomusyo.db`。コンテナでは named volume `bot-data` が `/data` にマウントされる。
2. コンテナのルート FS は読み取り専用(`read_only: true`)。書き込めるのは `/data`(volume)と `/tmp`(tmpfs)だけ。`DB_PATH` を別の場所にすると、起動時に DB を開けず終了する。
3. bind mount で `/data` をホストのディレクトリに置く場合、そのディレクトリを **uid 10001**(コンテナ内の `bot` ユーザー)が書き込める必要がある。例: `sudo chown -R 10001:10001 /path/to/data`。
4. 同じ DB ファイルに対して 2 つの Bot を同時に起動しない。単一インスタンスのロックは未実装(V2-21)。
5. マイグレーションの前に、`<DB のあるディレクトリ>/backups/` へ `pre-migrate-v<版>-<時刻>.db` としてバックアップが作られる(直近 5 世代を残す)。
6. 復元手順(DB の置き換え、オフサイトへの退避)は未整備です(V2-93, V2-94)。手動で行う場合は、Bot を止めてから DB とその `-wal` / `-shm` ファイルを一緒に扱うこと。

### デプロイ(初回)

前提: 小型 VPS 等で Docker と Docker Compose が使える(契約・OS 準備はユーザー作業)。

1. ホストにリポジトリを取得する(`git clone`)。
2. `.env.example` を `.env` にコピーして値を入れる。権限を 600 にする。コミットしない。
3. 必要な値は上の表を参照。
4. 起動: `docker compose up -d --build`(compose のサービス名は `bot`。実行は未確認)。
5. `docker compose logs` で起動を確認し、Discord で `/status` を実行して応答を確かめる(未確認)。

構成(`docker-compose.yml` / `Dockerfile`): `python:3.11-slim`、非 root ユーザー(uid 10001)、起動コマンドは `python -m nomusyo.bot`、`restart: unless-stopped`、`TZ=Asia/Tokyo`、読み取り専用ルート FS(`/tmp` は tmpfs)、`cap_drop: ALL`、`no-new-privileges`、ボリューム `bot-data` を `/data` にマウント、ログは json-file(`max-size: 10m`、`max-file: 5`)。DB は `/data` に書き込む(上記「DB とバックアップ」)。

### 更新(デプロイ)

1. ホストで `git pull` する。
2. `docker compose up -d --build` で再作成する。旧プロセスが残っていないか確認する。単一インスタンスのロックは未実装のため、旧コンテナと新コンテナを同時に動かさない(`docker compose ps` で 1 つだけになっていることを確認)。
3. 起動後にログと `/status` の応答を確認する。

### ログ確認

1. `docker compose logs --tail=200 bot`。追従は `-f`。
2. ログは標準出力。ローテーションは compose で設定済み(上記)。
3. トークンは出さない設計。出ていたら不具合として扱い、トークンは再発行する。

### 再起動・停止

1. 再起動: `docker compose restart`。停止: `docker compose stop`。
2. `restart: unless-stopped` を設定済み。ホスト再起動後に自動で上がるかは未確認。
3. 同じトークンで 2 プロセスを動かさない。別の場所で試験する場合は先に本番を止める。

### トークンの再発行

1. Developer Portal の Bot ページで「Reset Token」(`docs/setup-discord-bot.md` 3 章)。
2. ホストの `.env` の `DISCORD_BOT_TOKEN` を更新する。
3. Bot を再起動し、オンラインになることを確認する。
4. 古いトークンは無効になる。漏えいが疑われる場合はログ・履歴に残っていないかも確認する。

### 死活監視

目的: Bot が黙って停止したことに気付く。方式は外形監視(Dead man's switch)。`BOT_HEARTBEAT_URL` を設定した場合のみ、Bot が 5 分おきにその URL へ ping する(`cogs/heartbeat.py`)。未設定なら ping しない。

1. Healthchecks.io 等のサービスでチェックを作成する(ユーザー作業。サービスの仕様・無料枠は要確認)。
2. 期待する ping 間隔は 5 分より長く設定し、猶予(grace time)も設定する。
3. 通知先(メール/プッシュ。Discord が落ちても届くよう Discord 以外を含める)を設定する。
4. 発行された ping URL を `.env` の `BOT_HEARTBEAT_URL` に設定する。URL は秘匿扱い。
5. Bot を再起動し、サービス側で ping が届いていることを確認する(未確認)。
6. 動作テスト: Bot を一時停止し、猶予時間後に通知が来ることを確認し、再開で復旧通知が来ることを確認する。
7. Bot 内の障害通知(`ADMIN_USER_ID` への DM)は Discord 稼働が前提のため、上記サービスの通知と併用する。

### 障害対応

1. Bot がオフライン: 死活監視の通知を確認 → ホストにログイン → `docker compose ps` とログを確認 → 再起動。
2. ログ上の認証エラー: トークンが無効の可能性。再発行する(上記)。
3. 起動直後に設定エラーで終了: 必須の環境変数の不足/不正。ログの項目名を見て `.env` を直す。
4. ニュースが二重に届く: Actions の cron と `/news` の手動実行は別系統。定時投稿を Bot に移す機能は未実装のため、Actions の cron を止める必要は現状ない。
5. DB が開けずに終了: `DB_PATH` の親ディレクトリが存在するか、uid 10001 が書き込めるかを確認する(上記「DB とバックアップ」)。ログには例外の型名だけが出る。
6. DB・バックアップ・復元: マイグレーション前のバックアップは自動で作られる。復元手順は未整備(V2-93, V2-94)。

### コストの目安

要確認。設計文書の目安は VPS が月 500〜1,500 円程度、Fly.io が月数百円〜、決定記録の予算は月千円前後。実額は契約時にホスティング事業者の料金表で確認する。Anthropic API の利用料(ニュース要約)は別途かかる。
