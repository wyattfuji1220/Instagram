"""週次レビュー用のプレビューページを生成する。

実体は docs/preview/<週>.html（GitHub Pages で配信され、画像の相対パスが解決できる）。
output/index.html はそこへのリンク集で、こちらが日常的な入口になる。
"""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path
from typing import Any

from .config import CARDS_PER_POST, DOCS_DIR, OUTPUT_DIR, PAGES_PREVIEW_DIRNAME

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


def _render_day_block(day_date: date, draft: dict[str, Any]) -> str:
    images = "".join(
        f'<img src="../img/{day_date.isoformat()}/{i:02d}.jpg" alt="card {i}" loading="lazy">'
        for i in range(1, CARDS_PER_POST + 1)
    )
    tags = "".join(f'<span class="tag">{_esc(t)}</span>' for t in draft.get("hashtags", []))
    grounding = "".join(f"<li>{_esc(g)}</li>" for g in draft.get("grounding", []))
    caption = _esc(draft.get("caption", ""))

    return f"""
    <section class="day">
      <div class="day-head">
        <span class="date">{day_date.strftime('%m/%d (%a)')}</span>
        <span class="badge">Day {_esc(draft.get('day_index'))} / {_esc(draft.get('theme'))}</span>
        <span class="book">{_esc(draft.get('book_title'))} — {_esc(draft.get('book_author'))}</span>
      </div>
      <div class="cards">{images}</div>
      <h3>キャプション</h3>
      <pre class="caption">{caption}</pre>
      <h3>ハッシュタグ</h3>
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


def render_index(pages_base_url: str) -> Path:
    """output/index.html に、これまでのプレビューへのリンク集を書き出す。"""
    preview_dir = DOCS_DIR / PAGES_PREVIEW_DIRNAME
    pages = sorted(preview_dir.glob("*.html"), reverse=True) if preview_dir.exists() else []
    items = "".join(
        f'<li><a href="{pages_base_url}/{PAGES_PREVIEW_DIRNAME}/{_esc(p.name)}">'
        f"{_esc(p.stem)} の投稿プレビュー</a></li>"
        for p in pages
    )
    if not items:
        items = "<li>まだプレビューはありません。</li>"

    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>読書Instagram 自動投稿</title>
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>読書Instagram 自動投稿</h1>
<p class="lede">週次で生成された投稿プレビューの一覧です。</p>
<ul class="links">{items}</ul>
</div></body></html>
"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def load_week_drafts(days: list[date]) -> list[tuple[date, dict[str, Any]]]:
    from .queue import draft_path

    result = []
    for day in days:
        path = draft_path(day)
        if path.exists():
            result.append((day, json.loads(path.read_text(encoding="utf-8"))))
    return result
