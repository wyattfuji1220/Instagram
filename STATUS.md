# 現在の状況（引き継ぎメモ）

最終更新: 2026-08-20 19:38 JST

新しいセッションで作業を再開するときは、まずこのファイルを読んでください。
仕様や手順は [REQUIREMENTS.md](REQUIREMENTS.md) / [SETUP.md](SETUP.md) / [README.md](README.md) にあります。

---

## 稼働状況

| 項目 | 状態 |
|---|---|
| Instagram 連携 | 完了（`@anne_books88` / Instagram ログイン方式） |
| GitHub Pages | 公開中（`https://wyattfuji1220.github.io/Instagram/`） |
| 楽天ブックスAPI | 稼働（書影・内容紹介が取得できる） |
| NDLサーチ / openBD | 稼働 |
| Google Books | この回線からは 429 で常に失敗（他ソースで代替できるため実害なし） |

### 投稿済み

| 日付 | 内容 | media_id |
|---|---|---|
| 2026-08-20 | 『すごい左利き』（カルーセル10枚） | `18226190584323552` |
| 2026-08-24 | 新刊特集 26年8月後半（6枚） | `18124850473768394` |
| 〃 | 上記のストーリー | `18187697140393837` |

※ 8/20 の投稿はストーリー機能を実装する前だったため、ストーリーはありません。

---

## 進行中の作業

**残り書籍の一括生成**（バックグラウンドで実行中）

```bash
PYTHONPATH=src python -m bookgram generate --days 70
```

- 2026-08-20 19:38 時点: 下書き20件 / キュー残30冊
- 1冊あたり約1.2分。完了まで残り約35分
- 停止した場合は同じコマンドを再実行すれば続きから進みます

### 完了後にやること

1. **著者・発行日が空の2冊を作り直す**
   書名が書誌DBの表記と食い違い、初回生成時にデータが取れなかった本です。
   簡略化書名での再検索を実装済みなので、材料を取り直して差し替えます。

   - `drafts/2026-08-27/post.json`（論語と算盤）
   - `drafts/2026-08-30/post.json`（キーエンス解剖）

   ```bash
   # material を取り直して published / book_author / cover_url を差し替えたあと
   PYTHONPATH=src python -m bookgram rerender --date 2026-08-27
   ```

2. **全下書きをコミットして push**（画像が GitHub Pages に載らないと投稿できません）

3. **プレビューで内容を確認**
   `docs/preview/*.html` をブラウザで開く

---

## 利用者側で未確認の設定

明日以降の自動投稿の前に確認が必要です。手元の `.env` では両方とも問題がありました。

**Secrets** — https://github.com/wyattfuji1220/Instagram/settings/secrets/actions

| Secret | 正しい値 |
|---|---|
| `RAKUTEN_APP_ID` | アプリケーションID（36文字のUUID） |
| `RAKUTEN_ACCESS_KEY` | アクセスキー（46文字。逆に入れると 403 Invalid Access Key） |

**Variables** — https://github.com/wyattfuji1220/Instagram/settings/variables/actions

| Variable | 値 |
|---|---|
| `PAGES_BASE_URL` | `https://wyattfuji1220.github.io/Instagram` |
| `IG_API_HOST` | `https://graph.instagram.com`（無いとエラー190になる） |

確認後、Actions の `daily-post` を **dry_run にチェックを入れて**手動実行すれば、
投稿せずに認証と画像URLの疎通だけ検証できます。

---

## 運用サイクル

| タイミング | 内容 |
|---|---|
| 火〜日 07:00 | 読了本の紹介（10枚）＋ストーリー |
| 月 07:00 | ビジネス書 新刊特集（6枚）＋ストーリー |
| 日 20:00 | 翌週分の下書きを生成し、PR を作成 |
| PR マージ時 | その日付が投稿対象になる（**未マージなら投稿されない**） |

在庫は 49冊 ＝ 約8週間（2026年10月中旬まで）。
残り14冊を切ると生成時に警告が出ます。

---

## 設計上の約束（変更しないこと）

- **根拠データにない事実を書かせない。** 書誌データ・読書メモ・Web検索結果のみを根拠とし、
  各投稿の `grounding` に根拠の所在を残す。
- **新刊特集の事実は楽天のデータをそのまま使う。** Claude には「どれを選ぶか」と
  「注目ポイントの文章」だけを任せる（未読の本なので）。
- **投稿の前提は PR マージ。** 未レビューの内容が投稿される経路を作らない。
- **画像は 1080×1350（4:5）。** Instagram のグリッドが 4:5 表示のため、
  正方形にするとアーカイブで左右が切れる。
- **画像は公開URL経由。** Graph API はローカルファイルを受け付けない。

## つまずきやすい点

| 症状 | 原因と対処 |
|---|---|
| 楽天が 403 Invalid Access Key | `RAKUTEN_APP_ID` と `RAKUTEN_ACCESS_KEY` が逆 |
| 楽天が 403 REFERRER_MISSING | Referer と Origin の**両方**が必要（実装済み） |
| Instagram がエラー190 | `IG_API_HOST=https://graph.instagram.com` が未設定 |
| 画像URLが公開されていない | Pages のデプロイ待ち。PR を朝7時より前にマージする |
| Google Books が 429 | 匿名アクセスの制限。他ソースで続行するので無視してよい |
