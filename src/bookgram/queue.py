"""本のキュー管理と、日付への割り当て。

キューの実体は books/queue.yaml。
「どの日に何を投稿するか」の状態は drafts/YYYY-MM-DD/post.json の存在そのもので表す。
別途ステートファイルを持たないので、下書きを消せばその日は自動的に再割り当て対象になる。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import DRAFTS_DIR, QUEUE_FILE

STATUS_PENDING = "pending"
STATUS_GENERATED = "generated"


def load_queue() -> dict[str, Any]:
    if not QUEUE_FILE.exists():
        return {"books": []}
    data = yaml.safe_load(QUEUE_FILE.read_text(encoding="utf-8")) or {}
    data.setdefault("books", [])
    return data


def save_queue(data: dict[str, Any]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def pending_books(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in data["books"] if b.get("status", STATUS_PENDING) == STATUS_PENDING]


def take_next_book(data: dict[str, Any]) -> dict[str, Any] | None:
    """未生成の本を1冊取り出す（キューからは消さず status を更新する）。"""
    pending = pending_books(data)
    return pending[0] if pending else None


def mark_generated(data: dict[str, Any], book: dict[str, Any]) -> None:
    for entry in data["books"]:
        if entry is book or (
            entry.get("title") == book.get("title")
            and entry.get("isbn", "") == book.get("isbn", "")
        ):
            entry["status"] = STATUS_GENERATED
            return


def draft_path(day: date) -> Path:
    return DRAFTS_DIR / day.isoformat() / "post.json"


def load_draft(day: date) -> dict[str, Any] | None:
    path = draft_path(day)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_draft(day: date, payload: dict[str, Any]) -> Path:
    path = draft_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def free_dates(
    start: date, count: int, *, skip_weekdays: Iterable[int] | None = None
) -> list[date]:
    """start 以降で下書きがまだ無い日付を count 件返す。

    skip_weekdays に挙げた曜日は割り当て対象から外す（特集の枠を空けるため）。
    """
    skipped = set(skip_weekdays or ())
    found: list[date] = []
    cursor = start
    # 割り当て済みの日や特集日が続いても止まらないよう、1年先まで探す。
    for _ in range(400):
        if len(found) >= count:
            break
        if cursor.weekday() in skipped:
            cursor += timedelta(days=1)
            continue
        if not draft_path(cursor).exists():
            found.append(cursor)
        cursor += timedelta(days=1)
    return found


def open_slots(
    start: date, days: int, *, skip_weekdays: Iterable[int] | None = None
) -> list[date]:
    """start から days 日間のうち、下書きがまだ無い日付を返す。

    skip_weekdays の曜日は特集の枠なので対象から外す。
    連続性は見ないので、途中に埋まっている日があっても先の日付まで拾える。
    """
    skipped = set(skip_weekdays or ())
    slots: list[date] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        if day.weekday() in skipped:
            continue
        if not draft_path(day).exists():
            slots.append(day)
    return slots


def coverage_days(start: date, horizon: int = 21) -> int:
    """start から連続して下書きが存在する日数（在庫日数）。"""
    covered = 0
    cursor = start
    while covered < horizon and draft_path(cursor).exists():
        covered += 1
        cursor += timedelta(days=1)
    return covered
