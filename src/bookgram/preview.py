"""週次レビュー用のプレビューページを生成する。

実体は docs/preview/<週>.html（GitHub Pages で配信され、画像の相対パスが解決できる）。
output/index.html はそこへのリンク集で、こちらが日常的な入口になる。
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import CARDS_PER_POST, DOCS_DIR, OUTPUT_DIR, PAGES_PREVIEW_DIRNAME

NEWLINE = chr(10)

PAGE_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 48px 32px 96px;
  background: #0B0E14; color: #F2F5F9;
  font-family: "Noto Sans JP", "Hiragino Sans", "Yu Gothic", system-ui, sans-serif;
  line-height: 1.7;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 30px; margin: 0 0 8px; letter-spacing: 0.02em; }
.lede { color: #8C97A8; margin: 0 0 40px; font-size: 15px; }
.day {
  border: 1px solid #232B39; border-radius: 16px;
  padding: 28px; margin-bottom: 36px; background: #101521;
}
.day-head {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 14px;
  padding-bottom: 18px; margin-bottom: 22px; border-bottom: 1px solid #232B39;
}
.date { font-size: 22px; font-weight: 800; }
.badge {
  font-size: 13px; font-weight: 700; color: #F5B94A;
  border: 1px solid #F5B94A; border-radius: 999px; padding: 4px 14px;
}
.book { color: #8C97A8; font-size: 14px; }
.cards {
  display: flex; gap: 14px; overflow-x: auto;
  padding-bottom: 12px; margin-bottom: 22px;
}
.cards img {
  width: 232px; height: 290px; flex: 0 0 auto;
  border-radius: 10px; border: 1px solid #232B39; object-fit: cover;
}
h3 { font-size: 14px; color: #8C97A8; margin: 22px 0 8px; letter-spacing: 0.08em; }
pre.caption {
  white-space: pre-wrap; word-break: break-word; margin: 0;
  background: #0B0E14; border: 1px solid #232B39; border-radius: 10px;
  padding: 18px; font-family: inherit; font-size: 14px;
}
.tags { display: flex; flex-wrap: wrap; gap: 8px; }
.tag {
  font-size: 13px; color: #A8B3C4;
  background: #0B0E14; border: 1px solid #232B39;
  border-radius: 6px; padding: 4px 10px;
}
ul.grounding { margin: 0; padding-left: 20px; font-size: 13px; color: #8C97A8; }
ul.grounding li { margin-bottom: 4px; }
a { color: #F5B94A; }
.links li { margin-bottom: 10px; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _slide_lines(draft: dict[str, Any]) -> list[str]:
    """カードに載る文言を、並び順どおりに取り出す。"""
    if draft.get("kind") == "feature":
        top = draft.get("period_parts", {}).get("year") or draft["period_label"]
        lines = [
            f"1 表紙: {draft['cover_lead']} / {top}"
            f"{draft.get('cover_main', 'ビジネス書')}"
            f"{draft.get('count_word', '新刊')}{len(draft['books'])}選"
        ]
        label = draft.get("point_label", "注目")
        for i, book in enumerate(draft["books"], start=2):
            fact = book.get("fact_value") or book["sales_date"]
            lines.append(
                f"{i} {book['title']}（{fact} / {book['author']}）"
                f" {label}: {book['point']}"
            )
        lines.append(f"{len(draft['books']) + 2} まとめ: チェックリスト")
        return lines

    lines = [f"1 表紙: {draft['cover']['text']}"]
    lines.append(f"2 書誌情報: {draft.get('published', '—')} / {draft['book_author']}")
    for i, item in enumerate(draft["recommend"], start=1):
        lines.append(f"3 おすすめ{i}: {item['text']}")
    lines.append(f"4 問いかけ: {draft['question']['text']}")
    for i, point in enumerate(draft["points"], start=5):
        lines.append(f"{i} 本文: {point['text']}")
    lines.append(f"9 まとめ: {draft['summary']['text']}")
    return lines


def _render_day_block(
    day_date: date, draft: dict[str, Any], img_base: str = "../img"
) -> str:
    """1日分の枠。img_base はページの置き場所からの画像への道のり。

    docs/preview/ に置くページは ../img、docs/ 直下のページは img になる。
    ここを固定にしていたため、upcoming.html から画像がサイトの外を指して
    404 になっていた。
    """
    count = draft.get("image_count", CARDS_PER_POST)
    images = "".join(
        f'<img src="{img_base}/{day_date.isoformat()}/{i:02d}.jpg"'
        f' alt="card {i}" loading="lazy">'
        for i in range(1, count + 1)
    )
    slides = "".join(f"<li>{_esc(line)}</li>" for line in _slide_lines(draft))
    tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in draft.get("hashtags", []))
    grounding = "".join(f"<li>{_esc(g)}</li>" for g in draft.get("grounding", []))

    return f"""
    <section class="day">
      <div class="day-head">
        <span class="date">{day_date.strftime('%m/%d (%a)')}</span>
        <span class="badge">{_esc(draft.get('book_title') or draft.get('period_label'))}</span>
        <span class="book">{_esc(draft.get('book_author') or draft.get('feature_name') or '特集')}</span>
      </div>
      <div class="cards">{images}</div>
      <h3>カードの文言</h3>
      <ul class="grounding">{slides}</ul>
      <h3>キャプション</h3>
      <pre class="caption">{_esc(draft.get('caption', ''))}</pre>
      <h3>書籍固有ハッシュタグ</h3>
      <div class="tags">{tags}</div>
      <h3>根拠メモ (grounding)</h3>
      <ul class="grounding">{grounding}</ul>
    </section>
    """


def render_week_preview(
    week_label: str, drafts: list[tuple[date, dict[str, Any]]]
) -> Path:
    """1週間分のプレビューページを docs/preview/ に書き出す。"""
    blocks = "".join(_render_day_block(day, draft) for day, draft in drafts)
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>投稿プレビュー {_esc(week_label)}</title>
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>投稿プレビュー {_esc(week_label)}</h1>
<p class="lede">
  内容を確認して、問題なければ Pull Request をマージしてください。
  マージされた日付だけが自動投稿されます。修正する場合は drafts/ 以下の JSON を直接編集してください。
</p>
{blocks}
</div></body></html>
"""
    out_dir = DOCS_DIR / PAGES_PREVIEW_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{week_label}.html"
    path.write_text(page, encoding="utf-8")
    return path


def render_upcoming(days: int = 14) -> Path:
    """これから配信する分だけを1枚にまとめる。URL は常に同じ。

    週ごとのページは過去の投稿も混ざるうえ、どれが次の週か探す手間がある。
    「まだ投稿していない日」だけを日付順に並べた固定のページを置く。
    """
    from .queue import load_draft

    today = date.today()
    rows: list[tuple[date, dict[str, Any]]] = []
    cursor = today
    for _ in range(days * 3):  # 空き日を飛ばすため、暦としては長めに走査する
        if len(rows) >= days:
            break
        draft = load_draft(cursor)
        if draft and draft.get("status") != "posted":
            rows.append((cursor, draft))
        cursor += timedelta(days=1)

    blocks = "".join(
        _render_day_block(day, draft, img_base="img") for day, draft in rows
    )
    empty = "<p>これから配信する下書きはありません。</p>"
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>これから配信する投稿</title>
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>これから配信する投稿</h1>
<p class="lede">
  まだ投稿していない下書きを、日付の早い順に{days}件まで並べています。
  投稿が済んだ分は自動的に消えます。更新日時: {_esc(datetime.now().strftime("%Y-%m-%d %H:%M"))}
</p>
{blocks or empty}
</div></body></html>
"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCS_DIR / "upcoming.html"
    path.write_text(page, encoding="utf-8")
    return path


def _index_html(links_html: str) -> str:
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>読書Instagram 自動投稿</title>
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>読書Instagram 自動投稿</h1>
<p class="lede">週次で生成された投稿プレビューの一覧です。</p>
<ul class="links">{links_html}</ul>
</div></body></html>
"""


def render_index(pages_base_url: str) -> list[Path]:
    """トップページを2箇所に書き出す。

    docs/index.html   GitHub Pages が配信する実体。リンクは相対パス。
    output/index.html 手元から開く用の入口。リンクは絶対URL。
    """
    preview_dir = DOCS_DIR / PAGES_PREVIEW_DIRNAME
    pages = sorted(preview_dir.glob("*.html"), reverse=True) if preview_dir.exists() else []
    empty = "<li>まだプレビューはありません。日曜の週次生成を待つか、Actions から手動実行してください。</li>"

    top = (
        '<p style="margin:0 0 28px"><a href="upcoming.html" '
        'style="display:inline-block;padding:14px 26px;border-radius:10px;'
        'background:#F5B94A;color:#101521;font-weight:800;text-decoration:none">'
        'これから配信する投稿を見る</a></p>'
    )

    relative = "".join(
        f'<li><a href="{PAGES_PREVIEW_DIRNAME}/{_esc(p.name)}">'
        f"{_esc(p.stem)} の投稿プレビュー</a></li>"
        for p in pages
    )
    absolute = "".join(
        f'<li><a href="{pages_base_url}/{PAGES_PREVIEW_DIRNAME}/{_esc(p.name)}">'
        f"{_esc(p.stem)} の投稿プレビュー</a></li>"
        for p in pages
    )

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    docs_path = DOCS_DIR / "index.html"
    output_path = OUTPUT_DIR / "index.html"
    docs_path.write_text(_index_html(top + (relative or empty)), encoding="utf-8")
    output_path.write_text(
        _index_html(
            top.replace("upcoming.html", f"{pages_base_url}/upcoming.html")
            + (absolute or empty)
        ),
        encoding="utf-8",
    )
    return [docs_path, output_path]


def render_pr_body(
    week_label: str,
    drafts: list[tuple[date, dict[str, Any]]],
    repo: str,
    branch: str,
) -> Path:
    """PR本文用のMarkdownを書き出す。

    画像は raw.githubusercontent.com のブランチURLで参照するため、
    マージ前でもPR画面上で現物を確認できる。
    """
    raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}/docs/img"
    parts = [
        f"## {week_label} の投稿下書き",
        "",
        f"{len(drafts)} 投稿分（1投稿 = 1冊）。",
        "",
        "**このPRをマージした日付だけが、毎朝7時に自動投稿されます。**",
        "修正する場合は `drafts/` 以下の JSON をこのブランチ上で直接編集してください。",
        "",
        "### レビュー観点",
        "",
        "- [ ] 根拠データに無い事実を書いていないか（各日の grounding を確認）",
        "- [ ] 文字が画像からはみ出していないか",
        "- [ ] キャプションとハッシュタグが妥当か",
        "",
        "---",
        "",
    ]

    for day, draft in drafts:
        iso = day.isoformat()
        count = draft.get("image_count", CARDS_PER_POST)
        images = " ".join(
            f'<img src="{raw_base}/{iso}/{i:02d}.jpg" width="150">'
            for i in range(1, count + 1)
        )
        slides = NEWLINE.join(f"> {line}" for line in _slide_lines(draft))
        grounding = NEWLINE.join(f"> - {g}" for g in draft.get("grounding", []))
        parts += [
            f"### {day.strftime('%m/%d (%a)')} — "
            f"{draft.get('book_title') or draft.get('period_label')}"
            f" / {draft.get('book_author') or '新刊特集'}",
            "",
            images,
            "",
            "<details><summary>カードの文言</summary>",
            "",
            slides,
            "",
            "</details>",
            "",
            "<details><summary>キャプション</summary>",
            "",
            "```",
            draft.get("caption", ""),
            "",
            " ".join(draft.get("hashtags", [])),
            "```",
            "",
            "</details>",
            "",
            "<details><summary>根拠メモ (grounding)</summary>",
            "",
            grounding,
            "",
            "</details>",
            "",
            "---",
            "",
        ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "pr-body.md"
    path.write_text(NEWLINE.join(parts), encoding="utf-8")
    return path


def load_week_drafts(days: list[date]) -> list[tuple[date, dict[str, Any]]]:
    from .queue import draft_path

    result = []
    for day in days:
        path = draft_path(day)
        if path.exists():
            result.append((day, json.loads(path.read_text(encoding="utf-8"))))
    return result
