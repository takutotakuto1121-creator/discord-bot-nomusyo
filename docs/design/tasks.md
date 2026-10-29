# 実装タスク一覧

状態: 実装済み(2026-10-29)。設計時点からの変更点は末尾の「実装時の変更点」を参照。T01〜T24 は完了(✅)。T25(1週間の運用観察)と F01/F02 は未着手。

凡例: 担当 = developer(src/, .github/)、tester(tests/)、docs(docs/, README.md)。
1タスク=1つの小さな成果単位。developer と tester のタスクは同じ番号帯で並行可。

## フェーズ0: 土台

| ID | タスク | 担当 | 依存 | 完了条件 | 状態 |
|---|---|---|---|---|---|
| T01 | プロジェクト初期化: `pyproject.toml`(依存: feedparser, anthropic, requests / dev: pytest, ruff)、`src/nomusyo/` 骨格、`.gitignore`(`.env` 含む) | developer | なし | `pip install -e .[dev]` 成功、`ruff check` と `pytest`(空)が通る | ✅ |
| T02 | `config.py`: 環境変数読み込み(Secrets 必須チェック、任意値の既定値、`DRY_RUN`)。Secrets の値は repr/ログに出さない | developer | T01 | 必須変数欠落で分かりやすいエラー。値がログに出ない | ✅ |
| T03 | config のテスト | tester | T02 | 既定値・欠落・型変換・repr に Secrets が出ないことを検証 | ✅ |

## フェーズ1: 最小(固定文を Webhook に投稿)

| ID | タスク | 担当 | 依存 | 完了条件 | 状態 |
|---|---|---|---|---|---|
| T04 | `discord_io/webhook.py`: 送信関数(タイムアウト、429 の Retry-After 対応、5xx リトライ、URL をエラーに含めない) | developer | T02 | 固定文を送信でき、失敗時に例外。URL がログ/例外に出ない | ✅ |
| T05 | webhook のテスト(HTTP モック: 成功/429/5xx/4xx) | tester | T04 | リトライ回数・待機・例外が仕様どおり | ✅ |
| T06 | `main.py` 最小版 + `.github/workflows/daily-news.yml`(cron `0 22 * * *`、workflow_dispatch、concurrency、permissions 最小、timeout) | developer | T04 | 手動実行で固定文が Discord に届く。cron 式が UTC 22:00 | ✅ |
| T07 | 手順確認: Webhook 作成、Secrets 登録、手動実行方法を README に記載 | docs | T06 | 初見で Secrets 登録〜手動実行ができる手順 | ✅ |

## フェーズ2: 取得〜選定(Discord 非依存の core)

| ID | タスク | 担当 | 依存 | 完了条件 | 状態 |
|---|---|---|---|---|---|
| T08 | `core/models.py` + `core/sources.py`: Article 等の型、ソース定義(URL は疎通確認して確定、不通は報告) | developer | T01 | 各 URL の疎通結果をリードに報告、確定リストがコードに入る | ✅ |
| T09 | `core/fetcher.py`: 取得(タイムアウト、HTML 除去、概要切り詰め、期限フィルタ、ソース単位の失敗隔離) | developer | T08 | 一部ソース失敗でも他が返る | ✅ |
| T10 | fetcher のテスト(固定 RSS/Atom フィクスチャ、壊れたフィード、タイムアウト) | tester | T09 | ネットワーク不要で通る | ✅ |
| T11 | `core/dedupe.py` + `core/selector.py`: URL 正規化、タイトル類似除外、総合/テック配分、ラウンドロビン、不足補填 | developer | T08 | 仕様の配分で10件(不足時は補填) | ✅ |
| T12 | dedupe/selector のテスト(utm 除去、類似タイトル、比率、不足、0件) | tester | T11 | 境界値を網羅 | ✅ |

## フェーズ3: 要約

| ID | タスク | 担当 | 依存 | 完了条件 | 状態 |
|---|---|---|---|---|---|
| T13 | `core/summarizer.py`: Anthropic SDK で一括要約、system/データ分離、JSON 検証、再試行、フォールバック、`SUMMARY_MODEL` 反映、リンクはコード側付与 | developer | T08, T02 | 10件が要約され、失敗時はフォールバックになる。モデル ID が実在することを確認(不在ならリード報告) | ✅(ただしモデル ID の実在確認は未完了。ユーザーの初回手動実行で確認) |
| T14 | summarizer のテスト(SDK モック: 正常/不正 JSON/id 不整合/API エラー/インジェクション文を含む記事で出力が検証を通ること、`<` エスケープ) | tester | T13 | 全パスがネットワーク不要で通る | ✅ |
| T15 | `core/pipeline.py`: `run_pipeline(config)` で fetch→dedupe→select→summarize を統合(Discord 非依存) | developer | T09, T11, T13 | core 配下に discord の import がない | ✅ |

## フェーズ4: 投稿の結合

| ID | タスク | 担当 | 依存 | 完了条件 | 状態 |
|---|---|---|---|---|---|
| T16 | `discord_io/formatter.py`: Embed 整形(1記事=1Embed、色、日付見出し)、文字数制限の切り詰め、分割、`allowed_mentions` 無効化 | developer | T04, T15 | 制限値(Embed 10個/6000字等、公式で再確認)を超えない | ✅ |
| T17 | formatter のテスト(長文切り詰め、10件超、合計6000字超の分割、メンション無効) | tester | T16 | 境界値で通る | ✅ |
| T18 | `main.py` 本実装: pipeline → formatter → webhook、`--dry-run`、失敗時 exit code、ログ(Secrets 非出力) | developer | T15, T16 | dry-run で整形結果が出力され、本番実行で10件が届く | ✅ |
| T19 | 結合テスト(外部呼び出しをモックしたエンドツーエンド、失敗系の exit code) | tester | T18 | 仕様の失敗時挙動表どおり | ✅ |
| T20 | workflow を本実装に更新(依存インストール・キャッシュ、Secrets を env 経由、dry-run 手動入力) | developer | T18 | `workflow_dispatch` で実投稿成功 | ✅(ただし実 API での手動確認は未完了。ユーザーの初回手動実行で確認) |

## フェーズ5: 品質・運用

| ID | タスク | 担当 | 依存 | 完了条件 | 状態 |
|---|---|---|---|---|---|
| T21 | コードレビュー | reviewer | T19 | 指摘を developer が反映 | ✅ |
| T22 | セキュリティ監査(Secrets の出力、workflow の permissions/Action 固定、プロンプトインジェクション対策、依存) | security | T20 | 指摘ゼロまたは対応済み | ✅ |
| T23 | README・運用手順(ソース追加、モデル変更、cron 無効化対応、トラブルシュート、将来の常駐Bot化メモ) | docs | T20 | 運用手順が一通り揃う | ✅ |
| T24 | CI(ruff + pytest を PR/push で実行する workflow) | developer | T19 | CI が緑 | ✅ |
| T25 | 1週間の運用観察: cron 遅延・要約品質・コストを記録し、未決事項を見直す | リード | T20 | 結果をもとに設計の更新要否を判断 | 未着手 |

## 将来(今回対象外)

| ID | タスク | 担当 | 依存 | 状態 |
|---|---|---|---|
| F01 | 日跨ぎ重複防止(seen 管理) | developer | T20 | 未着手 |
| F02 | 常駐 Bot 化: discord.py + `/news` Cog が `core.pipeline` を呼ぶ | planner → developer | T15 | 未着手 |

## 依存関係(概要)

T01 → T02 → T04 → T06 → (T07) / T01 → T08 → T09, T11 → T15 ← T13 / (T04, T15) → T16 → T18 → T20 → T21〜T24

## 未決事項

| # | 論点 | 選択肢 | 推奨 |
|---|---|---|---|
| 1 | tester を実装と並行させるか | 各タスク直後に実施 / フェーズ終了時にまとめて | 各タスク直後(tester タスクは対応する実装タスク完了後に着手) |
| 2 | CI(T24)を早める | フェーズ0直後 / 最後 | T03 完了直後に前倒しすると回帰検知しやすい |
| 3 | 実 API を使った受け入れ確認 | しない(モックのみ) / T20 で手動1回 | T20 の手動実行で1回確認 |

## 実装時の変更点

- T13 のモデル ID 実在確認と、T20 の実 API での手動確認は未完了(実 API 未実施)。ユーザーの初回手動実行で確認する。既定 `claude-haiku-5-5` が存在しない場合、要約は恒久エラー扱いとなり exit 1 になる。
- T08: ソースは疎通確認のうえ確定した(詳細は `news-pipeline.md`)。`Source` の `weight` は予約のみで、現状の selector は使わない。
- T24: `.github/workflows/ci.yml`(push は main のみ・PR で ruff check / ruff format --check / pytest)を追加。あわせて `.github/dependabot.yml`(pip と github-actions、週次)がある。
- 未決事項 #3(実 API を使った受け入れ確認)は、T20 の手動実行で行う想定のまま未実施。#1 と #2 の運用結果はコードからは判断できないため未記載。
- 実装時の挙動変更(恒久的な API エラー、タイムアウト等)は `news-pipeline.md` の「実装時の変更点」を参照。
