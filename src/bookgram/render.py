"""原稿を HTML テンプレート経由で正方形カード画像に変換する。

カード構成（10枚）:
  1 表紙 / 2 書誌情報 / 3 こんな方におすすめ / 4 問いかけ
  5-8 本文 / 9 まとめ / 10 フォロー導線

背景画像は assets/backgrounds/ から書名をもとに決定的に選ぶ。
同じ本なら何度描き直しても同じ絵になり、本が変われば見た目も変わる。
"""

from __future__ import annotations

import base64
import hashlib
import html
import mimetypes
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from playwright.sync_api import sync_playwright

from .config import (
    BACKGROUNDS_DIR,
    CARD_HEIGHT,
    CARD_WIDTH,
    CARDS_PER_POST,
    STORY_HEIGHT,
    STORY_WIDTH,
    TEMPLATES_DIR,
    find_profile_icon,
    load_account,
)

JPEG_QUALITY = 92
STORY_FILENAME = "story.jpg"
LONG_TEXT_CHARS = 34
COVER_TIMEOUT = 30


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )


@lru_cache(maxsize=32)
def _data_uri(path: Path) -> str:
    """画像をデータURIにする。Playwright に外部参照をさせないため。"""
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _file_data_uri(path: Path) -> str:
    """キャッシュせずに読む。カード画像は生成のたびに中身が変わるため。"""
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _remote_data_uri(url: str) -> str:
    if not url:
        return ""
    try:
        response = requests.get(
            url, timeout=COVER_TIMEOUT, headers={"User-Agent": "bookgram/1.0"}
        )
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"[warn] 書影を取得できませんでした: {type(error).__name__}")
        return ""
    mime = response.headers.get("content-type", "image/jpeg").split(";")[0]
    return f"data:{mime};base64," + base64.b64encode(response.content).decode()


def _backgrounds() -> list[Path]:
    if not BACKGROUNDS_DIR.exists():
        return []
    return sorted(p for p in BACKGROUNDS_DIR.glob("*.jpg"))


def _pick_background(seed: str, index: int) -> str:
    """書名とスライド位置から背景を決定的に選ぶ。"""
    images = _backgrounds()
    if not images:
        return ""
    offset = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return _data_uri(images[(offset + index) % len(images)])


def _highlighted(text: str, highlight: str) -> Markup:
    """ハイライト部分だけ色を変えた HTML を作る。"""
    escaped = html.escape(text)
    if not highlight:
        return Markup(escaped)
    target = html.escape(highlight)
    if target not in escaped:
        return Markup(escaped)
    return Markup(escaped.replace(target, f'<span class="hl">{target}</span>', 1))


def build_card_contexts(post: dict[str, Any]) -> list[dict[str, Any]]:
    """10枚分のテンプレート変数を組み立てる。"""
    account = load_account()
    seed = post["book_title"]
    icon_path = find_profile_icon()
    icon = _data_uri(icon_path) if icon_path else ""
    cover_image = post.get("cover_data_uri", "")

    base = {
        "width": CARD_WIDTH,
        "height": CARD_HEIGHT,
        "top_note": account["top_note"],
        "cover_tag": account["cover_tag"],
        "handle": account["handle"],
        "account_name": account["name"],
        "outro_text": account["outro_text"].strip(),
        "icon": icon,
        "book_title": post["book_title"],
        "book_author": post["book_author"],
        "published": post.get("published", ""),
        "cover_image": cover_image,
    }

    def slide(index: int, variant: str, **extra: Any) -> dict[str, Any]:
        return {
            **base,
            "bg": _pick_background(seed, index),
            "variant": variant,
            "long_text": False,
            "text_html": Markup(""),
            "items": [],
            **extra,
        }

    def text_slide(index: int, data: dict[str, str], variant: str = "text") -> dict[str, Any]:
        return slide(
            index,
            variant,
            text_html=_highlighted(data["text"], data.get("highlight", "")),
            long_text=len(data["text"]) > LONG_TEXT_CHARS,
        )

    contexts = [
        text_slide(0, post["cover"], variant="cover"),
        slide(1, "biblio"),
        slide(
            2,
            "recommend",
            items=[
                _highlighted(item["text"], item.get("highlight", ""))
                for item in post["recommend"]
            ],
        ),
        text_slide(3, post["question"]),
    ]
    contexts += [
        text_slide(4 + i, point) for i, point in enumerate(post["points"])
    ]
    contexts.append(text_slide(8, post["summary"]))
    contexts.append(slide(9, "outro"))
    return contexts


def render_post(post: dict[str, Any], out_dir: Path) -> list[Path]:
    """1投稿分のカード画像を書き出してパスを返す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = _env().get_template("card.html.j2")
    contexts = build_card_contexts(post)
    paths: list[Path] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
            device_scale_factor=1,
        )
        for i, context in enumerate(contexts, start=1):
            page.set_content(template.render(**context), wait_until="load")
            path = out_dir / f"{i:02d}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY)
            paths.append(path)
        browser.close()

    if len(paths) != CARDS_PER_POST:
        raise RuntimeError(f"カード枚数が想定と異なります: {len(paths)}")
    return paths


def render_feature(post: dict[str, Any], out_dir: Path) -> list[Path]:
    """新刊特集のカードを書き出す（表紙 + 書籍数 + まとめ）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = _env().get_template("feature.html.j2")
    account = load_account()
    icon_path = find_profile_icon()
    books = post["books"]
    seed = post["period_label"]
    parts = post.get("period_parts") or {}

    base = {
        "width": CARD_WIDTH,
        "height": CARD_HEIGHT,
        "icon": _data_uri(icon_path) if icon_path else "",
        "handle": account["handle"],
        "period_label": post["period_label"],
        "period_year": parts.get("year", ""),
        "period_month": parts.get("month", ""),
        "period_half": parts.get("half", ""),
        "cover_lead": post.get("cover_lead")
        or account.get("feature_lead", "楽しみにしている"),
        "book_count": len(books),
    }

    contexts = [
        {**base, "variant": "fcover", "bg": _pick_background(seed, 0), "cover_image": ""}
    ]
    for i, book in enumerate(books):
        contexts.append(
            {
                **base,
                "variant": "fbook",
                "bg": _pick_background(seed, i + 1),
                "cover_image": book.get("cover_data_uri", ""),
                "book_title": book["title"],
                "book_author": book["author"],
                "sales_date": book["sales_date"],
                "point": book["point"],
            }
        )
    contexts.append(
        {
            **base,
            "variant": "fsummary",
            "bg": _pick_background(seed, len(books) + 1),
            "cover_image": "",
            "items": books,
        }
    )

    paths: list[Path] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
            device_scale_factor=1,
        )
        for i, context in enumerate(contexts, start=1):
            page.set_content(template.render(**context), wait_until="load")
            path = out_dir / f"{i:02d}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY)
            paths.append(path)
        browser.close()
    return paths


def story_line_for(post: dict[str, Any]) -> str:
    """ストーリーに載せる一言。無ければ表紙の文言で代用する。"""
    line = (post.get("story_line") or "").strip()
    if line:
        return line
    cover = post.get("cover")
    if isinstance(cover, dict) and cover.get("text"):
        return cover["text"].replace(chr(10), " ")
    return post.get("book_title") or post.get("period_label", "")


def build_story_context(post: dict[str, Any], feed_image: str = "") -> dict[str, Any]:
    account = load_account()
    icon_path = find_profile_icon()
    seed = post.get("book_title") or post.get("period_label", "")
    return {
        "width": STORY_WIDTH,
        "height": STORY_HEIGHT,
        "bg": _pick_background(seed, 0),
        "feed_image": feed_image,
        "icon": _data_uri(icon_path) if icon_path else "",
        "label": account.get("story_label", "New Post！"),
        "line": story_line_for(post),
        "cta": account.get("story_cta", "詳しくはフィード投稿から →"),
        "handle": account["handle"],
    }


def render_story(post: dict[str, Any], out_dir: Path) -> Path:
    """ストーリー用の縦長画像を1枚書き出す。

    中央にはフィードの1枚目をそのまま埋め込む。カード生成のあとに呼ぶこと。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    template = _env().get_template("story.html.j2")
    path = out_dir / STORY_FILENAME
    first_card = out_dir / "01.jpg"
    feed_image = _file_data_uri(first_card) if first_card.exists() else ""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": STORY_WIDTH, "height": STORY_HEIGHT},
            device_scale_factor=1,
        )
        page.set_content(
            template.render(**build_story_context(post, feed_image)), wait_until="load"
        )
        page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY)
        browser.close()

    return path


def fetch_cover_data_uri(url: str) -> str:
    """書影URLをデータURIに変換する。取得できなければ空文字。"""
    return _remote_data_uri(url)
