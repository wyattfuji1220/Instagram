"""楽天の売れ筋を毎週記録して、順位の動きを取り出す。

売上部数（実数）は取れない。週次で部数を出しているのはオリコンと日販・
トーハンだが、いずれも API が無く、数字は各社の著作物なので使えない。

楽天ウェブサービスからは「売れている順」が取れる。ただし**集計期間が
公開されていない**。実際、ビジネス書の文庫を引くと 2001年・2011年の本が
2026年の本と同じ並びに出る。そのまま「今週のランキング」とは名乗れない。

そこで、期間はこちらで作る。毎週この並びを丸ごと記録しておけば、
「先週7位→今週2位」「3週連続1位」「今週初登場」が自分の手元のデータから
言える。借りた数字ではなく自分で測った動きなので、根拠が完全に揃う。

記録だけを先に始める。比較できるのは2週目からで、意味のある動きが見える
のは3週目あたり。どの区分をどう発信するかは、貯まってから決める。

出力は rankings/YYYY-Www.json。1週あたり 9区分 × 20件で 30KB 程度。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .bookdata import (
    DEFAULT_RAKUTEN_REFERER,
    RAKUTEN_ENDPOINT,
    _polite_sleep,
    _request,
)
from .config import JST, RANKINGS_DIR

# 楽天Kobo（電子書籍）。紙とは別のエンドポイントとジャンル体系を持つ。
EBOOK_ENDPOINT = (
    "https://openapi.rakuten.co.jp/services/api/Kobo/EbookSearch/20170426"
)

# 1区分あたり何件残すか。上位20件あれば「圏内に入った／落ちた」が語れる。
# 30件まで1リクエストで取れるが、下位は入れ替わりが激しく雑音になる。
TOP_N = 20
HITS = 30


class RankingUnavailableError(RuntimeError):
    """楽天の資格情報が無い、または取得できなかった。"""


@dataclass(frozen=True)
class Segment:
    """記録する区分ひとつ。"""

    key: str
    label: str
    medium: str  # "paper" / "ebook"
    genre_id: str
    size: int | None = None  # 紙の判型。電子には無い


# 紙の判型。楽天の size パラメータ。
PAPER_SIZES: tuple[tuple[int, str], ...] = ((1, "単行本"), (2, "文庫"), (3, "新書"))
PAPER_GENRES: tuple[tuple[str, str, str], ...] = (
    ("business", "ビジネス書", "001006"),
    ("novel", "小説・エッセイ", "001004"),
)
# 電子は楽天Kobo のジャンル体系。101 が電子書籍の根。
EBOOK_GENRES: tuple[tuple[str, str, str], ...] = (
    ("business", "ビジネス書", "101905"),
    ("novel", "小説・エッセイ", "101901"),
    ("mystery", "ミステリー・サスペンス", "101902"),
)


def _segments() -> tuple[Segment, ...]:
    out: list[Segment] = []
    for genre_key, genre_label, genre_id in PAPER_GENRES:
        for size, size_label in PAPER_SIZES:
            out.append(
                Segment(
                    key=f"paper-{genre_key}-{size}",
                    label=f"{genre_label}／{size_label}",
                    medium="paper",
                    genre_id=genre_id,
                    size=size,
                )
            )
    for genre_key, genre_label, genre_id in EBOOK_GENRES:
        out.append(
            Segment(
                key=f"ebook-{genre_key}",
                label=f"{genre_label}／電子書籍",
                medium="ebook",
                genre_id=genre_id,
            )
        )
    return tuple(out)


SEGMENTS = _segments()


@dataclass
class Entry:
    """その週のその区分に並んでいた1冊。"""

    rank: int
    title: str
    author: str
    publisher: str
    item_id: str
    price: int
    sales_date: str
    review_count: int
    review_average: float
    cover_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Entry":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def identity(self) -> str:
        """週をまたいで同じ本だと判定するための鍵。

        ISBN（電子は itemNumber）を使う。付いていない本があるので、
        無ければ書名を正規化したもので代用する。
        """
        return self.item_id or _match_key(self.title)


def _match_key(title: str) -> str:
    """書名の表記ゆれを吸収する。全角半角・空白・記号を落とす。"""
    folded = unicodedata.normalize("NFKC", title).lower()
    return re.sub(r"[\s　！!？?・,，、。.：:；;「」『』（）()\[\]【】〔〕~〜ー-]+", "", folded)


# ------------------------------------------------------------------ 取得


def _auth() -> dict[str, str]:
    app_id = os.getenv("RAKUTEN_APP_ID", "").strip()
    access_key = os.getenv("RAKUTEN_ACCESS_KEY", "").strip()
    if not app_id or not access_key:
        raise RankingUnavailableError(
            "売れ筋の記録には RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が要ります。"
        )
    return {"applicationId": app_id, "accessKey": access_key}


def _headers() -> dict[str, str]:
    """楽天は Referer と Origin の両方を見る。片方だけでは 403 になる。"""
    site = os.getenv("RAKUTEN_REFERER", DEFAULT_RAKUTEN_REFERER)
    return {"Referer": site, "Origin": site}


def _to_entry(rank: int, item: dict[str, Any]) -> Entry:
    """紙と電子でキー名が違うのはここだけに閉じ込める。"""
    return Entry(
        rank=rank,
        title=(item.get("title") or "").strip(),
        author=(item.get("author") or "").replace("/", "、").strip(),
        publisher=(item.get("publisherName") or "").strip(),
        # 紙は isbn、電子は itemNumber。どちらも無いことがある。
        item_id=str(item.get("isbn") or item.get("itemNumber") or "").strip(),
        price=int(item.get("itemPrice") or 0),
        sales_date=(item.get("salesDate") or "").strip(),
        review_count=int(item.get("reviewCount") or 0),
        review_average=float(item.get("reviewAverage") or 0),
        cover_url=item.get("largeImageUrl") or item.get("mediumImageUrl") or "",
    )


def fetch_segment(segment: Segment) -> list[Entry]:
    """1区分の売れ筋を上位から取る。"""
    auth, headers = _auth(), _headers()
    params: dict[str, Any] = {
        **auth,
        "sort": "sales",
        "hits": HITS,
        "format": "json",
        "formatVersion": 2,
    }
    if segment.medium == "paper":
        endpoint = RAKUTEN_ENDPOINT
        params["booksGenreId"] = segment.genre_id
        params["size"] = segment.size
    else:
        endpoint = EBOOK_ENDPOINT
        params["koboGenreId"] = segment.genre_id

    items = _request(endpoint, params, headers).json().get("Items") or []
    return [_to_entry(i, item) for i, item in enumerate(items[:TOP_N], start=1)]


def week_key(day: date) -> str:
    """その日が属する週の呼び名。ISO週で数える。"""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def take_snapshot(day: date | None = None) -> dict[str, Any]:
    """全区分をまとめて1回ぶん記録する。"""
    day = day or datetime.now(JST).date()
    segments: dict[str, Any] = {}
    for index, segment in enumerate(SEGMENTS):
        if index:
            # 楽天への連続アクセスを避ける。9区分でも30秒はかからない。
            _polite_sleep()
        entries = fetch_segment(segment)
        segments[segment.key] = {
            "label": segment.label,
            "medium": segment.medium,
            "entries": [entry.to_dict() for entry in entries],
        }
        print(f"[ranking] {segment.label}: {len(entries)}件")
    return {
        "week": week_key(day),
        "taken_on": day.isoformat(),
        "taken_at": datetime.now(JST).isoformat(),
        "source": "楽天ブックス／楽天Kobo の売れている順",
        "segments": segments,
    }


# ------------------------------------------------------------------ 保存


def snapshot_path(week: str) -> Path:
    return RANKINGS_DIR / f"{week}.json"


def save_snapshot(snapshot: dict[str, Any]) -> Path:
    RANKINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_path(snapshot["week"])
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def stored_weeks() -> list[str]:
    """記録済みの週を古い順に返す。"""
    if not RANKINGS_DIR.exists():
        return []
    return sorted(p.stem for p in RANKINGS_DIR.glob("*.json"))


def load_snapshot(week: str) -> dict[str, Any] | None:
    path = snapshot_path(week)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def entries_of(snapshot: dict[str, Any] | None, segment_key: str) -> list[Entry]:
    if not snapshot:
        return []
    block = (snapshot.get("segments") or {}).get(segment_key) or {}
    return [Entry.from_dict(raw) for raw in block.get("entries") or []]


# ------------------------------------------------------------------ 動き


@dataclass
class Move:
    """先週からの動き。"""

    entry: Entry
    previous_rank: int | None
    weeks_in: int

    @property
    def is_new(self) -> bool:
        return self.previous_rank is None

    @property
    def delta(self) -> int:
        """上がった順位数。下がったときは負。初登場は0。"""
        if self.previous_rank is None:
            return 0
        return self.previous_rank - self.entry.rank

    @property
    def label(self) -> str:
        if self.is_new:
            return "初登場"
        if self.delta > 0:
            return f"↑{self.delta}"
        if self.delta < 0:
            return f"↓{-self.delta}"
        return "→"


def _rank_map(entries: Iterable[Entry]) -> dict[str, int]:
    return {entry.identity: entry.rank for entry in entries}


def weeks_in_top(identity: str, segment_key: str, weeks: list[str]) -> int:
    """その本が何週続けて圏内にいるか。新しい週から遡って数える。"""
    count = 0
    for week in reversed(weeks):
        ranks = _rank_map(entries_of(load_snapshot(week), segment_key))
        if identity not in ranks:
            break
        count += 1
    return count


def movements(week: str, segment_key: str) -> list[Move]:
    """その週・その区分の並びを、先週と比べた形で返す。"""
    weeks = stored_weeks()
    if week not in weeks:
        return []
    at = weeks.index(week)
    previous = _rank_map(entries_of(load_snapshot(weeks[at - 1]), segment_key)) if at else {}
    history = weeks[: at + 1]
    return [
        Move(
            entry=entry,
            previous_rank=previous.get(entry.identity),
            weeks_in=weeks_in_top(entry.identity, segment_key, history),
        )
        for entry in entries_of(load_snapshot(week), segment_key)
    ]


def build_report(week: str) -> str:
    """人が読む形にまとめる。Actions のログは後から読めないので残す。"""
    snapshot = load_snapshot(week)
    if snapshot is None:
        return f"# 売れ筋の記録\n\n{week} の記録がありません。\n"

    weeks = stored_weeks()
    first = len(weeks) < 2 or weeks.index(week) == 0

    out = [f"# 売れ筋の記録 {week}", ""]
    out.append(f"取得日: {snapshot.get('taken_on', '')}")
    out.append(f"出典: {snapshot.get('source', '')}（Supported by 楽天ウェブサービス）")
    out.append("")
    if first:
        out.append(
            "※ 最初の週なので比較対象がありません。動きが出るのは次回からです。"
        )
        out.append("")
    out.append(
        "※ 楽天の「売れている順」は集計期間が公開されていません。"
        "ここでいう順位の動きは、毎週この並びを記録して比べた当方の集計です。"
    )
    out.append("")

    for segment in SEGMENTS:
        moves = movements(week, segment.key)
        if not moves:
            continue
        out.append(f"## {segment.label}")
        out.append("")
        out.append("| 順位 | 動き | 連続 | 書名 | 著者 | 価格 | 評価 |")
        out.append("|---|---|---|---|---|---|---|")
        for move in moves:
            entry = move.entry
            stars = f"★{entry.review_average:.1f}（{entry.review_count}件）" if entry.review_count else "-"
            out.append(
                f"| {entry.rank} | {move.label} | {move.weeks_in}週 "
                f"| {entry.title} | {entry.author} "
                f"| {entry.price:,}円 | {stars} |"
            )
        out.append("")
    return "\n".join(out) + "\n"


def highlights(week: str, limit: int = 12) -> list[tuple[Segment, Move]]:
    """発信の種になりそうな動きだけを拾う。

    大きく上がった本、初登場で上位に入った本、長く居座っている本。
    どの切り口で出すかは後で決めるので、ここでは候補を並べるだけにする。
    """
    weeks = stored_weeks()
    if week not in weeks or weeks.index(week) == 0:
        # 最初の週は全部が「初登場」になる。比較対象が無いだけなので、
        # 動きとして数えない。
        return []

    found: list[tuple[Segment, Move]] = []
    for segment in SEGMENTS:
        for move in movements(week, segment.key):
            notable = (
                move.delta >= 3
                or (move.is_new and move.entry.rank <= 5)
                or move.weeks_in >= 4
            )
            if notable:
                found.append((segment, move))
    found.sort(
        key=lambda pair: (pair[1].weeks_in, pair[1].delta, -pair[1].entry.rank),
        reverse=True,
    )
    return found[:limit]
