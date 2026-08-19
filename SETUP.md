# セットアップ手順

初回のみ必要な作業です。所要 30〜45分。
**トークンやパスワードの入力はすべてあなたご自身で行ってください。**

---

## 1. Instagram アカウントの確認 ✅ 済み

プロアカウント（ビジネス／クリエイター）であることが必須です。
Instagram アプリ → 設定 → アカウントの種類とツール から確認できます。

---

## 2. Meta 開発者アプリを作る

認証方式は2通りあります。**推奨は A（Instagram ログイン）** です。
Facebook ページを作る必要がなく、トークンの自動延長も使えます。

### A. Instagram ログイン方式（推奨）

1. https://developers.facebook.com/ にログインし、**マイアプリ → アプリを作成**
2. ユースケース一覧から **「Instagramでメッセージとコンテンツを管理」** を選択
   （単独の「Instagram」という項目はありません。説明文に「投稿の公開」と
   書かれているものが目的のユースケースです）
3. 作成後、アプリのダッシュボードで **Instagram** のセットアップに進み、
   **「Instagram ログインでの API 設定」**（Instagram API with Instagram login）を選ぶ
4. **ビジネスログイン設定** から、投稿先の Instagram アカウントを連携する
5. 必要な権限（スコープ）に以下が含まれていることを確認する
   - `instagram_business_basic`
   - `instagram_business_content_publish`
6. **アクセストークンを生成** し、表示された長期トークン（60日有効）を控える
7. Instagram ユーザーIDを取得する。ブラウザで以下を開く:

   ```
   https://graph.instagram.com/v23.0/me?fields=user_id,username&access_token=【手順6のトークン】
   ```

   返ってくる `user_id` が `IG_USER_ID` です（`id` ではありません）。

   `.env` にトークンを書いた場合は、コマンドでも調べられます:

   ```bash
   PYTHONPATH=src python -m bookgram whoami
   ```

8. この方式では `IG_API_HOST` に `https://graph.instagram.com` を設定します（手順4参照）。

### B. Facebook ログイン方式

Facebook ページを Instagram アカウントに連携している場合はこちらでも動きます。
`IG_API_HOST` は既定値（`https://graph.facebook.com`）のままにしてください。
必要な権限は `instagram_basic` / `instagram_content_publish` / `pages_show_list` /
`pages_read_engagement` です。

---

## 3. GitHub Secrets を登録する

リポジトリの **Settings → Secrets and variables → Actions → Secrets** タブで登録します。

| Secret 名 | 値 |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ で発行した API キー |
| `IG_USER_ID` | 手順2で取得した ID |
| `IG_ACCESS_TOKEN` | 手順2で取得した長期トークン |

同じ画面の **Variables** タブで、以下も登録します。

| Variable 名 | 値 |
|---|---|
| `PAGES_BASE_URL` | `https://wyattfuji1220.github.io/Instagram` |
| `IG_API_HOST` | 方式Aなら `https://graph.instagram.com`（方式Bなら登録不要） |

---

## 4. GitHub Pages を有効にする

**Settings → Pages** で以下を設定します。

- Source: **Deploy from a branch**
- Branch: **main** / フォルダ: **/docs**

数分後に `https://wyattfuji1220.github.io/Instagram/` が開けば成功です。
生成された画像はここから配信され、Instagram API がその URL を読み込みます。

---

## 5. 本をキューに入れる

`books/queue.yaml` を編集して、読了した本を追加します。

```yaml
books:
  - title: "限りある時間の使い方"
    isbn: "9784763139331"
    notes: "効率化の逆説の部分が刺さった"
    status: pending
```

- `isbn` は任意ですが、同名異書の取り違えを防げるので入れることを推奨します。
- **`notes` は実質必須と考えてください。** openBD に内容紹介が無い本は珍しくなく、
  その場合メモが唯一の根拠になります。80文字以上あれば生成できます。
  自分の言葉で書いたメモが入るほど、投稿の内容はオリジナルで正確になります。
- 1冊 = 5日分の投稿になります。**最低5冊は入れてください。**

---

## 6. 動作確認

ローカルで確認する場合:

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env   # .env に値を記入
```

設定の点検:

```bash
PYTHONPATH=src python -m bookgram doctor
```

GitHub 上で確認する場合は、**Actions タブ → 「週次 下書き生成」→ Run workflow**
を手動実行してください。数分後に Pull Request が作られます。

---

## 7. 日々の運用

| タイミング | やること |
|---|---|
| 日曜 20:00 | システムが自動で下書きを生成し、PR を作る |
| 月曜まで | PR のプレビューを確認し、問題なければマージする |
| 毎朝 7:00 | マージ済みの日付が自動投稿される |
| 月1回程度 | `books/queue.yaml` に本を補充する |
| 60日ごと | アクセストークンを延長する（下記） |

### アクセストークンの延長

期限の14日前になると、投稿ログと `doctor` が警告を出します。

方式A（Instagram ログイン）の場合:

```bash
PYTHONPATH=src python -m bookgram refresh-token
```

表示された新しいトークンを GitHub Secrets の `IG_ACCESS_TOKEN` に貼り替えてください。

方式Bの場合は Meta のグラフAPIエクスプローラから再取得してください。

---

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| 生成が「根拠データが不足」で止まる | openBD に内容紹介が無い本です（珍しくありません）。`notes` に自分の言葉で80文字以上のメモを書いてください。これが最も確実です |
| ログに「Google Books を取得できませんでした: HTTP 429」 | 匿名アクセスのレート制限。他ソースで続行するので多くは無害。頻発する場合は Secrets に `GOOGLE_BOOKS_API_KEY` を登録する |
| 投稿が「画像URLが公開されていません」で失敗 | GitHub Pages のデプロイ待ち。PR を朝7時より前にマージする |
| 投稿が Graph API エラー [190] | トークン失効。手順7で延長する |
| 文字が画像からはみ出す | 生成文が長すぎる。`drafts/` の JSON を直接編集して短くする |
| キュー切れ警告が出た | `books/queue.yaml` に本を追加する |
