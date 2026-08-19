"""週次「ビジネス書 新刊特集」用に、楽天ブックスから新刊を集める。

楽天の新刊一覧は発売日の降順で引けるが、先頭には発売日未定の
プレースホルダ（2225年など）が並ぶため、目的の日付帯に届くまで
ページを送る必要がある。日付が窓を通り過ぎたら打ち切る。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .bookdata import (
    DEFAULT_RAKUTEN_REFERER,
    RAKUTEN_ENDPOINT,
    _polite_sleep,
    _request,
)

BUSINESS_GENRE_ID = "001006"  # ビジネス・経済・就職
HITS_PER_PAGE = 30
MAX_PAGES = 25
MIN_CAPTION_CHARS = 60
DAYS_BACK = 14
DAYS_AHEAD = 21


class NewBooksUnavailableError(RuntimeError):
    """楽天の認証情報が無い、または新刊が集まらなかった。"""


@dataclass
class NewBook:
    title: str
    author: str
    publisher: str
    sales_date: date
    sales_date_label: str
    isbn: str
    cover_url: str
    caption: str
    tags: list[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        return "\n".join(
            [
                f"書名: {self.title}",
                f"著者: {self.author or '不明'}",
                f"出版社: {self.publisher or '不明'}",
                f"発売日: {self.sales_date_label}",
                "内容紹介:",
                self.caption,
            ]
        )


def parse_sales_date(raw: str) -> date | None:
    """「2024年02月01日頃」「2024年02月」などを date にする。"""
    match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw or "")
    if match:
        try:
            return date(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    match = re.search(r"(\d{4})年(\d{1,2})月", raw or "")
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), 15)
        except ValueError:
            return None
    return None


def format_sales_date(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _auth() -> dict[str, str]:
    app_id = os.getenv("RAKUTEN_APP_ID", "").strip()
    access_key = os.getenv("RAKUTEN_ACCESS_KEY", "").strip()
    if not app_id or not access_key:
        raise NewBooksUnavailableError(
            "新刊特集には楽天の RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が必要です。"
        )
    return {"applicationId": app_id, "accessKey": access_key}


def fetch_new_business_books(
    today: date | None = None,
    *,
    days_back: int = DAYS_BACK,
    days_ahead: int = DAYS_AHEAD,
    limit: int = 12,
) -> list[NewBook]:
    """指定期間に発売される（された）ビジネス書を集める。"""
    auth = _auth()
    today = today or date.today()
    low, high = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
    site = os.getenv("RAKUTEN_REFERER", DEFAULT_RAKUTEN_REFERER)
    headers = {"Referer": site, "Origin": site}

    found: dict[str, NewBook] = {}
    for page in range(1, MAX_PAGES + 1):
        payload = _request(
            RAKUTEN_ENDPOINT,
            {
                **auth,
                "booksGenreId": BUSINESS_GENRE_ID,
                "sort": "-releaseDate",
                "hits": HITS_PER_PAGE,
                "page": page,
                "format": "json",
                "formatVersion": 2,
            },
            headers,
        ).json()

        items = payload.get("Items") or []
        if not items:
            break

        dates = [parse_sales_date(item.get("salesDate", "")) for item in items]
        for item, sales_date in zip(items, dates):
            if sales_date is None or not (low <= sales_date <= high):
                continue
            caption = (item.get("itemCaption") or "").strip()
            cover = item.get("largeImageUrl") or item.get("mediumImageUrl") or ""
            if len(caption) < MIN_CAPTION_CHARS or not cover:
                continue
            isbn = item.get("isbn") or item.get("title", "")
            found.setdefault(
                isbn,
                NewBook(
                    title=item.get("title", "").strip(),
                    author=(item.get("author") or "").replace("/", "、").strip(),
                    publisher=item.get("publisherName", ""),
                    sales_date=sales_date,
                    sales_date_label=format_sales_date(sales_date),
                    isbn=isbn,
                    cover_url=cover,
                    caption=caption,
                ),
            )

        known = [d for d in dates if d]
        if known and min(known) < low:
            break
        _polite_sleep()

    books = sorted(found.values(), key=lambda b: b.sales_date)
    if not books:
        raise NewBooksUnavailableError(
            f"{low} 〜 {high} に該当するビジネス書の新刊が見つかりませんでした。"
        )
    return books[:limit]


def period_label(today: date) -> str:
    """「26年8月後半」のような期間ラベルを作る。"""
    half = "前半" if today.day <= 15 else "後半"
    return f"{today.year % 100}年{today.month}月{half}"


def to_prompt_blocks(books: list[NewBook]) -> str:
    return "\n\n".join(
        f"### {i}冊目\n{book.to_prompt_block()}" for i, book in enumerate(books, start=1)
    )


def as_dicts(books: list[NewBook]) -> list[dict[str, Any]]:
    return [
        {
            "title": b.title,
            "author": b.author,
            "publisher": b.publisher,
            "sales_date": b.sales_date_label,
            "isbn": b.isbn,
            "cover_url": b.cover_url,
        }
        for b in books
    ]
