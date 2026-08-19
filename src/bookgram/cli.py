"""コマンドライン入口。

  python -m bookgram generate    週次: 在庫が7日分に満たなければ本を消化して下書きを作る
  python -m bookgram post        毎日: その日の下書きを Instagram に投稿する
  python -m bookgram preview     プレビューページだけ作り直す
  python -m bookgram doctor      認証・トークン・キューの状態を点検する
  python -m bookgram whoami      アクセストークンから IG_USER_ID を調べる
  python -m bookgram cleanup     古い画像を削除する
  python -m bookgram refresh-token  長期アクセストークンを延長する
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from typing import Any

from . import queue as bookqueue
from .bookdata import BookNotFoundError, fetch_material
from .config import (
    DAYS_PER_BOOK,
    IMAGE_RETENTION_DAYS,
    IMG_DIR,
    JST,
    POSTED_LOG,
    QUEUE_LOW_THRESHOLD,
    load_secrets,
)
from .generate import generate_book_posts
from .preview import load_week_drafts, render_index, render_week_preview
from .publish import InstagramClient, PublishError, build_caption, publish_carousel
from .render import render_day

TARGET_COVERAGE_DAYS = 7


def today_jst() -> date:
    return datetime.now(JST).date()


def week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


# --------------------------------------------------------------------------- generate


def cmd_generate(args: argparse.Namespace) -> int:
    secrets = load_secrets(require=("ANTHROPIC_API_KEY",))
    start = date.fromisoformat(args.start) if args.start else today_jst() + timedelta(days=1)

    data = bookqueue.load_queue()
    generated_books = 0

    while bookqueue.coverage_days(start) < TARGET_COVERAGE_DAYS:
        book = bookqueue.take_next_book(data)
        if book is None:
            print("[warn] キューに未生成の本がありません。books/queue.yaml に本を追加してください。")
            break

        title = book["title"]
        print(f"[generate] 『{title}』の書誌データを取得中…")
        try:
            material = fetch_material(title, book.get("isbn", ""))
        except BookNotFoundError as error:
            print(f"[error] {error}", file=sys.stderr)
            book["status"] = "needs_input"
            bookqueue.save_queue(data)
            return 1

        print(f"[generate] 原稿を生成中（sources={material.sources}）…")
        payload = generate_book_posts(
            material, secrets.anthropic_api_key, book.get("notes", "")
        )

        targets = bookqueue.free_dates(start, DAYS_PER_BOOK)
        days = sorted(payload["days"], key=lambda d: d["day_index"])

        for day_date, day in zip(targets, days):
            print(f"[render]   {day_date} Day{day['day_index']} 【{day['theme']}】")
            render_day(
                day,
                payload["book_title"],
                payload["book_author"],
                payload["one_line"],
                IMG_DIR / day_date.isoformat(),
            )
            bookqueue.save_draft(
                day_date,
                {
                    "date": day_date.isoformat(),
                    "book_title": payload["book_title"],
                    "book_author": payload["book_author"],
                    "one_line": payload["one_line"],
                    "day_index": day["day_index"],
                    "theme": day["theme"],
                    "cards": day["cards"],
                    "caption": day["caption"],
                    "hashtags": day["hashtags"],
                    "grounding": day["grounding"],
                    "status": "draft",
                    "meta": payload.get("_meta", {}),
                },
            )

        bookqueue.mark_generated(data, book)
        bookqueue.save_queue(data)
        generated_books += 1

    _write_previews(start, secrets.pages_base_url)

    remaining = len(bookqueue.pending_books(data))
    print(f"[done] 生成した本: {generated_books}冊 / キュー残: {remaining}冊")
    if remaining < QUEUE_LOW_THRESHOLD:
        print(f"::warning::キューの残りが{remaining}冊です。books/queue.yaml に本を追加してください。")
    return 0


def _write_previews(start: date, pages_base_url: str) -> None:
    days = [start + timedelta(days=i) for i in range(14)]
    drafts = load_week_drafts(days)
    if drafts:
        path = render_week_preview(week_label(start), drafts)
        print(f"[preview] {path}")
    print(f"[preview] {render_index(pages_base_url)}")


# ------------------------------------------------------------------------------- post


def cmd_post(args: argparse.Namespace) -> int:
    required = () if args.dry_run else ("IG_USER_ID", "IG_ACCESS_TOKEN")
    secrets = load_secrets(require=required)

    day = date.fromisoformat(args.date) if args.date else today_jst()
    draft = bookqueue.load_draft(day)

    if draft is None:
        print(f"[skip] {day} の下書きがありません。投稿しません。")
        return 0
    if draft.get("status") == "posted":
        print(f"[skip] {day} は投稿済みです (media_id={draft.get('media_id')})。")
        return 0

    image_urls = [
        f"{secrets.pages_base_url}/img/{day.isoformat()}/{i:02d}.jpg"
        for i in range(1, len(draft["cards"]) + 1)
    ]
    caption = build_caption(draft)

    if args.dry_run:
        print(f"[dry-run] {day} 『{draft['book_title']}』 Day{draft['day_index']}")
        for url in image_urls:
            print(f"  image: {url}")
        print("--- caption ---")
        print(caption)
        return 0

    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    print(f"[post] {day} 『{draft['book_title']}』 Day{draft['day_index']} を投稿中…")
    try:
        media_id = publish_carousel(client, image_urls, caption)
    except PublishError as error:
        print(f"::error::投稿に失敗しました: {error}", file=sys.stderr)
        return 1

    draft["status"] = "posted"
    draft["media_id"] = media_id
    draft["posted_at"] = datetime.now(JST).isoformat()
    bookqueue.save_draft(day, draft)
    _append_log(
        {
            "date": day.isoformat(),
            "media_id": media_id,
            "book_title": draft["book_title"],
            "day_index": draft["day_index"],
            "posted_at": draft["posted_at"],
        }
    )
    print(f"[done] 投稿しました: media_id={media_id}")

    days_left = client.token_days_remaining()
    if days_left is not None and days_left <= 14:
        print(f"::warning::アクセストークンの残り有効期間が{days_left}日です。更新してください。")
    return 0


def _append_log(record: dict[str, Any]) -> None:
    with POSTED_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------- preview


def cmd_preview(args: argparse.Namespace) -> int:
    secrets = load_secrets(require=())
    start = date.fromisoformat(args.start) if args.start else today_jst()
    _write_previews(start, secrets.pages_base_url)
    return 0


# ----------------------------------------------------------------------------- doctor


def cmd_doctor(_: argparse.Namespace) -> int:
    problems = 0

    try:
        secrets = load_secrets()
        print("[ok] 環境変数はすべて設定されています。")
    except RuntimeError as error:
        print(f"[NG] {error}")
        return 1

    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    try:
        info = client.account_info()
        print(f"[ok] Instagram アカウント: @{info.get('username')} ({info.get('name', '')})")
    except PublishError as error:
        print(f"[NG] Instagram アカウントに接続できません: {error}")
        problems += 1

    days_left = client.token_days_remaining()
    if days_left is None:
        print("[--] トークンの有効期限を取得できませんでした（無期限トークンの可能性があります）。")
    elif days_left <= 14:
        print(f"[NG] トークンの残り有効期間が{days_left}日です。更新してください。")
        problems += 1
    else:
        print(f"[ok] トークンの残り有効期間: {days_left}日")

    data = bookqueue.load_queue()
    pending = len(bookqueue.pending_books(data))
    coverage = bookqueue.coverage_days(today_jst())
    print(f"[--] キュー残: {pending}冊 / 投稿在庫: {coverage}日分")
    if pending < QUEUE_LOW_THRESHOLD:
        print(f"[NG] キューの残りが少なすぎます（{QUEUE_LOW_THRESHOLD}冊未満）。")
        problems += 1

    return 1 if problems else 0


# ------------------------------------------------------------------------------ whoami


def cmd_whoami(_: argparse.Namespace) -> int:
    """IG_ACCESS_TOKEN だけを使って IG_USER_ID を調べる（セットアップ用）。"""
    secrets = load_secrets(require=("IG_ACCESS_TOKEN",))
    client = InstagramClient(
        "me", secrets.ig_access_token, secrets.graph_api_version, secrets.api_host
    )
    try:
        info = client.whoami()
    except PublishError as error:
        print(f"[NG] {error}", file=sys.stderr)
        return 1
    user_id = info.get("user_id") or info.get("id", "")
    print(f"username : {info.get('username', '(取得できず)')}")
    print(f"IG_USER_ID: {user_id}")
    print("この IG_USER_ID を GitHub Secrets と .env に設定してください。")
    return 0


# ----------------------------------------------------------------------- refresh-token


def cmd_refresh_token(_: argparse.Namespace) -> int:
    secrets = load_secrets(require=("IG_USER_ID", "IG_ACCESS_TOKEN"))
    client = InstagramClient(
        secrets.ig_user_id,
        secrets.ig_access_token,
        secrets.graph_api_version,
        secrets.api_host,
    )
    try:
        result = client.refresh_long_lived_token()
    except PublishError as error:
        print(f"[NG] {error}", file=sys.stderr)
        return 1
    days = int(result.get("expires_in", 0)) // 86400
    print("新しい長期アクセストークン（GitHub Secrets の IG_ACCESS_TOKEN を差し替えてください）:")
    print(result["access_token"])
    print(f"有効期間: 約{days}日")
    return 0


# ---------------------------------------------------------------------------- cleanup


def cmd_cleanup(_: argparse.Namespace) -> int:
    if not IMG_DIR.exists():
        return 0
    cutoff = today_jst() - timedelta(days=IMAGE_RETENTION_DAYS)
    removed = 0
    for entry in IMG_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            entry_date = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if entry_date < cutoff:
            shutil.rmtree(entry)
            removed += 1
    print(f"[cleanup] {removed} 日分の画像を削除しました（{IMAGE_RETENTION_DAYS}日より前）。")
    return 0


# -------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bookgram", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="週次の下書き生成")
    p_gen.add_argument("--start", help="生成開始日 (YYYY-MM-DD)。既定は明日。")
    p_gen.set_defaults(func=cmd_generate)

    p_post = sub.add_parser("post", help="その日の下書きを投稿")
    p_post.add_argument("--date", help="投稿対象日 (YYYY-MM-DD)。既定は本日。")
    p_post.add_argument("--dry-run", action="store_true", help="投稿せず内容を表示する")
    p_post.set_defaults(func=cmd_post)

    p_prev = sub.add_parser("preview", help="プレビューページを作り直す")
    p_prev.add_argument("--start", help="表示開始日 (YYYY-MM-DD)。既定は本日。")
    p_prev.set_defaults(func=cmd_preview)

    sub.add_parser("doctor", help="設定と接続の点検").set_defaults(func=cmd_doctor)
    sub.add_parser("whoami", help="トークンから IG_USER_ID を調べる").set_defaults(
        func=cmd_whoami
    )
    sub.add_parser("refresh-token", help="長期トークンを延長する").set_defaults(
        func=cmd_refresh_token
    )
    sub.add_parser("cleanup", help="古い画像を削除").set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
