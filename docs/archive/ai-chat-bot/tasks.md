# 実装タスク一覧 (tasks)

担当: D=developer / T=tester / DOC=docs。1タスク=1つの小さな成果単位。レビュー(reviewer)・監査(security)は各フェーズ完了時に実施。

## フェーズ0: 最小構成(起動して /ping が返る)
| ID | タスク | 担当 | 依存 | 完了条件 |
|---|---|---|---|---|
| T01 | プロジェクト雛形: `pyproject.toml`(依存・ruff・pytest設定)、`src/desicordbot/__init__.py`、`.env.example` | D | - | `ruff check` と `pytest`(空)が通る。`.env.example` に変数名のみ記載、`.gitignore` に `.env` |
| T02 | `config.py`(pydantic-settings)と `logging_setup.py` | D | T01 | 必須変数欠落で起動時に明確なエラー。トークン等がログ/`repr`に出ない |
| T03 | `bot.py` + `__main__.py`: Bot 生成(default Intents、`AllowedMentions.none()`)、Cog 自動ロード、`DEV_GUILD_ID` 同期 | D | T02 | `python -m desicordbot` で起動しオンラインになる |
| T04 | `cogs/general.py`: `/ping`(レイテンシ表示) | D | T03 | 実サーバーで `/ping` が応答する |
| T05 | T02-T04 のテスト(設定検証、Cog 単体) | T | T04 | `pytest` 緑。Discord 接続不要(モック) |
| T06 | 最小限の README(セットアップ・起動手順・Bot招待/権限/Intents) | DOC | T04 | READMEの手順で第三者が起動可能 |

## フェーズ1: DB基盤
| ID | タスク | 担当 | 依存 | 完了条件 |
|---|---|---|---|---|
| T10 | `docker-compose.yml`(postgres:16)、`.env.example` に `DATABASE_URL` 追記 | D | T01 | `docker compose up -d db` で接続できる |
| T11 | `db/engine.py`、起動時疎通確認、終了時 dispose | D | T03, T10 | DB停止時は起動を失敗させ分かりやすいログ |
| T12 | Alembic 導入(async `env.py`)、初回マイグレーション(`ai-chat.md` §5 の全テーブル) + `db/models.py` | D | T11 | `alembic upgrade head` / `downgrade base` が成功 |
| T13 | `db/repositories.py`(conversations/messages/settings/usage の CRUD) | D | T12 | 各メソッドが async で動作 |
| T14 | リポジトリのテスト(テスト用DB、トランザクションロールバック) | T | T13 | CIまたはローカルで緑 |

## フェーズ2: AI チャット
| ID | タスク | 担当 | 依存 | 完了条件 |
|---|---|---|---|---|
| T20 | `services/text_utils.py`: 2000字分割(コードブロック維持)、メンション無害化 | D | T01 | 境界値(1900/ちょうど/超過/コードブロック跨ぎ)を含むテスト |
| T21 | T20 のテスト | T | T20 | 緑 |
| T22 | `services/claude_client.py`: AsyncAnthropic ラッパ(モデル・max_tokens・timeout 設定、例外の分類) | D | T02 | モックで正常/429/タイムアウト/認証エラーを区別して返す |
| T23 | `services/rate_limiter.py`(分あたり+同時実行1) | D | T01 | 単体テストで制限・解除を確認 |
| T24 | `services/chat_service.py`: 履歴取得・トリム・システムプロンプト組立・呼び出し・保存・使用量加算・日次上限チェック | D | T13, T20, T22, T23 | 履歴件数/トークン予算でのトリム、失敗時に履歴を汚さないことをテスト |
| T25 | T22-T24 のテスト(Anthropic はモック) | T | T24 | 緑 |
| T26 | `cogs/ai_chat.py`: `/chat`(defer・分割送信・ephemeral オプション・エラーハンドラ・`guild_only`) | D | T24 | 実サーバーで会話が続く。メンションが発火しない |
| T27 | `/reset`(me/channel と権限チェック) | D | T26 | 権限なしの `channel` が拒否される |
| T28 | `/chat-usage` | D | T26 | 本日の使用量が表示される |
| T29 | Cog テスト(権限・エラー分岐) | T | T26-T28 | 緑 |

## フェーズ3: 管理機能と運用
| ID | タスク | 担当 | 依存 | 完了条件 |
|---|---|---|---|---|
| T30 | `cogs/admin.py`: `/chat-system`, `/chat-config` | D | T26 | 設定が会話に反映、権限チェック有り |
| T31 | 履歴期限削除の定期タスク(日次) | D | T13 | 期限超過データのみ削除されるテスト |
| T32 | 管理機能のテスト | T | T30, T31 | 緑 |
| T33 | Dockerfile(Bot本体、非rootユーザー、環境変数のみで設定) | D | T26 | `docker build` と compose 経由で起動 |

## フェーズ4: 品質ゲート
| ID | タスク | 担当 | 依存 | 完了条件 |
|---|---|---|---|---|
| T40 | コードレビュー | reviewer | T29 | 指摘を D が修正 |
| T41 | セキュリティ監査(トークン/APIキー露出、Intents、権限、メンション抑止、依存) | security | T32 | 指摘ゼロまたは対応済み |
| T42 | ドキュメント整備(使い方、コマンド一覧、プライバシー/保持方針、運用・Docker手順、マイグレーション手順) | DOC | T33, T41 | README と docs が実装と一致 |

## 順序の要点
- 並行可: T10/T20/T22/T23(依存が軽い)、各 D タスクと対応する T タスク。
- クリティカルパス: T01 → T02 → T03 → T04 →(T10 → T11 → T12 → T13)→ T24 → T26。
- フェーズ0完了時点で一度リード確認(起動と `/ping`)を挟む。

## 未決事項
| ID | 論点 | 選択肢 | 推奨 |
|---|---|---|---|
| K-1 | CI(GitHub Actions)の導入 | (a) 今回含む (b) 後続 | (b)。ホスティング未定のため後続 |
| K-2 | テスト用DB | (a) docker の postgres (b) testcontainers (c) SQLite 代替 | (a) 本番と同一エンジンで挙動差を避ける |
| K-3 | 実 Anthropic API を使う統合テスト | (a) なし (b) 手動実行のみ | (b) マーカーで通常実行から除外 |
| K-4 | 他ドキュメントの未決事項(overview O-1〜4, ai-chat U-1〜9) | - | 着手前にリードがユーザーへ確認 |
