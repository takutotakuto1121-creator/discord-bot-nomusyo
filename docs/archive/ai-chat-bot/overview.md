# DesicordBot 全体設計 (overview)

## 1. 目的
Discord サーバー上で Claude API を使った AI 応答・チャットを提供する Bot。ローカル実行を前提とし、後から Docker 化・ホスティング移行がしやすい構成にする。

## 2. スコープ
### 今回やる
- 起動・スラッシュコマンド同期・`/ping`
- Claude API による AI チャット(`/chat`、`/reset` 等。詳細は `ai-chat.md`)
- PostgreSQL への会話履歴・設定・使用量の永続化(非同期)
- DBマイグレーション、設定管理、ロギング
- 最小限のレート制限とコスト対策
- Dockerfile / docker-compose(ローカルDB用。Bot本体のコンテナ化は後続タスクで最小限のみ)

### 今回やらない
- 音声チャンネル、画像生成、ツール使用(function calling)、RAG
- Web ダッシュボード、複数ギルド向けの課金/プラン管理
- シャーディング、本番向け監視基盤(メトリクス基盤)
- メッセージ内容の常時監視(Message Content Intent を要する機能。未決事項参照)

## 3. アーキテクチャ
```mermaid
flowchart LR
  U[Discord User] -->|slash command| D[Discord Gateway]
  D --> B[bot.py: DesicordBot]
  B --> C1[cogs/general.py /ping]
  B --> C2[cogs/ai_chat.py /chat /reset]
  B --> C3[cogs/admin.py 設定系]
  C2 --> S1[services/chat_service.py]
  S1 --> S2[services/claude_client.py]
  S1 --> S3[services/rate_limiter.py]
  S1 --> R[db/repositories]
  S2 -->|HTTPS async| A[(Anthropic API)]
  R --> P[(PostgreSQL)]
  B --> CFG[config.py Settings]
```
方針: Cog は入出力(Discord との境界)に徹し、ロジックは `services/`、DB アクセスは `db/` に分離する。これによりテスト容易性と移行容易性を確保する。

## 4. ディレクトリ構成
```
DesicordBot/
├── CLAUDE.md
├── README.md                 # docs エージェントが作成
├── pyproject.toml            # 依存・ruff・pytest 設定
├── .env.example              # 変数名のみ(値なし)
├── .gitignore                # .env を除外(既存)
├── Dockerfile
├── docker-compose.yml        # postgres (+ 任意で bot)
├── alembic.ini
├── migrations/               # Alembic (env.py, versions/)
├── docs/design/              # 本設計
├── src/desicordbot/
│   ├── __main__.py           # python -m desicordbot
│   ├── bot.py                # Bot 本体、Cog ロード、setup_hook
│   ├── config.py             # pydantic-settings による Settings
│   ├── logging_setup.py
│   ├── cogs/
│   │   ├── general.py        # /ping
│   │   ├── ai_chat.py        # /chat /reset ほか
│   │   └── admin.py          # 管理者向け設定
│   ├── services/
│   │   ├── chat_service.py   # 履歴組立・トリム・呼び出し・保存
│   │   ├── claude_client.py  # anthropic.AsyncAnthropic ラッパ
│   │   ├── rate_limiter.py
│   │   └── text_utils.py     # 2000字分割、メンション無害化
│   └── db/
│       ├── engine.py         # AsyncEngine / sessionmaker
│       ├── models.py         # SQLAlchemy モデル
│       └── repositories.py
└── tests/                    # pytest (unit は DB/API をモック)
```

## 5. 技術選定と理由
| 項目 | 選定 | 理由 |
|---|---|---|
| Discord | discord.py 2.x (`app_commands`) | 規約どおり。スラッシュコマンド標準対応 |
| Claude | 公式 `anthropic` SDK の `AsyncAnthropic` | 非同期対応、リトライ/タイムアウトが組込み |
| DB | PostgreSQL 16 | 要件。JSON・インデックス・将来の拡張性 |
| DBドライバ/ORM | SQLAlchemy 2.0 async + asyncpg | 非同期、Alembic と統合可能 |
| マイグレーション | Alembic (async env.py) | 標準的、履歴管理と自動生成。起動時自動適用はせず `alembic upgrade head` を明示実行 |
| 設定 | pydantic-settings + `.env` | 型検証、起動時に必須変数の欠落を検知 |
| ログ | 標準 `logging` | 秘密情報(トークン・APIキー・DB URL)はマスク |
| テスト/Lint | pytest + pytest-asyncio / ruff | 規約どおり |
| 移行容易性 | 12-factor(設定は環境変数のみ、ログは stdout) | ローカル→Docker→PaaS を同一コードで |

## 6. 設定/環境変数
| 変数 | 必須 | 既定 | 説明 |
|---|---|---|---|
| `DISCORD_TOKEN` | 必須 | - | Bot トークン。ログ出力禁止 |
| `ANTHROPIC_API_KEY` | 必須 | - | Claude API キー。ログ出力禁止 |
| `DATABASE_URL` | 必須 | - | `postgresql+asyncpg://user:pass@host:5432/db` |
| `CLAUDE_MODEL` | 任意 | `claude-sonnet-5-5` | 使用モデルID |
| `CLAUDE_MAX_OUTPUT_TOKENS` | 任意 | `1024` | 1応答の最大出力トークン |
| `CLAUDE_TIMEOUT_SECONDS` | 任意 | `60` | API タイムアウト |
| `HISTORY_MAX_MESSAGES` | 任意 | `20` | 文脈に含める最大件数 |
| `HISTORY_MAX_INPUT_TOKENS` | 任意 | `8000` | 入力トークン予算 |
| `RATE_LIMIT_USER_PER_MIN` | 任意 | `5` | ユーザーあたり/分 |
| `DAILY_TOKEN_BUDGET_PER_USER` | 任意 | `50000` | ユーザー日次トークン上限 |
| `DAILY_TOKEN_BUDGET_GLOBAL` | 任意 | `500000` | 全体日次上限 |
| `ALLOWED_GUILD_IDS` | 任意 | 空(全許可) | カンマ区切り。開発時は特定ギルドのみ |
| `DEV_GUILD_ID` | 任意 | 空 | 設定時はそのギルドへコマンドを即時同期 |
| `OWNER_IDS` | 任意 | 空 | Bot 運営者のユーザーID(全体操作の許可) |
| `LOG_LEVEL` | 任意 | `INFO` | |

`.env.example` には変数名とダミー値のみを載せる。`.env` はコミットしない。

## 7. Intents と Bot 権限(最小限)
- Intents: `Intents.default()` から開始し、**Privileged Intents は全て無効**(Message Content / Members / Presence を使わない)。スラッシュコマンドのみで動作する。
- メンション応答を採用する場合のみ Message Content Intent が必要(未決事項 U-1)。
- 招待時の権限(OAuth2 scope: `bot`, `applications.commands`)
  - View Channels, Send Messages, Send Messages in Threads, Read Message History(返信参照に必要な場合のみ), Embed Links は不要
  - Administrator / Manage * / Mention Everyone は付与しない

## 8. 運用上の共通方針
- 起動順序: 設定読込 → ログ設定 → DBエンジン作成/疎通確認 → Cog ロード → コマンド同期 → 接続
- グローバル同期は反映に時間がかかるため、開発時は `DEV_GUILD_ID` に対してギルド同期
- 終了時: `bot.close()` で DB エンジンと Anthropic クライアントを破棄
- ブロッキング呼び出し禁止(DB/HTTP はすべて async)

## 未決事項
| ID | 論点 | 選択肢 | 推奨 |
|---|---|---|---|
| O-1 | ORM/マイグレーション手段 | (a) SQLAlchemy+Alembic (b) asyncpg直 + 手書きSQLマイグレーション | (a)。スキーマ変更が多くなる見込みのため |
| O-2 | Bot 本体の Docker 化時期 | (a) 今回タスクに含む (b) 後続 | (b) だが Dockerfile は最小限を用意 |
| O-3 | パッケージ管理 | (a) uv (b) pip + requirements (c) poetry | (a) uv + pyproject.toml |
| O-4 | コマンド同期方針 | (a) 起動時自動 (b) 管理コマンドで手動 | (a) 開発ギルド限定、本番グローバルは (b) |
