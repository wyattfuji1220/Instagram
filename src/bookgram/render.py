"""生成された原稿を HTML テンプレート経由で PNG/JPEG カードに変換する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from .config import CARD_HEIGHT, CARD_WIDTH, CARDS_PER_POST, TEMPLATES_DIR

JPEG_QUALITY = 92


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )


def _variant(index: int, total: int) -> str:
    if index == 1:
        return "hook"
    if index == total:
        return "outro"
    return "body"


def build_card_contexts(
    day: dict[str, Any], book_title: str, book_author: str, one_line: str
) -> list[dict[str, Any]]:
    """1日分のカード枚数ぶんのテンプレート変数を組み立てる。"""
    cards = day["cards"]
    total = len(cards)
    contexts = []

    for i, card in enumerate(cards, start=1):
        variant = _variant(i, total)
        context = {
            "width": CARD_WIDTH,
            "height": CARD_HEIGHT,
            "card_index": i,
            "card_total": total,
            "variant": variant,
            "kicker": card["kicker"],
            "headline": card["headline"],
            "body": card["body"],
            "one_line": one_line,
            "book_title": book_title,
            "book_author": book_author,
            "cta_title": "",
            "cta_text": "",
        }
        if variant == "hook":
            context["kicker"] = f"Day {day['day_index']} / {day['theme']}"
        if variant == "outro":
            context["cta_title"] = "保存して、あとで読み返す"
            context["cta_text"] = (
                "読んだ本を毎朝1冊分ずつ紹介しています。"
                "続きが気になったらフォローしてください。"
            )
        contexts.append(context)

    return contexts


def render_day(
    day: dict[str, Any],
    book_title: str,
    book_author: str,
    one_line: str,
    out_dir: Path,
) -> list[Path]:
    """1日分のカード画像を書き出してパスを返す。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    template = _env().get_template("card.html.j2")
    contexts = build_card_contexts(day, book_title, book_author, one_line)
    paths: list[Path] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
            device_scale_factor=1,
        )
        for context in contexts:
            page.set_content(template.render(**context), wait_until="load")
            path = out_dir / f"{context['card_index']:02d}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY)
            paths.append(path)
        browser.close()

    if len(paths) != CARDS_PER_POST:
        raise RuntimeError(f"カード枚数が想定と異なります: {len(paths)}")
    return paths
