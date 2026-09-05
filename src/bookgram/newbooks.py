"""楽天ブックスから、特集に載せる本を集める。

3種類の特集を同じ仕組みで賄う。

  business  ビジネス書の新刊   発売日の降順から期間内のものを拾う
  novel     小説の新刊         売れ筋順から期間内のものを拾う
            （発売日順は同人的なシリーズ物で埋まり、選びようがないため）
  classic   殿堂入り書評       レビュー件数順から、評価の高い定番を拾う

新刊一覧の先頭には発売日未定のプレースホルダ（2225年など）が並ぶため、
目的の日付帯に届くまでページを送る。日付が窓を通り過ぎたら打ち切る。
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
# 小説は「その他」(001004015) にウェブ小説が大量に入るため、
# 文芸として読まれる4ジャンルだけを名指しで拾う。
NOVEL_GENRE_IDS = (
    "001004008",  # 日本の小説
    "001004009",  # 外国の小説
    "001004001",  # ミステリー・サスペンス
    "001004002",  # SF・ホラー
)
NOVEL_PAGES = 2
NOVEL_DAYS_BACK = 30
NOVEL_DAYS_AHEAD = 30
NOVEL_MIN_CAPTION_CHARS = 120
# 殿堂入りの足切り。この線を超える本はどれも「読み継がれている」と言える。
CLASSIC_PAGES = 6
CLASSIC_MIN_REVIEWS = 200
CLASSIC_MIN_AVERAGE = 4.0
CLASSIC_MIN_CAPTION_CHARS = 100

# 「安いのに評価が高い」特集。価格の意外性で見てもらい、買う直前の判断材料に
# なるので保存されやすい。値だけが根拠なので、条件は緩めない。
BARGAIN_PAGES = 20
BARGAIN_MAX_PRICE = 1000
BARGAIN_MIN_AVERAGE = 4.2
# 100件だと候補が7冊しか集まらず、4冊の特集を2本作れない。60件まで緩める。
# それでも「読まれたうえで高評価」と言える水準。
BARGAIN_MIN_REVIEWS = 60
BARGAIN_MIN_CAPTION_CHARS = 100
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
    review_count: int = 0
    review_average: float = 0.0
    price: int = 0
    tags: list[str] = field(default_factory=list)

    @property
    def price_label(self) -> str:
        """「880円」。取れなければ空。"""
        return f"{self.price:,}円" if self.price else ""

    @property
    def review_label(self) -> str:
        """「レビュー1,234件 ★4.3」。件数が無ければ空。"""
        if not self.review_count:
            return ""
        return f"レビュー{self.review_count:,}件　★{self.review_average:.1f}"

    def to_prompt_block(self) -> str:
        lines = [
            f"書名: {self.title}",
            f"著者: {self.author or '不明'}",
            f"出版社: {self.publisher or '不明'}",
            f"発売日: {self.sales_date_label}",
        ]
        if self.price_label:
            lines.append(f"価格: {self.price_label}")
        if self.review_label:
            lines.append(f"読者の評価: {self.review_label}")
        lines += ["内容紹介:", self.caption]
        return chr(10).join(lines)


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
            "特集には楽天の RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が必要です。"
        )
    return {"applicationId": app_id, "accessKey": access_key}


def _headers() -> dict[str, str]:
    """楽天は Referer と Origin の両方を見る。片方だけでは 403 になる。"""
    site = os.getenv("RAKUTEN_REFERER", DEFAULT_RAKUTEN_REFERER)
    return {"Referer": site, "Origin": site}


def _search(auth: dict[str, str], headers: dict[str, str], **params: Any) -> list[dict]:
    payload = _request(
        RAKUTEN_ENDPOINT,
        {
            **auth,
            "hits": HITS_PER_PAGE,
            "format": "json",
            "formatVersion": 2,
            **params,
        },
        headers,
    ).json()
    return payload.get("Items") or []


def _to_book(item: dict[str, Any], sales_date: date) -> NewBook:
    return NewBook(
        title=(item.get("title") or "").strip(),
        author=(item.get("author") or "").replace("/", "、").strip(),
        publisher=item.get("publisherName", ""),
        sales_date=sales_date,
        sales_date_label=format_sales_date(sales_date),
        isbn=item.get("isbn") or item.get("title", ""),
        cover_url=item.get("largeImageUrl") or item.get("mediumImageUrl") or "",
        caption=(item.get("itemCaption") or "").strip(),
        review_count=int(item.get("reviewCount") or 0),
        review_average=float(item.get("reviewAverage") or 0),
        price=int(item.get("itemPrice") or 0),
    )


# 楽天が発行する2つの値の想定文字数。取り違えの判定にだけ使う。
APP_ID_LENGTH = 36
ACCESS_KEY_LENGTH = 46


def _probe(app_id: str, access_key: str) -> tuple[bool, str]:
    """最小のクエリを1回だけ投げて、認証が通るかを見る。

    戻り値のメッセージには資格情報を一切含めない。楽天のエラー本文は
    値を反射しないため、そのまま載せても漏れない。
    """
    import requests

    try:
        response = requests.get(
            RAKUTEN_ENDPOINT,
            params={
                "applicationId": app_id,
                "accessKey": access_key,
                "booksGenreId": BUSINESS_GENRE_ID,
                "hits": 1,
                "format": "json",
                "formatVersion": 2,
            },
            headers=_headers(),
            timeout=20,
        )
    except requests.RequestException as error:
        return False, f"接続できません（{type(error).__name__}）"

    if response.status_code == 200:
        return True, "OK"
    try:
        message = response.json().get("errors", {}).get("errorMessage", "")
    except ValueError:
        message = ""
    return False, f"HTTP {response.status_code} {message}".strip()


def diagnose_rakuten() -> list[tuple[str, str]]:
    """楽天の資格情報を点検する。値そのものは決して出力しない。"""
    app_id = os.getenv("RAKUTEN_APP_ID", "").strip()
    access_key = os.getenv("RAKUTEN_ACCESS_KEY", "").strip()

    if not app_id or not access_key:
        missing = [
            name
            for name, value in (
                ("RAKUTEN_APP_ID", app_id),
                ("RAKUTEN_ACCESS_KEY", access_key),
            )
            if not value
        ]
        return [("NG", "未設定です: " + "、".join(missing))]

    results: list[tuple[str, str]] = []
    if len(app_id) != APP_ID_LENGTH:
        results.append(
            ("--", f"RAKUTEN_APP_ID の長さが想定と違います（{len(app_id)}文字 / 想定{APP_ID_LENGTH}文字）")
        )
    if len(access_key) != ACCESS_KEY_LENGTH:
        results.append(
            ("--", f"RAKUTEN_ACCESS_KEY の長さが想定と違います（{len(access_key)}文字 / 想定{ACCESS_KEY_LENGTH}文字）")
        )

    ok, detail = _probe(app_id, access_key)
    if ok:
        results.append(("ok", "楽天ブックスAPIに接続できました。"))
        return results

    # 取り違えは一度やると気づきにくいので、入れ替えて試して切り分ける。
    swapped_ok, _ = _probe(access_key, app_id)
    if swapped_ok:
        results.append(
            ("NG", "RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が入れ替わっています。")
        )
    else:
        results.append(("NG", f"楽天ブックスAPIに接続できません: {detail}"))
    return results


def fetch_new_business_books(
    today: date | None = None,
    *,
    days_back: int = DAYS_BACK,
    days_ahead: int = DAYS_AHEAD,
    limit: int = 12,
) -> list[NewBook]:
    """指定期間に発売される（された）ビジネス書を集める。"""
    auth, headers = _auth(), _headers()
    today = today or date.today()
    low, high = today - timedelta(days=days_back), today + timedelta(days=days_ahead)

    found: dict[str, NewBook] = {}
    for page in range(1, MAX_PAGES + 1):
        items = _search(
            auth,
            headers,
            booksGenreId=BUSINESS_GENRE_ID,
            sort="-releaseDate",
            page=page,
        )
        if not items:
            break

        dates = [parse_sales_date(item.get("salesDate", "")) for item in items]
        for item, sales_date in zip(items, dates):
            if sales_date is None or not (low <= sales_date <= high):
                continue
            book = _to_book(item, sales_date)
            if len(book.caption) < MIN_CAPTION_CHARS or not book.cover_url:
                continue
            found.setdefault(book.isbn, book)

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


# 巻数つきの続刊は、途中から読めないので特集には向かない。
VOLUME_PATTERN = re.compile(
    r"（\s*\d+\s*）|\(\s*\d+\s*\)|第\d+巻"
    # 「これは経費で落ちません！ 14 〜経理部の森若さん〜」のような巻数表記。
    # 前後を区切られた1〜2桁だけを見て、「52ヘルツのクジラたち」は残す。
    r"|[\s　]\d{1,2}([\s　〜~]|$)"
    r"|（上|（下|（前編|（後編|上巻|下巻"
    # 「ブラッディダイスの殺人 上」のように括弧なしで分冊されたもの
    r"|[\s　](上|下|前編|後編)$"
    r"|設定資料集|写真集|特装版"
)
# 「るるぶ■■版」「◯◯（仮）」など、書名が確定していない本は載せない。
PROVISIONAL_PATTERN = re.compile(r"■|●●|（仮）|\(仮\)|未定")


def is_standalone_title(title: str) -> bool:
    """1冊で読み切れそうな書名か。続刊・資料集・仮題を弾く。"""
    title = title or ""
    return not (VOLUME_PATTERN.search(title) or PROVISIONAL_PATTERN.search(title))


def fetch_new_novels(
    today: date | None = None,
    *,
    days_back: int = NOVEL_DAYS_BACK,
    days_ahead: int = NOVEL_DAYS_AHEAD,
    limit: int = 12,
) -> list[NewBook]:
    """最近発売された小説を、売れ筋順に集める。

    発売日順で引くとシリーズ物のウェブ小説で埋まってしまい選べないため、
    売れ筋順に並べたうえで発売日の窓を当てる。
    """
    auth, headers = _auth(), _headers()
    today = today or date.today()
    low, high = today - timedelta(days=days_back), today + timedelta(days=days_ahead)

    found: dict[str, NewBook] = {}
    for genre_id in NOVEL_GENRE_IDS:
        for page in range(1, NOVEL_PAGES + 1):
            items = _search(
                auth, headers, booksGenreId=genre_id, sort="sales", page=page
            )
            if not items:
                break
            for item in items:
                sales_date = parse_sales_date(item.get("salesDate", ""))
                if sales_date is None or not (low <= sales_date <= high):
                    continue
                book = _to_book(item, sales_date)
                if len(book.caption) < NOVEL_MIN_CAPTION_CHARS or not book.cover_url:
                    continue
                if not is_standalone_title(book.title):
                    continue
                found.setdefault(book.isbn, book)
            _polite_sleep()

    books = sorted(found.values(), key=lambda b: b.sales_date, reverse=True)
    if not books:
        raise NewBooksUnavailableError(
            f"{low} 〜 {high} に該当する小説の新刊が見つかりませんでした。"
        )
    return books[:limit]


def fetch_classics(
    *,
    exclude_isbns: set[str] | None = None,
    exclude_titles: set[str] | None = None,
    limit: int = 16,
) -> list[NewBook]:
    """レビュー件数の多い定番ビジネス書を集める。

    「殿堂入り」の根拠は楽天の実データ（件数と平均点）そのもの。
    自分が既に紹介した本は除外して、内容が重ならないようにする。
    """
    auth, headers = _auth(), _headers()
    exclude_isbns = exclude_isbns or set()
    normalized = {_normalize_title(t) for t in (exclude_titles or set())}

    found: dict[str, NewBook] = {}
    for page in range(1, CLASSIC_PAGES + 1):
        items = _search(
            auth, headers, booksGenreId=BUSINESS_GENRE_ID, sort="reviewCount", page=page
        )
        if not items:
            break
        for item in items:
            sales_date = parse_sales_date(item.get("salesDate", "")) or date(1970, 1, 1)
            book = _to_book(item, sales_date)
            if book.review_count < CLASSIC_MIN_REVIEWS:
                continue
            if book.review_average < CLASSIC_MIN_AVERAGE:
                continue
            if len(book.caption) < CLASSIC_MIN_CAPTION_CHARS or not book.cover_url:
                continue
            if book.isbn in exclude_isbns:
                continue
            if _normalize_title(book.title) in normalized:
                continue
            found.setdefault(book.isbn, book)
        _polite_sleep()

    books = sorted(found.values(), key=lambda b: b.review_count, reverse=True)
    if not books:
        raise NewBooksUnavailableError(
            "殿堂入りの候補が見つかりませんでした。除外条件が厳しすぎる可能性があります。"
        )
    return books[:limit]


def fetch_bargains(
    *,
    exclude_isbns: set[str] | None = None,
    exclude_titles: set[str] | None = None,
    limit: int = 16,
) -> list[NewBook]:
    """1,000円以下で評価の高いビジネス書を集める。

    「安い＝内容が薄い」とは限らない、という切り口。根拠は楽天の価格・
    平均点・レビュー件数で、いずれも実データそのもの。
    """
    auth, headers = _auth(), _headers()
    exclude_isbns = exclude_isbns or set()
    normalized = {_normalize_title(t) for t in (exclude_titles or set())}

    found: dict[str, NewBook] = {}
    # レビュー数の多い順だけだと母集団が偏る。安い順からも拾って幅を出す。
    for order in ("reviewCount", "+itemPrice"):
      for page in range(1, BARGAIN_PAGES + 1):
        items = _search(
            auth, headers, booksGenreId=BUSINESS_GENRE_ID, sort=order, page=page
        )
        if not items:
            break
        for item in items:
            sales_date = parse_sales_date(item.get("salesDate", "")) or date(1970, 1, 1)
            book = _to_book(item, sales_date)
            if not book.price or book.price > BARGAIN_MAX_PRICE:
                continue
            if book.review_average < BARGAIN_MIN_AVERAGE:
                continue
            if book.review_count < BARGAIN_MIN_REVIEWS:
                continue
            if len(book.caption) < BARGAIN_MIN_CAPTION_CHARS or not book.cover_url:
                continue
            if book.isbn in exclude_isbns:
                continue
            if _normalize_title(book.title) in normalized:
                continue
            found.setdefault(book.isbn, book)
        _polite_sleep()

    # 安い順ではなく評価順。「安いから薄い」ではないことを示す並び。
    books = sorted(
        found.values(), key=lambda b: (b.review_average, b.review_count), reverse=True
    )
    if not books:
        raise NewBooksUnavailableError(
            f"{BARGAIN_MAX_PRICE}円以下・評価{BARGAIN_MIN_AVERAGE}以上の候補が"
            "見つかりませんでした。除外条件が厳しすぎる可能性があります。"
        )
    return books[:limit]


def _normalize_title(title: str) -> str:
    """表記ゆれを吸収して書名を突き合わせる。"""
    return re.sub(r"[\s　・:：\-−―ー『』「」【】（）()]", "", title or "").lower()


def period_parts(today: date) -> dict[str, str]:
    """表紙で数字だけ色を変えるため、期間ラベルを分解して返す。"""
    half = "前半" if today.day <= 15 else "後半"
    return {
        "year": f"{today.year % 100}年",
        "month": str(today.month),
        "half": f"月{half}",
    }


def period_label(today: date) -> str:
    """「26年8月後半」のような期間ラベルを作る。"""
    parts = period_parts(today)
    return f"{parts['year']}{parts['month']}{parts['half']}"


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
