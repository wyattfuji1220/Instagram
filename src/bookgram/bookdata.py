"""書誌データ取得。openBD（日本の書籍データベース）と Google Books API を併用する。

生成AIに渡す「根拠データ(material)」を組み立てるのがこのモジュールの役割。
両APIとも取得できなかった場合は例外を投げ、知らない本を書かせないようにする。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any

import requests

OPENBD_ENDPOINT = "https://api.openbd.jp/v1/get"
GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
USER_AGENT = "bookgram/1.0 (personal reading log)"
TIMEOUT = 15


class BookNotFoundError(RuntimeError):
    """書誌データがどのソースからも取得できなかった。"""


@dataclass
class BookMaterial:
    title: str
    isbn: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    published_date: str = ""
    page_count: int = 0
    description: str = ""
    table_of_contents: str = ""
    author_bio: str = ""
    categories: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt_block(self) -> str:
        """Claude に渡す根拠テキスト。ここに無い情報は書かせない。"""
        lines = [
            f"書名: {self.title}",
            f"著者: {', '.join(self.authors) or '不明'}",
            f"出版社: {self.publisher or '不明'}",
            f"出版日: {self.published_date or '不明'}",
            f"ページ数: {self.page_count or '不明'}",
            f"ISBN: {self.isbn or '不明'}",
            f"ジャンル: {', '.join(self.categories) or '不明'}",
        ]
        if self.description:
            lines += ["", "【出版社による内容紹介】", self.description]
        if self.table_of_contents:
            lines += ["", "【目次】", self.table_of_contents]
        if self.author_bio:
            lines += ["", "【著者略歴】", self.author_bio]
        return "\n".join(lines)

    def has_substance(self) -> bool:
        """紹介文を書くのに足る情報があるか。"""
        return len(self.description) + len(self.table_of_contents) >= 80


def _polite_sleep() -> None:
    time.sleep(random.uniform(1.0, 3.0))


def _get_json(url: str, params: dict[str, Any]) -> Any:
    response = requests.get(
        url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    return response.json()


def _normalize_isbn(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isdigit() or ch in "Xx").upper()


def search_google_books(title: str, isbn: str = "") -> dict[str, Any]:
    query = f"isbn:{isbn}" if isbn else f"intitle:{title}"
    payload = _get_json(
        GOOGLE_BOOKS_ENDPOINT, {"q": query, "maxResults": 5, "langRestrict": "ja"}
    )
    items = payload.get("items") or []
    if not items:
        return {}

    if isbn:
        return items[0].get("volumeInfo", {})

    # タイトル検索は同名異書が混ざるため、説明文の充実した候補を優先する。
    best = max(
        items,
        key=lambda item: len(item.get("volumeInfo", {}).get("description", "") or ""),
    )
    return best.get("volumeInfo", {})


def fetch_openbd(isbn: str) -> dict[str, Any]:
    payload = _get_json(OPENBD_ENDPOINT, {"isbn": isbn})
    if not payload or payload[0] is None:
        return {}
    return payload[0]


def _extract_openbd_texts(record: dict[str, Any]) -> dict[str, str]:
    """openBD の ONIX 構造から内容紹介・目次・著者略歴を取り出す。

    TextType: 03=内容紹介, 04=目次, 23=著者略歴（ONIX コードリスト153）
    """
    type_map = {"03": "description", "04": "table_of_contents", "23": "author_bio"}
    result: dict[str, str] = {}

    collateral = record.get("onix", {}).get("CollateralDetail", {})
    for entry in collateral.get("TextContent", []) or []:
        key = type_map.get(entry.get("TextType", ""))
        text = (entry.get("Text") or "").strip()
        if key and text and len(text) > len(result.get(key, "")):
            result[key] = text
    return result


def fetch_material(title: str, isbn: str = "") -> BookMaterial:
    """1冊分の書誌データを取得してマージする。"""
    material = BookMaterial(title=title, isbn=_normalize_isbn(isbn))

    volume = search_google_books(title, material.isbn)
    if volume:
        material.sources.append("google_books")
        material.title = volume.get("title") or material.title
        if volume.get("subtitle"):
            material.title = f"{material.title} {volume['subtitle']}"
        material.authors = volume.get("authors") or []
        material.publisher = volume.get("publisher") or ""
        material.published_date = volume.get("publishedDate") or ""
        material.page_count = volume.get("pageCount") or 0
        material.description = (volume.get("description") or "").strip()
        material.categories = volume.get("categories") or []
        if not material.isbn:
            for ident in volume.get("industryIdentifiers", []) or []:
                if ident.get("type") == "ISBN_13":
                    material.isbn = ident.get("identifier", "")
                    break

    if material.isbn:
        _polite_sleep()
        record = fetch_openbd(material.isbn)
        if record:
            material.sources.append("openbd")
            summary = record.get("summary", {}) or {}
            material.title = summary.get("title") or material.title
            if summary.get("author") and not material.authors:
                material.authors = [
                    name.strip() for name in summary["author"].split("/") if name.strip()
                ]
            material.publisher = material.publisher or summary.get("publisher", "")
            material.published_date = material.published_date or summary.get("pubdate", "")

            texts = _extract_openbd_texts(record)
            # openBD の日本語内容紹介は Google Books より詳しいことが多いので優先。
            if len(texts.get("description", "")) > len(material.description):
                material.description = texts["description"]
            material.table_of_contents = texts.get("table_of_contents", "")
            material.author_bio = texts.get("author_bio", "")

    if not material.has_substance():
        raise BookNotFoundError(
            f"『{title}』の書誌データが不足しています "
            f"(sources={material.sources or 'なし'})。"
            "ISBNを books/queue.yaml に追記するか、手動で notes を書いてください。"
        )

    return material
