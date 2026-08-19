"""書誌データ取得。

生成AIに渡す「根拠データ(material)」を組み立てるのがこのモジュールの役割。
根拠が集まらなかった場合は例外を投げ、知らない本を書かせないようにする。

3つのソースを併用する。どれか1つが落ちても残りで成立する設計。

  NDLサーチ    和書の書誌情報とISBN。キー不要で、和書のタイトル解決に最も強い
  楽天ブックス 商用和書の内容紹介。カバー率が最も高い。無料の applicationId が要る
  Google Books 内容紹介が充実していることが多い。匿名アクセスは429になりやすい
  openBD       和書の内容紹介・目次・著者略歴。ISBNが分かっている場合のみ引ける

加えて、あなた自身の読書メモ(notes)も同等の根拠として扱う。
APIが全滅してもメモさえ書けば生成できる。
"""

from __future__ import annotations

import os
import random
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any

import requests

NDL_ENDPOINT = "https://ndlsearch.ndl.go.jp/api/opensearch"
# 2026年時点の楽天ウェブサービスは openapi.rakuten.co.jp に移行しており、
# applicationId(UUID) と accessKey の両方が必須。旧 app.rakuten.co.jp は
# 数字19桁のIDを要求するが、現行コンソールではそれが発行されない。
RAKUTEN_ENDPOINT = (
    "https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404"
)
OPENBD_ENDPOINT = "https://api.openbd.jp/v1/get"
GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
USER_AGENT = "bookgram/1.0 (personal reading log)"
TIMEOUT = 15
RETRY_ATTEMPTS = 3
RETRY_BASE_WAIT = 3.0
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
MIN_SUBSTANCE_CHARS = 80
# 例外メッセージやログに載せてはいけないクエリパラメータ
SECRET_PARAMS = {
    "applicationId",
    "accessKey",
    "key",
    "access_token",
    "affiliateId",
}


class BookNotFoundError(RuntimeError):
    """書誌データがどのソースからも取得できなかった。"""


@dataclass
class BookMaterial:
    # 利用者が queue.yaml に書いた書名。カードに表示するのはこちら。
    title: str
    # 書誌DB上の正式書名（副題込み）。根拠としてのみ使う。
    official_title: str = ""
    isbn: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    published_date: str = ""
    page_count: int = 0
    description: str = ""
    table_of_contents: str = ""
    author_bio: str = ""
    personal_notes: str = ""
    cover_url: str = ""
    categories: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    web_sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt_block(self) -> str:
        """Claude に渡す根拠テキスト。ここに無い情報は書かせない。"""
        lines = [
            f"書名: {self.title}",
            *(
                [f"正式書名（書誌DB上の表記）: {self.official_title}"]
                if self.official_title and self.official_title != self.title
                else []
            ),
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
        if self.web_sources:
            lines += ["", "【上記のWeb情報の出典】"] + [f"- {u}" for u in self.web_sources]
        if self.personal_notes:
            lines += [
                "",
                "【読者本人による読書メモ】",
                "（実際に読んだ本人の記録です。内容紹介と同等の根拠として扱ってください）",
                self.personal_notes,
            ]
        return "\n".join(lines)

    def substance_chars(self) -> int:
        return (
            len(self.description)
            + len(self.table_of_contents)
            + len(self.personal_notes)
        )

    def has_substance(self) -> bool:
        """紹介文を書くのに足る根拠があるか。"""
        return self.substance_chars() >= MIN_SUBSTANCE_CHARS


def _polite_sleep() -> None:
    time.sleep(random.uniform(1.0, 3.0))


def _safe_params(params: dict[str, Any]) -> dict[str, Any]:
    """ログや例外に出しても安全な形にする。"""
    return {
        k: ("***" if k in SECRET_PARAMS else v) for k, v in params.items()
    }


def _request(url: str, params: dict[str, Any]) -> requests.Response:
    """GET する。429/5xx は指数バックオフで再試行する。

    APIキーがURLに乗るため、例外メッセージには秘匿済みの情報だけを載せる。
    requests の HTTPError は生URLを含むので、そのまま外へ出さないこと。
    """
    last_error: Exception | None = None

    for attempt in range(RETRY_ATTEMPTS):
        try:
            response = requests.get(
                url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
            )
        except requests.RequestException as error:
            # 例外文字列にURL（=キー）が入りうるので型名だけ残す
            last_error = requests.RequestException(
                f"{type(error).__name__} while requesting {url}"
            )
        else:
            if response.status_code not in RETRYABLE_STATUS:
                if response.status_code >= 400:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code} from {url} "
                        f"params={_safe_params(params)}"
                    )
                return response
            last_error = requests.HTTPError(
                f"HTTP {response.status_code} from {url} "
                f"params={_safe_params(params)}"
            )

        if attempt < RETRY_ATTEMPTS - 1:
            wait = RETRY_BASE_WAIT * (2**attempt) + random.uniform(0, 1.5)
            print(f"[retry] 一時エラー。{wait:.1f}秒待って再試行します（{attempt + 1}回目）")
            time.sleep(wait)

    raise last_error if last_error else RuntimeError(f"{url} の取得に失敗しました")


def _normalize_isbn(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isdigit() or ch in "Xx").upper()


def _normalize_person(raw: str) -> str:
    """NDL の「相良, 奈美香」形式を「相良奈美香」に整える。"""
    return raw.replace(", ", "").replace(",", "").strip()


# ------------------------------------------------------------------- NDLサーチ


def _ndl_item_fields(item: ET.Element) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "isbn": "",
        "title": "",
        "authors": [],
        "publisher": "",
        "published_date": "",
    }
    for child in item:
        tag = child.tag.split("}")[-1]
        text = (child.text or "").strip()
        if not text:
            continue
        if tag == "identifier" and child.attrib.get(XSI_TYPE, "").endswith("ISBN"):
            fields["isbn"] = _normalize_isbn(text)
        elif tag == "title" and not fields["title"]:
            fields["title"] = text
        elif tag == "creator":
            name = _normalize_person(text)
            if name not in fields["authors"]:
                fields["authors"].append(name)
        elif tag == "publisher" and not fields["publisher"]:
            fields["publisher"] = text
        elif tag == "date" and not fields["published_date"]:
            fields["published_date"] = text
    return fields


def search_ndl(title: str) -> dict[str, Any]:
    """国立国会図書館サーチで和書を検索する。ISBNを持つ最初の候補を返す。

    検索結果には書評や雑誌記事が混ざるため、ISBNを持つ＝図書である候補に絞る。
    """
    response = _request(NDL_ENDPOINT, {"title": title, "cnt": 10})
    root = ET.fromstring(response.content)
    for item in root.findall(".//item"):
        fields = _ndl_item_fields(item)
        if fields["isbn"]:
            return fields
    return {}


# ------------------------------------------------------------------ 楽天ブックス


def search_rakuten(title: str, isbn: str = "") -> dict[str, Any]:
    """楽天ブックスで検索する。商用和書の内容紹介(itemCaption)のカバー率が高い。

    RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY の両方が要る。
    どちらか欠けていれば何もしない（他ソースで続行する）。
    """
    app_id = os.getenv("RAKUTEN_APP_ID", "")
    access_key = os.getenv("RAKUTEN_ACCESS_KEY", "")
    if not app_id or not access_key:
        return {}

    params: dict[str, Any] = {
        "applicationId": app_id,
        "accessKey": access_key,
        "hits": 5,
        "format": "json",
        "formatVersion": 2,
    }
    if isbn:
        params["isbn"] = isbn
    else:
        params["title"] = title

    items = _request(RAKUTEN_ENDPOINT, params).json().get("Items") or []
    if not items:
        return {}

    # formatVersion=2 では Items が直接アイテムの配列になる。
    entries = [item.get("Item", item) for item in items]
    best = max(entries, key=lambda e: len(e.get("itemCaption", "") or ""))
    return best


def _apply_rakuten(material: BookMaterial, entry: dict[str, Any]) -> None:
    material.sources.append("rakuten")
    material.official_title = entry.get("title") or material.official_title
    if entry.get("author") and not material.authors:
        material.authors = [
            _normalize_person(name)
            for name in entry["author"].replace("/", "、").split("、")
            if name.strip()
        ]
    material.publisher = material.publisher or entry.get("publisherName", "")
    material.published_date = material.published_date or entry.get("salesDate", "")
    material.isbn = material.isbn or _normalize_isbn(entry.get("isbn", ""))
    material.cover_url = material.cover_url or (
        entry.get("largeImageUrl") or entry.get("mediumImageUrl") or ""
    )
    caption = (entry.get("itemCaption") or "").strip()
    if len(caption) > len(material.description):
        material.description = caption


# ---------------------------------------------------------------- Google Books


def search_google_books(title: str, isbn: str = "") -> dict[str, Any]:
    query = f"isbn:{isbn}" if isbn else f"intitle:{title}"
    params: dict[str, Any] = {"q": query, "maxResults": 5, "langRestrict": "ja"}
    api_key = os.getenv("GOOGLE_BOOKS_API_KEY", "")
    if api_key:
        params["key"] = api_key

    payload = _request(GOOGLE_BOOKS_ENDPOINT, params).json()
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


# ---------------------------------------------------------------------- openBD


def fetch_openbd(isbn: str) -> dict[str, Any]:
    payload = _request(OPENBD_ENDPOINT, {"isbn": isbn}).json()
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


# ------------------------------------------------------------------ 統合


def _try(source: str, fetch):
    """1ソースの失敗が全体を止めないようにする。"""
    try:
        return fetch()
    except Exception as error:  # noqa: BLE001 - 残りのソースで続行したい
        print(f"[warn] {source} を取得できませんでした: {error}")
        return None


def fetch_material(
    title: str, isbn: str = "", notes: str = "", *, strict: bool = True
) -> BookMaterial:
    """1冊分の根拠データを取得してマージする。

    strict=False にすると根拠不足でも例外を投げず、そのまま返す。
    Web検索で補完してから判定したい場合に使う。
    """
    material = BookMaterial(
        title=title, isbn=_normalize_isbn(isbn), personal_notes=notes.strip()
    )

    if not material.isbn:
        ndl = _try("NDLサーチ", lambda: search_ndl(title))
        if ndl:
            material.sources.append("ndl")
            material.isbn = ndl["isbn"]
            material.official_title = ndl["title"]
            material.authors = ndl["authors"]
            material.publisher = ndl["publisher"]
            material.published_date = ndl["published_date"]
        _polite_sleep()

    rakuten = _try("楽天ブックス", lambda: search_rakuten(title, material.isbn))
    if rakuten:
        _apply_rakuten(material, rakuten)
        _polite_sleep()

    volume = _try(
        "Google Books", lambda: search_google_books(title, material.isbn)
    )
    if volume:
        material.sources.append("google_books")
        google_title = volume.get("title") or ""
        if volume.get("subtitle"):
            google_title = f"{google_title} {volume['subtitle']}"
        material.official_title = google_title or material.official_title
        material.authors = volume.get("authors") or material.authors
        material.publisher = volume.get("publisher") or material.publisher
        material.published_date = volume.get("publishedDate") or material.published_date
        material.page_count = volume.get("pageCount") or material.page_count
        google_description = (volume.get("description") or "").strip()
        if len(google_description) > len(material.description):
            material.description = google_description
        material.categories = volume.get("categories") or []
        if not material.isbn:
            for ident in volume.get("industryIdentifiers", []) or []:
                if ident.get("type") == "ISBN_13":
                    material.isbn = ident.get("identifier", "")
                    break

    if material.isbn:
        _polite_sleep()
        record = _try("openBD", lambda: fetch_openbd(material.isbn))
        if record:
            material.sources.append("openbd")
            summary = record.get("summary", {}) or {}
            material.official_title = summary.get("title") or material.official_title
            if summary.get("author") and not material.authors:
                material.authors = [
                    _normalize_person(name)
                    for name in summary["author"].split("/")
                    if name.strip()
                ]
            material.cover_url = material.cover_url or summary.get("cover", "")
            material.publisher = material.publisher or summary.get("publisher", "")
            material.published_date = material.published_date or summary.get("pubdate", "")

            texts = _extract_openbd_texts(record)
            # openBD の日本語内容紹介は Google Books より詳しいことが多いので優先。
            if len(texts.get("description", "")) > len(material.description):
                material.description = texts["description"]
            material.table_of_contents = texts.get("table_of_contents", "")
            material.author_bio = texts.get("author_bio", "")

    if strict and not material.has_substance():
        raise BookNotFoundError(
            f"『{title}』の根拠データが不足しています"
            f"（取得できたソース: {', '.join(material.sources) or 'なし'} / "
            f"根拠文字数: {material.substance_chars()}、必要: {MIN_SUBSTANCE_CHARS}）。"
            " books/queue.yaml の notes に、この本について自分の言葉で"
            f"{MIN_SUBSTANCE_CHARS}文字以上のメモを書いてください。"
            " それが最も確実な解決策です。ISBN の追記も有効です。"
        )

    return material
