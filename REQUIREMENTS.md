# 読書記録 Instagram 自動投稿システム — 要件定義

最終更新: 2026-08-19

## 1. 目的

自分の趣味である読書の記録を、Instagram に毎日自動投稿する。
運用の手間を「本を1冊キューに追加する」＋「週1回のレビュー」だけに圧縮する。

## 2. 確定した設計判断

| 項目 | 決定 |
|---|---|
| コンテンツ供給 | 1冊を5日分に分割して配信（週1.4冊消費） |
| 画像生成 | HTML/CSS テンプレート → Playwright で PNG 化 |
| 原稿生成 | 書名のみ入力 → 書誌データを根拠に Claude API が全文生成 |
| 実行環境 | GitHub Actions cron（無料・PC非依存） |
| レビュー | 週次一括（日曜生成 → PR でレビュー → マージで承認） |
| デザイン | ダーク＋高コントラスト |
| 投稿仕様 | 毎朝 7:00 JST / カルーセル5枚 / 1080×1350 (4:5) |

## 3. システム構成

```
books/queue.yaml  ←  あなたが本を追加（書名 + ISBN任意）
      │
      │  [日曜 20:00 JST] weekly-generate.yml
      ▼
  ① 書誌データ取得   openBD API + Google Books API
                     （著者/出版社/内容紹介/目次/ページ数）
      ▼
  ② 原稿生成         Claude API (claude-opus-5)
                     material を根拠に5日分 × カルーセル5枚 + キャプション
      ▼
  ③ 画像生成         Jinja2 → HTML → Playwright → PNG (1080×1350)
      ▼
  ④ 成果物配置       drafts/YYYY-MM-DD/*.json
                     docs/img/YYYY-MM-DD/*.jpg   （GitHub Pages で公開）
                     output/preview-YYYY-Www.html （レビュー用）
      ▼
  ⑤ PR 自動作成      プレビューURL付き
      │
      │  ★ あなたがレビュー → マージ = 承認
      ▼
  [毎日 07:00 JST ごろ] daily-post.yml（予約は 06:00。実行の遅れを見込む）
      ▼
  ⑥ 投稿             Instagram Graph API
                     /media (×5) → /media (CAROUSEL) → /media_publish
      ▼
  ⑦ 記録             posted.jsonl に追記。失敗時は Issue 自動作成
```

## 4. 1冊 → 5日分の分割構成

| Day | テーマ | 狙い |
|---|---|---|
| 1 | 【この本、こんな本】 | 概要・読むべき人・基本情報 |
| 2 | 【要点①】 | 中心となる主張 |
| 3 | 【要点②】 | 印象に残ったポイント |
| 4 | 【キーワード3選】 | 用語・概念の解説 |
| 5 | 【総括】 | 評価・誰におすすめ・次の1冊 |

※ Day4 を「引用」ではなく「キーワード解説」にしているのは意図的。
　 引用文はAI生成で捏造リスクが最も高いため、原文の再現を要求しない構成にした。

### カルーセル5枚の構成（全Day共通）

| 枚目 | 役割 |
|---|---|
| 1 | フック（大見出し＋書名） |
| 2〜4 | 本文（1枚1メッセージ） |
| 5 | まとめ＋フォロー導線 |

## 5. ハルシネーション対策（4層）

1. **根拠データの強制**: openBD / Google Books から内容紹介・目次・著者略歴を取得し、
   material として Claude に渡す。両方から取得できない本は生成を中止して Issue 通知。
2. **プロンプト制約**: material に存在しない固有名詞・数値・引用を書かせない。
   断定を避けた文体（「〜と論じられています」）を強制。
3. **grounding フィールド**: 生成JSONに各主張の根拠箇所を出力させ、レビュー画面に併記。
4. **週次人間レビュー**: PR マージが投稿の必須条件。未マージなら投稿しない。

## 6. ディレクトリ構成

```
Instagram/
├─ .github/workflows/
│   ├─ weekly-generate.yml   日曜20:00 生成＋PR作成
│   └─ daily-post.yml        毎朝07:00ごろ 投稿（予約は06:00）
├─ src/bookgram/
│   ├─ config.py             設定・環境変数
│   ├─ bookdata.py           openBD / Google Books
│   ├─ generate.py           Claude API による原稿生成
│   ├─ render.py             HTML → PNG
│   ├─ publish.py            Instagram Graph API
│   ├─ queue.py              キュー管理・スケジューリング
│   ├─ preview.py            レビュー用HTML生成
│   └─ cli.py                コマンドライン入口
├─ templates/                カード用HTML/CSS
├─ books/queue.yaml          本のキュー（あなたが編集）
├─ drafts/YYYY-MM-DD/        生成された下書き（JSON）
├─ docs/                     GitHub Pages（画像ホスティング）
├─ output/                   閲覧用成果物（週次プレビュー）
└─ posted.jsonl              投稿履歴
```

## 7. 必要なシークレット（GitHub Secrets）

| 名前 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | 原稿生成 |
| `IG_USER_ID` | Instagram ビジネスアカウントID |
| `IG_ACCESS_TOKEN` | 長期アクセストークン（60日有効） |

`GITHUB_TOKEN` は Actions が自動発行するため設定不要。

## 8. 運用上の制約

- Instagram Content Publishing API: 24時間あたり50投稿まで（毎日1投稿なら余裕）
- 画像は公開HTTPS URLが必要 → GitHub Pages を使用
- カルーセルは2〜10枚、全て同じアスペクト比が望ましい
- 長期アクセストークンは60日で失効 → 期限14日前に Issue で通知

## 9. コスト見積

| 項目 | 月額 |
|---|---|
| GitHub Actions | 0円（パブリックリポジトリは無制限） |
| GitHub Pages | 0円 |
| Claude API (claude-opus-5) | 約200〜300円（月6冊生成） |
| **合計** | **約300円/月** |

内訳: 1冊あたり input ~5K / output ~8K tokens。$5/$25 per MTok → 約$0.23/冊。

## 10. スコープ外（今回は作らない）

- Instagram ストーリーズ・リール投稿
- コメント自動返信
- フォロワー分析ダッシュボード
- 複数アカウント運用
