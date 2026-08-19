# CLAUDE.md（プロジェクト設定）

読書記録 Instagram 自動投稿システム。グローバル設定に加えて以下を適用する。

## リポジトリ

- push 先: https://github.com/wyattfuji1220/Instagram.git
- 既定ブランチ: `main`
- 週次生成は `drafts/YYYY-Www` ブランチから PR を出す

## 実行方法

パッケージは `src/bookgram` にあり、インストールせず `PYTHONPATH=src` で実行する。

```bash
PYTHONPATH=src python -m bookgram <command>
```

## テスト

```bash
python -m pytest tests -q
```

外部APIを叩くテストは書かない。`bookdata` / `generate` / `publish` の
ネットワーク境界はモックするか、純粋関数だけをテストする。

## この構成で守ること

- **書誌データを根拠にしない生成を追加しない。** `generate.py` のプロンプト制約
  （根拠に無い固有名詞・数値・引用を書かせない）は品質の生命線なので緩めない。
- **投稿の前提はPRマージ。** `daily-post` が下書きの存在を条件に投稿する設計を崩さない。
  未レビューの内容が投稿される経路を作らない。
- **画像は必ず公開URL経由。** Instagram Graph API はローカルファイルを受け付けない。
  `verify_images_public` のチェックを外さない。
- カード本文は 60〜110 文字を想定してCSSを組んでいる。文字数の想定を変える場合は
  `templates/card.html.j2` のフォントサイズも合わせて調整する。

## 秘密情報

`.env` はコミットしない（`.gitignore` 済み）。
CI では GitHub Secrets（`ANTHROPIC_API_KEY` / `IG_USER_ID` / `IG_ACCESS_TOKEN`）と
Variables（`PAGES_BASE_URL` / `IG_API_HOST`）を使う。

## モデル

原稿生成は `claude-opus-5`（`src/bookgram/config.py` の `MODEL`）。
変更する場合はコスト見積（REQUIREMENTS.md 第9節）も更新する。
