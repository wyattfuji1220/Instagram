# GitHub Actions 上での設定点検

実行: 2026-08-22 19:08 JST

```
[ok] ANTHROPIC_API_KEY は設定されています。
[ok] IG_USER_ID は設定されています。
[ok] IG_ACCESS_TOKEN は設定されています。
[--] IG_API_HOST: https://graph.instagram.com
[ok] Instagram アカウント: @anne_books88 (Anne（アン）📚月一冊から始めるビジネス書)
[ok] 音源ライブラリに接続できました（トレンド25件）
[ok] 音源用トークンの残り: 59日
[ok] 楽天ブックスAPIに接続できました。
[--] トークンの有効期限を取得できませんでした（無期限トークンの可能性があります）。
[--] キュー残: 0冊 / 投稿在庫: 9日分
[NG] キューの残りが少なすぎます（14冊未満）。
```

## 音源用の設定はどこに入っているか

```
Secrets 側
  FB_ACCESS_TOKEN: 入っています
  FB_IG_USER_ID: 入っています
  FB_APP_ID: 入っています
  FB_APP_SECRET: 入っています
Variables 側
  FB_ACCESS_TOKEN: 空です
  FB_IG_USER_ID: 空です
```
