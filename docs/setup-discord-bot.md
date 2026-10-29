# Discord Bot のセットアップ(常駐 Bot 用)

常駐 Bot(スラッシュコマンド・スケジュール通知)に必要な Bot を、Discord Developer Portal で作成する手順。
Webhook だけを使う現行の Actions 構成には不要。方針は `docs/design/resident-bot.md` の 8 章。

注意: Discord の画面表記・メニュー配置は変わることがある。以下の項目名は執筆時点の想定で、見つからない場合は近い名前の項目を探すこと。確信が持てない点は「要確認」と書く。

## 前提

- Discord アカウントと、Bot を入れる自分のサーバー(管理権限があるもの)。
- ブラウザで Developer Portal(`https://discord.com/developers/applications`)を開ける。

## 1. アプリケーションを作る

1. Developer Portal を開き、Discord アカウントでログインする。
2. 「New Application」を押し、名前(例: のむしょう)を入力して規約に同意し、作成する。

## 2. Bot を設定する

1. 左メニューの「Bot」を開く(アプリ作成時に Bot ユーザーが自動で用意される想定。無ければ追加する。要確認)。
2. 「Public Bot」を**オフ**にする(自分だけが招待できるようにする)。
3. 「Requires OAuth2 Code Grant」が**オフ**であることを確認する。
4. 「Privileged Gateway Intents」の 3 つ(Presence Intent / Server Members Intent / Message Content Intent)は**すべてオフのまま**にする(本 Bot は不要)。
5. 保存が必要な画面なら保存する。

## 2b. Installation を設定する(User Install を外す)

目的: Bot を「ユーザーインストール」で他人や他の場所へ入れられないようにする。

1. 左メニューの「Installation」を開く(項目名は要確認)。
2. 「Install Link」を「None」にする。または Guild Install のみにする。
3. 「Installation Contexts」(名称は要確認)で「User Install」のチェックを外し、「Guild Install」のみにする。
4. 保存が必要な画面なら保存する。

注意: 実際の画面での確認は未実施。表記・配置は要確認。

## 3. トークンを取得して保管する

1. 「Bot」ページの「Reset Token」(または「Copy」)でトークンを表示する。表示は作成/再発行の直後のみの場合がある(要確認)ので、その場で控える。
2. トークンはパスワードと同じ扱いにする。
   - `.env` の `DISCORD_BOT_TOKEN=` に書く。`.env` は権限 600 にし、Git にコミットしない。
   - チャット、スクリーンショット、Issue、ログに貼らない。
3. 漏れた/漏れたかもしれない場合は、「Reset Token」で**再発行**する。古いトークンは無効になる。再発行後は `.env` を更新して Bot を再起動する(`docs/operations.md`)。

## 4. 招待 URL を作ってサーバーへ入れる

1. 左メニューの「OAuth2」を開く。URL Generator(「OAuth2 URL Generator」。位置は要確認)を使う。
2. Scopes で `bot` と `applications.commands` にチェックを入れる。
3. Bot Permissions で次の**最小限**だけを選ぶ。
   - View Channels(表示名は View Channel の場合あり)
   - Send Messages
   - Embed Links
   - スレッドを使う場合のみ Send Messages in Threads(当面は不要)
   - Administrator、Manage Messages 等は付与しない。
4. 生成された URL をブラウザで開き、招待先のサーバーを選んで認可する。
5. Bot は `DISCORD_GUILD_ID` のサーバーでのみ使う想定。それ以外のサーバーに入ると自動退出する仕様(実装中。動作は未確認)。
6. サーバーのメンバー一覧に Bot が表示されれば成功(オフライン表示は正常。Bot はまだ起動していない)。
7. 通知用の専用チャンネルを作り、チャンネルの権限を自分と Bot のみが見える設定にすることを推奨する。

## 5. ID を取得する(開発者モード)

ID の取得には開発者モードが必要。

1. Discord の「ユーザー設定」→「詳細設定」(Advanced)→「開発者モード」をオンにする(項目名は変わることがある)。
2. ギルド(サーバー)ID: サーバー名(アイコン)を右クリック(スマホは長押し)→「サーバー ID をコピー」。
3. ユーザー ID: 自分のアイコン/名前を右クリック→「ユーザー ID をコピー」。
4. チャンネル ID: 通知用チャンネルを右クリック→「チャンネル ID をコピー」。
5. 控えた値を `.env` に書く(変数名は `.env.example` と `src/nomusyo/bot/config.py` に基づく)。

| 値 | 変数 |
|---|---|
| ギルド ID | `DISCORD_GUILD_ID` |
| 自分のユーザー ID | `ALLOWED_USER_IDS`(カンマ区切りで複数可。操作を許可するユーザー) |
| 失敗 DM の宛先(任意) | `ADMIN_USER_ID`(省略時は `ALLOWED_USER_IDS` の先頭) |
| 通知チャンネル ID | `NOTIFY_CHANNEL_ID` |

コマンドはギルド限定(`DISCORD_GUILD_ID` に登録、DM では使えない)。`ALLOWED_USER_IDS` 以外のユーザーが実行すると拒否される。

## 6. 確認

1. 招待後、Bot がサーバーに参加していること(オフラインで可)。
2. 特権 Intents がすべてオフであること。
3. Public Bot がオフであること。
4. トークンと各 ID を控えてあること。
5. 起動後の動作確認(`/news`、`/status`)は `docs/operations.md` の常駐 Bot の節を参照(実 Discord 接続での確認は未実施)。

## 失敗したとき

| 症状 | 確認 |
|---|---|
| 招待 URL を開いてもサーバーが選べない | そのサーバーで「サーバー管理」権限があるか |
| 招待できるが「Public Bot がオフ」で拒否される | 招待操作をしているアカウントが Bot の所有者(アプリを作ったアカウント)か。要確認 |
| スラッシュコマンドが出ない | `applications.commands` スコープで招待したか。`DISCORD_GUILD_ID` が招待先サーバーの ID か(コマンドはそのギルドにのみ同期される) |
| 起動時に認証エラー | トークンの貼り間違い・再発行後の古い値。再発行して更新 |
