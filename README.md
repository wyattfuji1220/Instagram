# 読書記録 Instagram 自動投稿システム

読了した本を、毎朝7時に Instagram へカルーセル投稿するシステム。
運用は「本をキューに追加する」と「週1回レビューする」だけ。

- 要件と設計判断: [REQUIREMENTS.md](REQUIREMENTS.md)
- 初回セットアップ: [SETUP.md](SETUP.md)

## しくみ

```
books/queue.yaml に本を追加
        ↓  日曜 20:00（GitHub Actions）
根拠データ取得（NDL / 楽天ブックス / Google Books / openBD）
        ↓  足りなければ Claude の Web検索で補完
        ↓
Claude API が5日分の原稿を生成
        ↓
HTML/CSS → Playwright → カード画像 5枚 × 5日
        ↓
Pull Request が自動で作られる  ←  あなたがレビュー＆マージ
        ↓  毎朝 07:00（GitHub Actions）
Instagram Graph API でカルーセル投稿
```

1冊 = 1投稿。月曜は書籍紹介ではなく「ビジネス書 新刊特集」を配信します。

| 曜日 | 内容 | 枚数 |
|---|---|---|
| 火〜日 | 読了した本の紹介（読書メモが根拠） | 10枚 |
| 月 | ビジネス書 新刊特集（楽天の新刊から4冊選抜） | 6枚 |

投稿と同時に、9:16のストーリーも自動で流れます。

## コマンド

```bash
PYTHONPATH=src python -m bookgram generate       # 下書きを生成（週次相当）
PYTHONPATH=src python -m bookgram feature        # ビジネス書の新刊特集（月曜枠）
PYTHONPATH=src python -m bookgram post --dry-run # 今日の投稿内容を確認
PYTHONPATH=src python -m bookgram post           # 今日の分を投稿
PYTHONPATH=src python -m bookgram preview        # プレビューを作り直す
PYTHONPATH=src python -m bookgram doctor         # 設定と接続の点検
PYTHONPATH=src python -m bookgram whoami         # トークンから IG_USER_ID を調べる
PYTHONPATH=src python -m bookgram refresh-token  # 長期トークンを延長
PYTHONPATH=src python -m bookgram cleanup        # 古い画像を削除
```

## ディレクトリ

| パス | 中身 |
|---|---|
| `books/queue.yaml` | 投稿する本のキュー（**あなたが編集する唯一のファイル**） |
| `drafts/YYYY-MM-DD/post.json` | 生成された下書き。手で修正してよい |
| `docs/img/YYYY-MM-DD/` | 投稿用カード画像（GitHub Pages で配信） |
| `docs/preview/` | 週次レビュー用ページ |
| `output/index.html` | プレビューへのリンク集 |
| `posted.jsonl` | 投稿履歴 |

## テスト

```bash
python -m pytest tests -q
```

## ハルシネーション対策

書名だけからAIが全文を生成する構成のため、事実誤認を4層で防いでいます。

1. 4つの書誌DBと、必要ならWeb検索から根拠を集めて渡す。集まらない本は生成中止
2. 根拠に無い固有名詞・数値・引用を書かせないプロンプト制約
3. 各日の記述の根拠を `grounding` に出力させ、レビュー画面に併記
4. Pull Request のマージを投稿の必須条件にする（未マージなら投稿されない）
