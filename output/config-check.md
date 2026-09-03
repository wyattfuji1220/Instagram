# GitHub Actions 上での設定点検

実行: 2026-09-03 21:56 JST

```
[ok] ANTHROPIC_API_KEY は設定されています。
[ok] IG_USER_ID は設定されています。
[ok] IG_ACCESS_TOKEN は設定されています。
[--] IG_API_HOST: https://graph.instagram.com
[ok] Instagram アカウント: @anne_books88 (Anne（アン）📚毎朝7時の読書レビュー)
[ok] 音源ライブラリに接続できました（トレンド25件）
[ok] 音源用トークンの残り: 47日
[ok] 楽天ブックスAPIに接続できました。
[--] トークンの有効期限を取得できませんでした（無期限トークンの可能性があります）。
[ok] Google Books に接続できました（1万人の脳を見た名医が教えるすごい左利き）。
[ok] すべての下書きに書影が付いています。
[ok] 身元が分かる表現は見つかりませんでした。
[--] キュー残: 72冊 / 投稿在庫: 18日分
```

## 音源用の設定はどこに入っているか

```
Secrets 側
  FB_ACCESS_TOKEN: 入っています
  FB_IG_USER_ID: 入っています
  FB_APP_ID: 入っています
  FB_APP_SECRET: 入っています
  GOOGLE_BOOKS_API_KEY: 入っています
Variables 側
  FB_ACCESS_TOKEN: 空です
  FB_IG_USER_ID: 空です
```
